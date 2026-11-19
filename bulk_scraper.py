import os
import sys
import time
import re
import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from linkedin_extractor import LinkedInScraper

# Reconfigure console to avoid Unicode crashes on Windows CMD / PowerShell
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

# ==============================
# SETTINGS
# ==============================

INPUT_FILE = "input.xlsx"
OUTPUT_FILE = "output.xlsx"
OUTPUT_CSV = "output.csv"

# Saves to Excel & CSV every BATCH_SIZE URLs (so progress is never lost)
BATCH_SIZE = 20

# Number of concurrent scraper threads (3-5 recommended)
MAX_WORKERS = 3

# Timeout per request in seconds
TIMEOUT = 15

# Automatically resume from already completed URLs in output.xlsx / output.csv
RESUME = True


# ==============================
# HELPER FUNCTIONS
# ==============================

def clean_for_excel(val):
    """
    Remove illegal XML/Excel characters (such as \x0b vertical tab, \x00-\x08 control characters)
    that cause openpyxl.utils.exceptions.IllegalCharacterError.
    """
    if isinstance(val, str):
        val = ILLEGAL_CHARACTERS_RE.sub("", val)
        val = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", val)
    return val


def format_row(r: dict) -> dict:
    """Format and sanitize scraper result dictionary into clean row."""
    row = {
        "LinkedIn URL": r.get("linkedin_url", ""),
        "Company Name": r.get("name", ""),
        "About / Overview": r.get("description", ""),
        "Description": r.get("description", ""),
        "Website": r.get("website", ""),
        "Industry": r.get("industry", ""),
        "Company Size": r.get("company_size", ""),
        "Employee Count": r.get("employee_count", ""),
        "Employees Min": r.get("employees_min", ""),
        "Employees Max": r.get("employees_max", ""),
        "Headquarters": r.get("headquarters", ""),
        "Founded": r.get("founded", ""),
        "Followers": r.get("followers", ""),
        "Method Used": r.get("method_used", ""),
        "Success": r.get("success", False)
    }
    return {k: clean_for_excel(v) for k, v in row.items()}


def safe_save(results_list, excel_path, csv_path):
    """
    Safely saves results to Excel and CSV.
    If output.xlsx is locked by Excel, writes to a backup file so data is NEVER lost.
    """
    if not results_list:
        return

    df = pd.DataFrame(results_list)

    # Sanitize all string columns to guarantee openpyxl will never crash
    for col in df.columns:
        df[col] = df[col].apply(clean_for_excel)

    # 1. Always save CSV first (failsafe backup, cannot be corrupted by XML issues)
    try:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"[Warning] CSV save error: {e}")

    # 2. Save Excel
    try:
        df.to_excel(excel_path, index=False, engine="openpyxl")
    except PermissionError:
        backup_name = f"output_autosave_{int(time.time())}.xlsx"
        print(f"\n[WARNING] '{excel_path}' is open in Excel! Saved checkpoint to '{backup_name}'.")
        print("Please close output.xlsx in Excel so it can be updated automatically.\n")
        try:
            df.to_excel(backup_name, index=False, engine="openpyxl")
        except Exception:
            pass
    except Exception as e:
        print(f"[Warning] Excel save error: {e}")


# ==============================
# MAIN EXECUTION
# ==============================

