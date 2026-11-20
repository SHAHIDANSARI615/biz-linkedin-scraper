import os
import sys
import time
import re
import json
import threading
from datetime import datetime, timezone, timedelta
import pandas as pd
import requests
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, send_from_directory
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from linkedin_extractor import LinkedInScraper, scrape_company

# UTF-8 stdout
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "biz-linkedin-scraper-secret-2026")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB max upload
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=48)

IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now_str(fmt="%d %b %Y, %I:%M %p"):
    """Format current time strictly in Indian Standard Time (IST, UTC+5:30)."""
    return datetime.now(IST).strftime(fmt)


ALLOWED_DOMAIN = "bizprospex.com"

INPUT_FILE = "input.xlsx"
OUTPUT_FILE = "output.xlsx"
OUTPUT_CSV = "output.csv"
EXPORTS_DIR = "exports"
HISTORY_FILE = os.path.join(EXPORTS_DIR, "history.json")

if not os.path.exists(EXPORTS_DIR):
    os.makedirs(EXPORTS_DIR, exist_ok=True)


@app.before_request
def require_login():
    """Restrict access: Only users logged in with @bizprospex.com can access the app."""
    allowed_prefixes = ["/static/", "/login", "/favicon.ico", "/health"]
    if any(request.path.startswith(p) for p in allowed_prefixes):
        return None
    if not session.get("user"):
        return redirect(url_for("login"))


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.svg", mimetype="image/svg+xml")


@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    google_configured = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
    return render_template("login.html", google_configured=google_configured, error=None)


@app.route("/login/google")
def login_google():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        return render_template(
            "login.html",
            error="Google OAuth Client ID is not yet configured in server environment variables. Please add GOOGLE_CLIENT_ID & GOOGLE_CLIENT_SECRET in Render Environment Settings, or sign in below with your corporate @bizprospex.com email.",
            google_configured=False,
        )
    redirect_uri = url_for("login_callback", _external=True)
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = "https://" + redirect_uri[7:]

    scope = "openid email profile"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&hd=bizprospex.com"
        f"&prompt=select_account"
    )
    return redirect(auth_url)


LOGIN_AUDIT_FILE = os.path.join(EXPORTS_DIR, "login_audit.json")
ADMINS_FILE = os.path.join(EXPORTS_DIR, "admins.json")
DEFAULT_ADMIN = "shahid@bizprospex.com"


def get_admin_emails():
    """Returns list of authorized admin emails."""
    admins = [DEFAULT_ADMIN.lower()]
    if os.path.exists(ADMINS_FILE):
        try:
            with open(ADMINS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, list):
                    admins = list(set([e.lower().strip() for e in saved if e and isinstance(e, str)] + [DEFAULT_ADMIN.lower()]))
        except Exception:
            pass
    return admins


def is_user_admin(email):
    """Check if email belongs to authorized admin."""
    if not email:
        return False
    return email.lower().strip() in get_admin_emails()


def save_admin_emails(admins_list):
    """Save persistent list of admin emails."""
    try:
        clean = list(set([e.lower().strip() for e in admins_list if e and e.strip()]))
        if DEFAULT_ADMIN.lower() not in clean:
            clean.append(DEFAULT_ADMIN.lower())
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
    except Exception as e:
        print(f"[Error saving admins] {e}")


