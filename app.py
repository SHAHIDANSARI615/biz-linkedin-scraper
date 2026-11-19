import os
import sys
import time
import re
import json
import threading
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


def record_login_audit(email, name, ip, status="SUCCESS", reason=""):
    """Record corporate login audit log for tracking and security."""
    try:
        events = []
        if os.path.exists(LOGIN_AUDIT_FILE):
            try:
                with open(LOGIN_AUDIT_FILE, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except Exception:
                events = []
        event = {
            "time": time.strftime("%d %b %Y, %I:%M:%S %p"),
            "email": email,
            "name": name,
            "ip": ip,
            "status": status,
            "reason": reason,
        }
        events.insert(0, event)
        events = events[:300]
        with open(LOGIN_AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)
        print(f"🔔 [LOGIN AUDIT] {status} - User: {name} ({email}) | IP: {ip} | Time: {event['time']}")
    except Exception as e:
        print(f"[Audit Error] {e}")


@app.route("/login/callback")
def login_callback():
    code = request.args.get("code")
    err = request.args.get("error")
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()

    if err or not code:
        record_login_audit("unknown", "Unknown", client_ip, "FAILED", err or "No authorization code")
        return render_template("login.html", error=f"Google login failed: {err or 'No authorization code'}", google_configured=True)

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
            record_login_audit("unknown", "Unknown", client_ip, "FAILED", "Invalid Google Token")
            return render_template("login.html", error="Failed to verify token with Google.", google_configured=True)

        access_token = token_resp.json().get("access_token")
        user_resp = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if not user_resp.ok:
            record_login_audit("unknown", "Unknown", client_ip, "FAILED", "Failed fetching user info")
            return render_template("login.html", error="Failed to fetch user profile from Google.", google_configured=True)

        userinfo = user_resp.json()
        email = userinfo.get("email", "").lower().strip()
        user_name = userinfo.get("name", email.split("@")[0])

        # STRICT DOMAIN CHECK: ONLY @bizprospex.com
        if not email.endswith(f"@{ALLOWED_DOMAIN}"):
            record_login_audit(email, user_name, client_ip, "BLOCKED", f"Rejected: Not @{ALLOWED_DOMAIN}")
            return render_template(
                "login.html",
                error=f"Account '{email}' is not permitted. Only verified @{ALLOWED_DOMAIN} accounts can access Biz Linkedin Scraper.",
                google_configured=True,
            )

        # Successful Login Audit
        record_login_audit(email, user_name, client_ip, "SUCCESS", "Logged In")

        session["user"] = {
            "email": email,
            "name": user_name,
            "picture": userinfo.get("picture", ""),
        }
        return redirect(url_for("index"))
    except Exception as e:
        record_login_audit("unknown", "Unknown", client_ip, "ERROR", str(e))
        return render_template("login.html", error=f"Authentication error: {str(e)}", google_configured=True)


@app.route("/api/admin/logins")
def api_admin_logins():
    """Returns recent login audit activity."""
    if not session.get("user"):
        return jsonify({"error": "Unauthorized"}), 401
    events = []
    if os.path.exists(LOGIN_AUDIT_FILE):
        try:
            with open(LOGIN_AUDIT_FILE, "r", encoding="utf-8") as f:
                events = json.load(f)
        except Exception:
            events = []
    return jsonify({"success": True, "logins": events})


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Biz Linkedin Scraper", "domain": ALLOWED_DOMAIN})



def clean_for_excel(val):
    """Strip illegal XML/Excel characters (e.g. \\x0b, \\x00-\\x08) to prevent openpyxl crashes."""
    if isinstance(val, str):
        val = ILLEGAL_CHARACTERS_RE.sub("", val)
        val = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", val)
    return val


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
    """Format and sanitize result dictionary into clean row based on selected fields."""
    if not selected_fields or len(selected_fields) == 0:
        fields_order = [
            "LinkedIn URL", "Company Name", "Website", "Industry",
            "About / Overview", "Description", "Company Size",
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
        row[f] = clean_for_excel(val)
    return row


def safe_save(results_list, excel_path=OUTPUT_FILE, csv_path=OUTPUT_CSV):
    """Safely saves results to Excel and CSV, handling PermissionError if Excel is open."""
    if not results_list:
        return

    df = pd.DataFrame(results_list)
    for col in df.columns:
        df[col] = df[col].apply(clean_for_excel)

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


def add_run_to_history(run_id, source_name, total, extracted, failed, fields, records, executed_by=""):
    history = load_history()
    excel_name = f"{run_id}.xlsx"
    csv_name = f"{run_id}.csv"
    excel_path = os.path.join(EXPORTS_DIR, excel_name)
    csv_path = os.path.join(EXPORTS_DIR, csv_name)

    safe_save(records, excel_path, csv_path)

    entry = {
        "id": run_id,
        "timestamp": time.strftime("%d %b %Y, %I:%M %p"),
        "source": source_name,
        "total": total,
        "extracted": extracted,
        "failed": failed,
        "fields": fields or ["All Fields"],
        "executed_by": executed_by or "Bizprospex Team Member",
        "excel_file": excel_name,
        "csv_file": csv_name
    }
    history.insert(0, entry)
    save_history(history)
    return entry


class ScraperManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.is_running = False
        self.should_stop = False
        self.total = 0
        self.completed = 0
        self.success_count = 0
        self.fail_count = 0
        self.current_url = ""
        self.logs = []
        self.session_records = []
        self.batch_size = 20
        self.max_workers = 3
        self.timeout = 15
        self.selected_fields = None
        self.last_completed_run = None

    def add_log(self, msg, level="info"):
        timestamp = time.strftime("%H:%M:%S")
        log_entry = {"time": timestamp, "msg": msg, "level": level}
        with self.lock:
            self.logs.append(log_entry)
            if len(self.logs) > 300:
                self.logs.pop(0)

    def get_status(self):
        with self.lock:
            percent = round((self.completed / self.total * 100), 1) if self.total > 0 else 0
            latest = list(reversed(self.session_records[-50:])) if self.session_records else []
            return {
                "is_running": self.is_running,
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

    def start_scraping(self, urls, max_workers=3, timeout=15, batch_size=20, resume=False, selected_fields=None, is_manual=False, executed_by=""):
        with self.lock:
            if self.is_running:
                return False, "Scraper is already running"

            self.is_running = True
            self.should_stop = False
            self.max_workers = max_workers
            self.timeout = timeout
            self.batch_size = batch_size
            self.selected_fields = selected_fields
            self.executed_by = executed_by or "Bizprospex Member"
            self.completed = 0
            self.success_count = 0
            self.fail_count = 0
            self.session_records = []
            self.current_url = ""
            self.logs = []
            self.last_completed_run = None

        self.thread = threading.Thread(
            target=self._run_bulk,
            args=(urls, resume, is_manual),
            daemon=True
        )
        self.thread.start()
        return True, "Scraping started"

    def stop_scraping(self):
        with self.lock:
            if not self.is_running:
                return False, "Scraper is not running"
            self.should_stop = True
            self.add_log("Stop requested. Saving current progress...", "warning")
            return True, "Stopping..."

    def _run_bulk(self, all_urls, resume, is_manual):
        pending_urls = list(dict.fromkeys(all_urls))  # deduplicate preserving order
        run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}"
        source_name = f"Pasted URLs ({len(pending_urls)})" if is_manual else f"{INPUT_FILE} ({len(pending_urls)} URLs)"

        with self.lock:
            self.total = len(pending_urls)
            self.completed = 0
            self.success_count = 0
            self.fail_count = 0
            self.session_records = []

        self.add_log(f"Starting scraping run [{run_id}] for {len(pending_urls)} URLs...", "info")
        if self.selected_fields:
            self.add_log(f"Selected Fields: {', '.join(self.selected_fields)}", "info")

        if not pending_urls:
            self.add_log("No URLs to scrape.", "error")
            with self.lock:
                self.is_running = False
            return

        scraper = LinkedInScraper(timeout=self.timeout)

        try:
            for b_start in range(0, len(pending_urls), self.batch_size):
                with self.lock:
                    if self.should_stop:
                        self.add_log("Scraping stopped by user.", "warning")
                        break

                batch = pending_urls[b_start:b_start + self.batch_size]

                def on_progress(done_in_b, total_in_b, res):
                    url = res.get("linkedin_url", "")
                    success = res.get("success", False)
                    name = res.get("name", "")
                    method = res.get("method_used", "")

                    formatted_single = format_row(res, self.selected_fields)

                    with self.lock:
                        self.current_url = url
                        self.completed = b_start + done_in_b
                        self.session_records.append(formatted_single)

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

                batch_res = scraper.scrape_bulk(
                    batch,
                    max_workers=self.max_workers,
                    callback=on_progress
                )

                # Save session checkpoint
                with self.lock:
                    records_to_save = list(self.session_records)

                safe_save(records_to_save, OUTPUT_FILE, OUTPUT_CSV)

        except Exception as e:
            self.add_log(f"Error during scraping: {str(e)}", "error")
        finally:
            with self.lock:
                records_to_save = list(self.session_records)
                self.is_running = False
                self.current_url = ""

            safe_save(records_to_save, OUTPUT_FILE, OUTPUT_CSV)

            # Record run in persistent history with dedicated files
            if records_to_save:
                run_entry = add_run_to_history(
                    run_id,
                    source_name,
                    len(pending_urls),
                    self.success_count,
                    self.fail_count,
                    self.selected_fields,
                    records_to_save,
                    executed_by=getattr(self, "executed_by", "Bizprospex Member")
                )
                with self.lock:
                    self.last_completed_run = run_entry

            self.add_log(f"Scraping run completed! {len(records_to_save)} records saved cleanly in Run History.", "success")


manager = ScraperManager()


# ==============================
# ROUTES
# ==============================

@app.route("/")
def index():
    input_count = 0
    if os.path.exists(INPUT_FILE):
        try:
            df = pd.read_excel(INPUT_FILE)
            if "LinkedIn URL" in df.columns:
                input_count = df["LinkedIn URL"].dropna().count()
        except Exception:
            pass

    return render_template("index.html", input_file=INPUT_FILE, input_count=input_count)


@app.route("/api/status")
def api_status():
    return jsonify(manager.get_status())


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
    user_label = user.get("name") or user.get("email") or "Bizprospex Member"

    ok, msg = manager.start_scraping(
        urls,
        max_workers=workers,
        timeout=timeout,
        batch_size=batch_size,
        resume=False,
        selected_fields=selected_fields,
        is_manual=is_manual,
        executed_by=user_label
    )
    if ok:
        return jsonify({"success": True, "message": msg, "total": len(urls)})
    return jsonify({"success": False, "message": msg}), 400


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = manager.stop_scraping()
    return jsonify({"success": ok, "message": msg})


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
# RUN HISTORY API ROUTES
# ==============================

@app.route("/api/history")
def api_history():
    history = load_history()
    return jsonify({"history": history})


@app.route("/api/history/<run_id>/download/<file_type>")
def api_download_history_file(run_id, file_type):
    history = load_history()
    run = next((r for r in history if r.get("id") == run_id), None)
    if not run:
        return jsonify({"error": "Run not found"}), 404

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
    history = load_history()
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
    if os.path.exists(OUTPUT_FILE):
        return send_file(OUTPUT_FILE, as_attachment=True, download_name="Biz_LinkedIn_Companies.xlsx")
    return jsonify({"error": "No output file found"}), 404


@app.route("/api/download/csv")
def download_csv():
    if os.path.exists(OUTPUT_CSV):
        return send_file(OUTPUT_CSV, as_attachment=True, download_name="Biz_LinkedIn_Companies.csv")
    return jsonify({"error": "No CSV file found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print("  🚀 Biz Linkedin Scraper is starting!")
    print(f"  💻 Local Port:        http://localhost:{port}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False)