def main():
    print("=" * 60)
    print(" LinkedIn Company Scraper (Auto-Save & Resume Enabled)")
    print("=" * 60)

    # Read input Excel
    if not os.path.exists(INPUT_FILE):
        print(f"Error: {INPUT_FILE} not found!")
        return

    df_in = pd.read_excel(INPUT_FILE)
    if "LinkedIn URL" not in df_in.columns:
        print(f"Error: 'LinkedIn URL' column not found in {INPUT_FILE}!")
        return

    all_urls = (
        df_in["LinkedIn URL"]
        .dropna()
        .astype(str)
        .str.strip()
        .tolist()
    )

    total_input = len(all_urls)
    print(f"Found {total_input} LinkedIn URLs in {INPUT_FILE}")

    # Resume check: load existing completed records if any
    saved_records = []
    completed_urls = set()

    if RESUME:
        source_df = None
        if os.path.exists(OUTPUT_FILE):
            try:
                temp_df = pd.read_excel(OUTPUT_FILE)
                if "Success" in temp_df.columns and "LinkedIn URL" in temp_df.columns:
                    valid = temp_df[temp_df["Success"].notna() & (temp_df["Success"] != "")]
                    if len(valid) > 0:
                        source_df = valid
            except Exception:
                pass

        if source_df is None and os.path.exists(OUTPUT_CSV):
            try:
                temp_df = pd.read_csv(OUTPUT_CSV)
                if "Success" in temp_df.columns and "LinkedIn URL" in temp_df.columns:
                    valid = temp_df[temp_df["Success"].notna() & (temp_df["Success"] != "")]
                    if len(valid) > 0:
                        source_df = valid
            except Exception:
                pass

        if source_df is not None:
            saved_records = source_df.to_dict(orient="records")
            completed_urls = {
                str(u).strip().rstrip("/")
                for u in source_df["LinkedIn URL"].dropna()
            }
            print(f"Resuming: Found {len(completed_urls)} previously completed URLs. Skipping them.")

    # Filter out already scraped URLs
    pending_urls = [u for u in all_urls if u.rstrip("/") not in completed_urls]
    print(f"Remaining URLs to scrape: {len(pending_urls)}\n")

    if not pending_urls:
        print("All URLs are already completed!")
        return

    scraper = LinkedInScraper(timeout=TIMEOUT)
    total_completed = len(saved_records)

    try:
        # Process in batches
        for batch_start in range(0, len(pending_urls), BATCH_SIZE):
            batch = pending_urls[batch_start:batch_start + BATCH_SIZE]

            def on_progress(done_in_batch, total_in_batch, result):
                nonlocal total_completed
                url = result.get("linkedin_url", "")
                success = result.get("success", False)
                name = result.get("name", "")
                method = result.get("method_used", "")

                status = "SUCCESS" if success else "FAILED"
                if success:
                    detail = f"{name} | Method: {method}" if name else f"Found data | Method: {method}"
                else:
                    err = result.get("error", "No data / Protected")
                    detail = f"Reason: {err}"

                current_total_done = total_completed + done_in_batch
                percent = (current_total_done / total_input) * 100
                try:
                    print(f"[{current_total_done}/{total_input}] ({percent:.1f}%) [{status}] {url} -> {detail}", flush=True)
                except Exception:
                    pass

            batch_results = scraper.scrape_bulk(
                batch,
                max_workers=MAX_WORKERS,
                callback=on_progress
            )

            # Process and append results
            for r in batch_results:
                saved_records.append(format_row(r))

            total_completed = len(saved_records)

            # Checkpoint save after every batch
            safe_save(saved_records, OUTPUT_FILE, OUTPUT_CSV)
            print(f"--> [CHECKPOINT SAVED] {total_completed}/{total_input} records saved to {OUTPUT_FILE} and {OUTPUT_CSV}\n", flush=True)

    except KeyboardInterrupt:
        print("\n\n[PAUSED] Scraping stopped by user (Ctrl+C).")
    except Exception as e:
        print(f"\n\n[ERROR] An unexpected error occurred: {e}")
    finally:
        # Final safe save guaranteed
        print("\nSaving final results to disk...")
        safe_save(saved_records, OUTPUT_FILE, OUTPUT_CSV)
        print("================================")
        print("SCRAPING SESSION COMPLETE")
        print("================================")
        print(f"Total processed: {len(saved_records)} / {total_input}")
        print(f"Excel output   : {OUTPUT_FILE}")
        print(f"CSV backup     : {OUTPUT_CSV}")
        print("You can run this script again anytime to resume remaining URLs.")


if __name__ == "__main__":
    main()