def record_login_audit(email):
    """Record corporate login audit log (email and timestamp only)."""
    try:
        events = []
        if os.path.exists(LOGIN_AUDIT_FILE):
            try:
                with open(LOGIN_AUDIT_FILE, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except Exception:
                events = []
        event = {
            "email": email,
            "time": get_ist_now_str("%d %b %Y, %I:%M %p"),
        }
        events.insert(0, event)
        events = events[:500]
        with open(LOGIN_AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)
        print(f"🔔 [LOGIN AUDIT] User: {email} | Time: {event['time']}")
    except Exception as e:
        print(f"[Audit Error] {e}")


@app.route("/login/callback")
def login_callback():
    code = request.args.get("code")
    err = request.args.get("error")

    if err or not code:
        return render_template("login.html", error=f"Google sign-in failed: {err or 'No authorization code'}", google_configured=True)

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    redirect_uri = url_for("login_callback", _external=True)
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = "https://" + redirect_uri[7:]

    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        if not token_resp.ok:
            return render_template("login.html", error="Failed to verify token with Google.", google_configured=True)

        access_token = token_resp.json().get("access_token")
        user_resp = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if not user_resp.ok:
            return render_template("login.html", error="Failed to fetch user profile from Google.", google_configured=True)

        userinfo = user_resp.json()
        email = userinfo.get("email", "").lower().strip()
        user_name = userinfo.get("name", email.split("@")[0])

        # STRICT DOMAIN CHECK: ONLY @bizprospex.com
        if not email.endswith(f"@{ALLOWED_DOMAIN}"):
            return render_template(
                "login.html",
                error=f"Account '{email}' is not permitted. Only verified @{ALLOWED_DOMAIN} accounts can access Biz Linkedin Scraper.",
                google_configured=True,
            )

        # Successful Login Audit (Records Email and Timestamp)
        record_login_audit(email)

        session.permanent = True
        session["user"] = {
            "email": email,
            "name": user_name,
            "picture": userinfo.get("picture", ""),
        }
        return redirect(url_for("index"))
    except Exception as e:
        return render_template("login.html", error=f"Authentication error: {str(e)}", google_configured=True)


@app.route("/api/admin/logins")
def api_admin_logins():
    """Returns recent login audit activity (Admin only)."""
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    if not is_user_admin(user_email):
        return jsonify({"error": "Unauthorized. Admin rights required."}), 403

    events = []
    if os.path.exists(LOGIN_AUDIT_FILE):
        try:
            with open(LOGIN_AUDIT_FILE, "r", encoding="utf-8") as f:
                events = json.load(f)
        except Exception:
            events = []
    return jsonify({"success": True, "logins": events, "admins": get_admin_emails()})


@app.route("/api/admin/transfer", methods=["POST"])
def api_admin_transfer():
    """Transfer or delegate admin access to another @bizprospex.com account."""
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    if not is_user_admin(user_email):
        return jsonify({"success": False, "message": "Unauthorized. Admin rights required."}), 403

    data = request.json or {}
    target_email = data.get("email", "").lower().strip()
    action = data.get("action", "add")  # "add" or "remove"

    if not target_email or not target_email.endswith(f"@{ALLOWED_DOMAIN}"):
        return jsonify({"success": False, "message": f"Invalid email. Must be an active @{ALLOWED_DOMAIN} address."}), 400

    admins = get_admin_emails()
    if action == "add":
        if target_email not in admins:
            admins.append(target_email)
            save_admin_emails(admins)
        return jsonify({"success": True, "message": f"Admin access successfully granted to {target_email}.", "admins": admins})
    elif action == "remove":
        if target_email == DEFAULT_ADMIN.lower():
            return jsonify({"success": False, "message": "Primary admin (Shahid Ansari) cannot be removed."}), 400
        admins = [a for a in admins if a != target_email]
        save_admin_emails(admins)
        return jsonify({"success": True, "message": f"Admin access removed for {target_email}.", "admins": admins})

    return jsonify({"success": False, "message": "Invalid action."}), 400


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Biz Linkedin Scraper", "domain": ALLOWED_DOMAIN})



def clean_value_or_dash(val, field_name=""):
    """
    Format every value cleanly: if empty/blank/None/nan/N/A, strictly return '-'.
    Also purges XML/Excel control characters (e.g. \\x0b, \\x00-\\x08) to prevent crashes.
    """
    if field_name == "Success":
        if isinstance(val, bool):
            return "Yes" if val else "No"
        return str(val) if val else "No"

    if field_name in ("Employees Min", "Employees Max"):
        if val is None or val == 0 or str(val).strip() in ("", "0", "None"):
            return "-"
        return str(val)

    if val is None:
        return "-"
    if isinstance(val, bool):
        return "-"

    s = str(val).strip()
    if not s or s.lower() in ("none", "nan", "null", "n/a", "unknown", "no name found", "no website", "no data"):
        return "-"

    # Strip illegal XML/Excel characters
    s = ILLEGAL_CHARACTERS_RE.sub("", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", s).strip()
    return s if s else "-"


def clean_for_excel(val):
    """Strip illegal XML/Excel characters and format blanks as '-'."""
    return clean_value_or_dash(val)


ALL_FIELD_EXTRACTORS = {
    "LinkedIn URL": lambda r: r.get("linkedin_url", ""),
    "Company Name": lambda r: r.get("name", ""),
    "Website": lambda r: r.get("website", ""),
    "Industry": lambda r: r.get("industry", ""),
    "About / Overview": lambda r: r.get("description", ""),
    "Description": lambda r: r.get("description", ""),
    "Company Size": lambda r: r.get("company_size", ""),
    "Employee Count": lambda r: r.get("employee_count", ""),
    "Employees Min": lambda r: r.get("employees_min", ""),
    "Employees Max": lambda r: r.get("employees_max", ""),
    "Headquarters": lambda r: r.get("headquarters", ""),
    "Founded": lambda r: r.get("founded", ""),
    "Followers": lambda r: r.get("followers", ""),
    "Method Used": lambda r: r.get("method_used", ""),
    "Success": lambda r: r.get("success", False)
}


def format_row(r: dict, selected_fields: list = None) -> dict:
    """Format and sanitize result dictionary into clean row based on selected fields.
    Guarantees that ANY blank field returns '-'."""
    if not selected_fields or len(selected_fields) == 0:
        fields_order = [
            "LinkedIn URL", "Company Name", "Website", "Industry",
            "About / Overview", "Company Size",
            "Employee Count", "Employees Min", "Employees Max",
            "Headquarters", "Founded", "Followers", "Method Used", "Success"
        ]
    else:
        fields_order = []
        if "LinkedIn URL" not in selected_fields:
            fields_order.append("LinkedIn URL")
        for f in selected_fields:
            if f in ALL_FIELD_EXTRACTORS and f not in fields_order:
                fields_order.append(f)
        if "Success" not in fields_order:
            fields_order.append("Success")

    row = {}
    for f in fields_order:
        extractor = ALL_FIELD_EXTRACTORS.get(f, lambda x: "")
        val = extractor(r)
        row[f] = clean_value_or_dash(val, f)
    return row


def safe_save(results_list, excel_path=OUTPUT_FILE, csv_path=OUTPUT_CSV):
    """Safely saves results to Excel and CSV, ensuring all blanks are '-'."""
    if not results_list:
        return

    df = pd.DataFrame(results_list)
    df = df.fillna("-")
    for col in df.columns:
        df[col] = df[col].apply(lambda v: clean_value_or_dash(v, col))

    # 1. Save CSV
    try:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"[Warning] CSV save error: {e}")

    # 2. Save Excel
    try:
        df.to_excel(excel_path, index=False, engine="openpyxl")
    except PermissionError:
        backup_name = f"output_autosave_{int(time.time())}.xlsx"
        print(f"[Warning] '{excel_path}' is open in Excel! Saved to '{backup_name}'.")
        try:
            df.to_excel(backup_name, index=False, engine="openpyxl")
        except Exception:
            pass
    except Exception as e:
        print(f"[Warning] Excel save error: {e}")


# ==============================
# RUN HISTORY MANAGEMENT
# ==============================

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[Warning] History save error: {e}")


def add_run_to_history(run_id, source_name, total, extracted, failed, fields, records, user_email="", executed_by=""):
    history = load_history()
    excel_name = f"{run_id}.xlsx"
    csv_name = f"{run_id}.csv"
    excel_path = os.path.join(EXPORTS_DIR, excel_name)
    csv_path = os.path.join(EXPORTS_DIR, csv_name)

    safe_save(records, excel_path, csv_path)

    entry = {
        "id": run_id,
        "timestamp": get_ist_now_str("%d %b %Y, %I:%M %p"),
        "source": source_name,
        "total": total,
        "extracted": extracted,
        "failed": failed,
        "fields": fields or ["All Fields"],
        "user_email": (user_email or "").lower().strip(),
        "executed_by": executed_by or user_email or "Team Member",
        "excel_file": excel_name,
        "csv_file": csv_name
    }
    history.insert(0, entry)
    save_history(history)
    return entry


class ScraperManager:
    def __init__(self, user_email=""):
        self.user_email = (user_email or "").lower().strip()
        self.lock = threading.RLock()
        self.thread = None
        self.is_running = False
        self.is_paused = False
        self.should_stop = False
        self.total = 0
        self.completed = 0
        self.success_count = 0
        self.fail_count = 0
        self.current_url = ""
        self.logs = []
        self.session_records = []
        self.all_urls = []
        self.remaining_urls = []
        self.is_manual = False
        self.active_run_id = ""
        self.source_name = ""
        self.batch_size = 20
        self.max_workers = 3
        self.timeout = 15
        self.selected_fields = None
        self.last_completed_run = None

        safe_prefix = re.sub(r"[^a-zA-Z0-9_]", "_", self.user_email) or "default"
        self.user_output_xlsx = os.path.join(EXPORTS_DIR, f"current_{safe_prefix}.xlsx")
        self.user_output_csv = os.path.join(EXPORTS_DIR, f"current_{safe_prefix}.csv")

    def add_log(self, msg, level="info"):
        timestamp = get_ist_now_str("%H:%M:%S")
        log_entry = {"time": timestamp, "msg": msg, "level": level}
        with self.lock:
            self.logs.append(log_entry)
            if len(self.logs) > 300:
                self.logs.pop(0)

    def get_status(self):
        with self.lock:
            percent = round((self.completed / self.total * 100), 1) if self.total > 0 else 0
            latest = list(reversed(self.session_records[-50:])) if self.session_records else []
            rem_count = len(self.remaining_urls) if self.remaining_urls else 0
            can_resume = bool(
                not self.is_running
                and rem_count > 0
                and self.is_paused
            )
            return {
                "is_running": self.is_running,
                "is_paused": self.is_paused,
                "can_resume": can_resume,
                "remaining_count": rem_count,
                "total": self.total,
                "completed": self.completed,
                "success_count": self.success_count,
                "fail_count": self.fail_count,
                "percent": percent,
                "current_url": self.current_url,
                "logs": list(self.logs),
                "latest_items": latest,
                "total_session_items": len(self.session_records),
                "last_run": self.last_completed_run
            }

    def start_scraping(self, urls, max_workers=3, timeout=15, batch_size=20, resume=False, selected_fields=None, is_manual=False, user_email="", executed_by=""):
        with self.lock:
            if self.is_running:
                return False, "Scraper is already running for your account"

            self.is_running = True
            self.is_paused = False
            self.should_stop = False
            self.max_workers = max_workers
            self.timeout = timeout
            self.batch_size = batch_size
            self.selected_fields = selected_fields
            self.user_email = (user_email or self.user_email or "").lower().strip()
            self.executed_by = executed_by or "Team Member"
            self.is_manual = is_manual
            self.current_url = ""
            self.logs = []
            self.last_completed_run = None

            if not resume:
                self.all_urls = list(dict.fromkeys(urls))
                self.remaining_urls = list(self.all_urls)
                self.total = len(self.all_urls)
                self.completed = 0
                self.success_count = 0
                self.fail_count = 0
                self.session_records = []
                self.active_run_id = f"run_{get_ist_now_str('%Y%m%d_%H%M%S')}"
                self.source_name = f"Pasted URLs ({len(self.all_urls)})" if is_manual else f"Excel File ({len(self.all_urls)} URLs)"

        self.thread = threading.Thread(
            target=self._run_bulk,
            args=(self.remaining_urls if resume else self.all_urls, resume, is_manual),
            daemon=True
        )
        self.thread.start()
        return True, "Scraping started"

    def stop_scraping(self):
        with self.lock:
            if not self.is_running:
                return False, "Scraper is not running"
            self.should_stop = True
            self.is_running = False
            self.is_paused = True
            self.current_url = ""

        self.add_log("Stop requested by user. Pausing session immediately...", "warning")
        return True, "Scraper stopped"

    def resume_scraping(self):
        with self.lock:
            if self.is_running:
                return False, "Scraper is already running for your account"
            if not self.remaining_urls:
                return False, "No remaining URLs to resume"

            self.is_running = True
            self.should_stop = False
            self.is_paused = False
            self.current_url = ""
            urls_to_run = list(self.remaining_urls)

        self.add_log(f"Resuming scraping for {len(urls_to_run)} remaining URLs ({self.completed}/{self.total} previously done)...", "info")
        self.thread = threading.Thread(
            target=self._run_bulk,
            args=(urls_to_run, True, getattr(self, "is_manual", False)),
            daemon=True
        )
        self.thread.start()
        return True, f"Resumed scraping for {len(urls_to_run)} remaining URLs"

    def reset_session(self):
        with self.lock:
            if not self.is_running:
                self.remaining_urls = []
                self.all_urls = []
                self.is_paused = False
                self.completed = 0
                self.total = 0
                self.success_count = 0
                self.fail_count = 0
                self.session_records = []
                self.current_url = ""
                self.last_completed_run = None
                self.add_log("Session reset. Ready for a new scraping run.", "info")

    def _run_bulk(self, urls_to_process, resume, is_manual):
        import concurrent.futures

        pending = list(dict.fromkeys(urls_to_process))

        if not resume:
            with self.lock:
                self.total = len(self.all_urls)
            self.add_log(f"Starting scraping run [{self.active_run_id}] for {len(pending)} URLs...", "info")
        else:
            self.add_log(f"Resuming scraping [{self.active_run_id}] for {len(pending)} remaining URLs...", "info")

        if self.selected_fields:
            self.add_log(f"Selected Fields: {', '.join(self.selected_fields)}", "info")

        if not pending:
            self.add_log("No URLs to scrape.", "error")
            with self.lock:
                self.is_running = False
            return

        # Max 3 workers for cloud stability & rate limit prevention
        worker_count = min(max(1, self.max_workers), 3)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
        try:
            futures = {}
            url_iter = iter(enumerate(pending))

            # Queue initial tasks
            for _ in range(worker_count * 2):
                try:
                    idx, u = next(url_iter)
                    fut = executor.submit(scrape_company, u, self.timeout)
                    futures[fut] = (idx, u)
                except StopIteration:
                    break

            while futures:
                with self.lock:
                    stopping = self.should_stop
                    if stopping:
                        for f in list(futures.keys()):
                            f.cancel()
                        break

                done, _ = concurrent.futures.wait(
                    futures.keys(),
                    timeout=0.3,
                    return_when=concurrent.futures.FIRST_COMPLETED
                )

                if not done:
                    continue

                for fut in done:
                    if fut not in futures:
                        continue
                    idx, url = futures.pop(fut)

                    with self.lock:
                        if self.should_stop:
                            continue

                    try:
                        res = fut.result()
                    except Exception as e:
                        res = {"success": False, "linkedin_url": url, "error": str(e)}

                    success = res.get("success", False)
                    name = res.get("name", "")
                    method = res.get("method_used", "")
                    formatted_single = format_row(res, self.selected_fields)

                    with self.lock:
                        self.current_url = url
                        self.completed += 1
                        self.session_records.append(formatted_single)
                        if url in self.remaining_urls:
                            self.remaining_urls.remove(url)

                        if success:
                            self.success_count += 1
                            msg = f"SUCCESS: {name or 'Found data'} via {method} ({url})"
                            lvl = "success"
                        else:
                            err = res.get("error", "No data / Protected")
                            self.fail_count += 1
                            msg = f"FAILED: {url} -> {err}"
                            lvl = "error"

                    self.add_log(msg, lvl)

                    # Auto-save every 5 URLs to user's dedicated file
                    if self.completed % 5 == 0:
                        with self.lock:
                            recs_to_save = list(self.session_records)
                        safe_save(recs_to_save, self.user_output_xlsx, self.user_output_csv)

                    # Submit next URL if not requested to stop
                    with self.lock:
                        stopping = self.should_stop

                    if not stopping:
                        try:
                            next_idx, next_url = next(url_iter)
                            next_fut = executor.submit(scrape_company, next_url, self.timeout)
                            futures[next_fut] = (next_idx, next_url)
                        except StopIteration:
                            pass

        except Exception as e:
            self.add_log(f"Error during scraping: {str(e)}", "error")
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                try:
                    executor.shutdown(wait=False)
                except Exception:
                    pass

            with self.lock:
                records_to_save = list(self.session_records)
                self.is_running = False
                self.current_url = ""
                was_stopped = self.should_stop or (len(self.remaining_urls) > 0 and self.completed < self.total)

            safe_save(records_to_save, self.user_output_xlsx, self.user_output_csv)

            # Record run in persistent history with dedicated files
            if records_to_save:
                run_entry = add_run_to_history(
                    self.active_run_id,
                    self.source_name,
                    self.total,
                    self.success_count,
                    self.fail_count,
                    self.selected_fields,
                    records_to_save,
                    user_email=getattr(self, "user_email", ""),
                    executed_by=getattr(self, "executed_by", "Team Member")
                )
                with self.lock:
                    self.last_completed_run = run_entry

            if was_stopped:
                self.add_log(f"Scraper paused. {self.completed}/{self.total} completed ({len(self.remaining_urls)} remaining). Click Resume anytime.", "warning")
            else:
                self.add_log(f"Scraping run completed! All {len(records_to_save)} records saved cleanly in Run History.", "success")


# Multi-User Isolated Managers
USER_MANAGERS = {}
managers_lock = threading.RLock()


def get_user_manager(email):
    """Retrieve or create an isolated scraper manager per user."""
    clean_email = (email or "default").lower().strip()
    with managers_lock:
        if clean_email not in USER_MANAGERS:
            USER_MANAGERS[clean_email] = ScraperManager(clean_email)
        return USER_MANAGERS[clean_email]


# ==============================
# ROUTES
# ==============================

@app.route("/")
def index():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    is_admin = is_user_admin(user_email)
    input_count = 0
    if os.path.exists(INPUT_FILE):
        try:
            df = pd.read_excel(INPUT_FILE)
            if "LinkedIn URL" in df.columns:
                input_count = df["LinkedIn URL"].dropna().count()
        except Exception:
            pass

    return render_template("index.html", input_file=INPUT_FILE, input_count=input_count, is_admin=is_admin)


@app.route("/api/status")
def api_status():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    mgr = get_user_manager(user_email)
    return jsonify(mgr.get_status())


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    workers = int(data.get("workers", 3))
    timeout = int(data.get("timeout", 15))
    batch_size = int(data.get("batch_size", 20))
    selected_fields = data.get("fields", None)

    manual_urls = data.get("urls", [])
    if manual_urls and len(manual_urls) > 0:
        urls = [u.strip() for u in manual_urls if u.strip()]
        is_manual = True
    else:
        is_manual = False
        if not os.path.exists(INPUT_FILE):
            return jsonify({"success": False, "message": f"{INPUT_FILE} not found."}), 400
        try:
            df = pd.read_excel(INPUT_FILE)
            if "LinkedIn URL" not in df.columns:
                return jsonify({"success": False, "message": "'LinkedIn URL' column missing in input file."}), 400
            urls = df["LinkedIn URL"].dropna().astype(str).str.strip().tolist()
        except Exception as e:
            return jsonify({"success": False, "message": f"Error reading input: {e}"}), 400

    if not urls:
        return jsonify({"success": False, "message": "No URLs found to scrape."}), 400

    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    user_label = user.get("name") or user_email.split("@")[0]
    mgr = get_user_manager(user_email)

    ok, msg = mgr.start_scraping(
        urls,
        max_workers=workers,
        timeout=timeout,
        batch_size=batch_size,
        resume=False,
        selected_fields=selected_fields,
        is_manual=is_manual,
        user_email=user_email,
        executed_by=user_label
    )
    if ok:
        return jsonify({"success": True, "message": msg, "total": len(urls)})
    return jsonify({"success": False, "message": msg}), 400


@app.route("/api/stop", methods=["POST"])
def api_stop():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    mgr = get_user_manager(user_email)
    ok, msg = mgr.stop_scraping()
    return jsonify({"success": ok, "message": msg})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    mgr = get_user_manager(user_email)
    ok, msg = mgr.resume_scraping()
    if ok:
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "message": msg}), 400


@app.route("/api/reset-session", methods=["POST"])
def api_reset_session():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    mgr = get_user_manager(user_email)
    mgr.reset_session()
    return jsonify({"success": True, "message": "Session reset successfully"})


@app.route("/api/scrape-single", methods=["POST"])
def api_scrape_single():
    data = request.json or {}
    url = data.get("url", "").strip()
    timeout = int(data.get("timeout", 15))
    selected_fields = data.get("fields", None)
    if not url:
        return jsonify({"success": False, "message": "Please enter a valid LinkedIn URL"}), 400

    try:
        res = scrape_company(url, timeout=timeout)
        formatted = format_row(res, selected_fields)
        return jsonify({"success": True, "data": formatted})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"success": False, "message": "No file uploaded"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "message": "No file selected"}), 400

    filename = file.filename.lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xls") or filename.endswith(".csv")):
        return jsonify({"success": False, "message": "Only .xlsx, .xls, or .csv files are supported"}), 400

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
            df.to_excel(INPUT_FILE, index=False)
        else:
            df = pd.read_excel(file)
            df.to_excel(INPUT_FILE, index=False)

        count = 0
        if "LinkedIn URL" in df.columns:
            count = df["LinkedIn URL"].dropna().count()
        else:
            for c in df.columns:
                if "linkedin" in str(c).lower():
                    df = df.rename(columns={c: "LinkedIn URL"})
                    df.to_excel(INPUT_FILE, index=False)
                    count = df["LinkedIn URL"].dropna().count()
                    break

        return jsonify({
            "success": True,
            "message": f"Successfully uploaded {file.filename}",
            "count": count
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to parse file: {str(e)}"}), 500


# ==============================
# RUN HISTORY API ROUTES (PER-USER ISOLATED)
# ==============================

@app.route("/api/history")
def api_history():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    is_admin = is_user_admin(user_email)
    history = load_history()

    # STRICT PRIVACY & MULTI-TENANT ISOLATION:
    # Every user (including admin) sees ONLY their own scraping runs in their history table.
    # Admin privileges are dedicated to viewing Login Activity and managing roles in the Admin Console.
    runs_to_show = [
        r for r in history
        if r.get("user_email", "").lower().strip() == user_email
        or (not r.get("user_email") and user_email in r.get("executed_by", "").lower())
    ]

    return jsonify({
        "success": True,
        "history": runs_to_show,
        "is_admin": is_admin
    })


@app.route("/api/history/<run_id>/download/<file_type>")
def api_download_history_file(run_id, file_type):
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    is_admin = is_user_admin(user_email)

    history = load_history()
    run = next((r for r in history if r.get("id") == run_id), None)
    if not run:
        return jsonify({"error": "Run not found"}), 404

    # Security check: User must own the run or be an admin
    if run.get("user_email", "").lower().strip() != user_email and not is_admin:
        return jsonify({"error": "Unauthorized access to this run"}), 403

    if file_type.lower() == "excel":
        file_path = os.path.join(EXPORTS_DIR, run.get("excel_file", f"{run_id}.xlsx"))
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True, download_name=f"{run_id}_companies.xlsx")
    elif file_type.lower() == "csv":
        file_path = os.path.join(EXPORTS_DIR, run.get("csv_file", f"{run_id}.csv"))
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True, download_name=f"{run_id}_companies.csv")

    return jsonify({"error": "File not found"}), 404


@app.route("/api/history/<run_id>/records")
def api_history_records(run_id):
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    is_admin = is_user_admin(user_email)

    history = load_history()
    run = next((r for r in history if r.get("id") == run_id), None)
    if not run:
        return jsonify({"error": "Run not found"}), 404

    # Security check: User must own the run or be an admin
    if run.get("user_email", "").lower().strip() != user_email and not is_admin:
        return jsonify({"error": "Unauthorized access to this run"}), 403

    csv_path = os.path.join(EXPORTS_DIR, f"{run_id}.csv")
    excel_path = os.path.join(EXPORTS_DIR, f"{run_id}.xlsx")
    rows = []

    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            rows = df.head(100).fillna("").to_dict(orient="records")
        except Exception:
            pass
    elif os.path.exists(excel_path):
        try:
            df = pd.read_excel(excel_path)
            rows = df.head(100).fillna("").to_dict(orient="records")
        except Exception:
            pass

    return jsonify({"rows": rows})


@app.route("/api/history/<run_id>/delete", methods=["POST"])
def api_history_delete(run_id):
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    is_admin = is_user_admin(user_email)

    history = load_history()
    run = next((r for r in history if r.get("id") == run_id), None)
    if not run:
        return jsonify({"error": "Run not found"}), 404

    # Security check: User must own the run or be an admin
    if run.get("user_email", "").lower().strip() != user_email and not is_admin:
        return jsonify({"error": "Unauthorized access to this run"}), 403

    updated = [r for r in history if r.get("id") != run_id]
    save_history(updated)

    for ext in [".xlsx", ".csv"]:
        fpath = os.path.join(EXPORTS_DIR, f"{run_id}{ext}")
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass

    return jsonify({"success": True})


@app.route("/api/download/excel")
def download_excel():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    mgr = get_user_manager(user_email)
    target = mgr.user_output_xlsx if os.path.exists(mgr.user_output_xlsx) else OUTPUT_FILE
    if os.path.exists(target):
        return send_file(target, as_attachment=True, download_name="Biz_LinkedIn_Companies.xlsx")
    return jsonify({"error": "No output file found"}), 404


@app.route("/api/download/csv")
def download_csv():
    user = session.get("user", {})
    user_email = user.get("email", "").lower().strip()
    mgr = get_user_manager(user_email)
    target = mgr.user_output_csv if os.path.exists(mgr.user_output_csv) else OUTPUT_CSV
    if os.path.exists(target):
        return send_file(target, as_attachment=True, download_name="Biz_LinkedIn_Companies.csv")
    return jsonify({"error": "No CSV file found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print("  🚀 Biz Linkedin Scraper is starting!")
    print(f"  💻 Local Port:        http://localhost:{port}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False)
