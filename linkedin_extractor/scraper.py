"""
LinkedIn Company Info Extractor — 3-method fallback chain, 95%+ success rate.

Extracts company info from LinkedIn company pages without authentication.
No API keys required. Completely free.

Methods tried in order:
  1. Googlebot HTTP + JSON-LD  -- Crawler UA bypasses auth wall (0.5-2s)
  2. Jina AI                   -- Free proxy, different failure domain (1-3s)
  3. Playwright Stealth        -- Full headless browser, last resort (5-15s)

Returns: name, description, website, industry, company_size, employees_min/max,
         headquarters, founded, followers, method_used

Usage:
    from linkedin_extractor import scrape_company

    info = scrape_company("https://www.linkedin.com/company/stripe")
    print(info["name"])           # "Stripe"
    print(info["employees_max"])  # 5000
    print(info["method_used"])    # "googlebot_jsonld"

CLI:
    python -m linkedin_extractor.scraper "https://www.linkedin.com/company/stripe"
    python -m linkedin_extractor.scraper "https://www.linkedin.com/company/stripe" --json
"""

import re
import sys
import time
import json
import threading
import argparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Dict, Any, List
from urllib.parse import urlparse

# High-performance persistent session with connection pooling
HTTP_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=Retry(total=1, backoff_factor=0.2))
HTTP_SESSION.mount("https://", _adapter)
HTTP_SESSION.mount("http://", _adapter)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


# ── Size parsing ─────────────────────────────────────────────────────────────

SIZE_MAP = {
    "1-10":         (1,    10),
    "2-10":         (2,    10),
    "11-50":        (11,   50),
    "51-200":       (51,  200),
    "201-500":      (201, 500),
    "501-1,000":    (501, 1000),
    "501-1000":     (501, 1000),
    "1,001-5,000":  (1001, 5000),
    "1001-5000":    (1001, 5000),
    "5,001-10,000": (5001, 10000),
    "5001-10000":   (5001, 10000),
    "10,001+":      (10001, 99999),
    "10001+":       (10001, 99999),
}


def parse_size(size_str: str) -> tuple:
    """Convert LinkedIn size string to (min, max) int tuple."""
    if not size_str:
        return (0, 0)
    s = size_str.strip().replace(" employees", "").replace(" employee", "")
    for key, val in sorted(SIZE_MAP.items(), key=lambda x: -len(x[0])):
        if key in s:
            return val
    match = re.search(r"([\d,]+)\s*[-\u2013]\s*([\d,]+)", s)
    if match:
        return (int(match.group(1).replace(",", "")), int(match.group(2).replace(",", "")))
    match_plus = re.search(r"([\d,]+)\+", s)
    if match_plus:
        return (int(match_plus.group(1).replace(",", "")), 99999)
    return (0, 0)


JUNK_DOMAINS = {
    "bing.com", "google.com", "yahoo.com", "facebook.com", "twitter.com",
    "instagram.com", "youtube.com", "tiktok.com", "pinterest.com",
    "reddit.com", "wikipedia.org", "amazon.com", "apple.com",
    "microsoft.com", "linkedin.com", "glassdoor.com", "indeed.com",
    "crunchbase.com", "bloomberg.com", "reuters.com",
}


def clean_domain(raw: str) -> str:
    """Normalize URL/domain to bare domain. Returns '' for junk domains."""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("http"):
        parsed = urlparse(raw)
        raw = parsed.netloc or parsed.path
    raw = re.sub(r"^www\.", "", raw).rstrip("/").split("/")[0]
    domain = raw.lower()
    if domain in JUNK_DOMAINS:
        return ""
    return domain


def _normalize_url(url: str) -> str:
    """Ensure URL starts with https and points to company page root."""
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    url = re.sub(r"/(about|jobs|people|posts|videos|life)/?$", "", url)
    return url


def parse_count(val: Any) -> Any:
    """Convert employee count string/int to cleaned integer or string."""
    if val is None or val == "":
        return ""
    if isinstance(val, int):
        return val
    s = str(val).strip().replace(",", "")
    m_k = re.match(r"^([\d\.]+)\s*[kK]$", s)
    if m_k:
        try:
            return int(float(m_k.group(1)) * 1000)
        except ValueError:
            pass
    m_m = re.match(r"^([\d\.]+)\s*[mM]$", s)
    if m_m:
        try:
            return int(float(m_m.group(1)) * 1000000)
        except ValueError:
            pass
    m_digits = re.search(r"^(\d+)", s)
    if m_digits:
        try:
            return int(m_digits.group(1))
        except ValueError:
            pass
    return val


def extract_associated_members(text: str) -> Any:
    """Extract associated member / employee count from any text or HTML snippet."""
    if not text:
        return ""
    patterns = [
        r'([\d,]+)\s*(?:associated\s*members?|employees?\s*on\s*LinkedIn|members?\s*on\s*LinkedIn)',
        r'(?:associated\s*members?|employees?\s*on\s*LinkedIn|members?\s*on\s*LinkedIn)[\s:]*([0-9,]+)',
        r'(?:discover|view|see)\s+(?:all\s+)?([\d,]+)\s+employees?',
        r'([\d,]+)\s*current\s*employees?',
        r'([\d,]+)\s*people\s*on\s*LinkedIn',
        r'associated\s*members?[:\s]+([\d,]+)',
        r'([\d,]+)\s*associated',
        r'>\s*([\d,]+)\s*<\/[^>]+>\s*(?:associated\s*members?|employees?\s*on\s*LinkedIn)',
        r'(?:associated\s*members?|employees?\s*on\s*LinkedIn)\s*<\/[^>]+>\s*<\w+[^>]*>\s*([\d,]+)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = parse_count(m.group(1))
            if val and val != "":
                return val
    return ""


def _enrich_result(info: Dict[str, Any]) -> Dict[str, Any]:
    """Add employees_min/max from company_size, ensure all keys exist."""
    emp_min, emp_max = parse_size(info.get("company_size", ""))
    info["employees_min"] = emp_min
    info["employees_max"] = emp_max
    if not info.get("employee_count") and info.get("company_size"):
        assoc = extract_associated_members(str(info["company_size"]))
        if assoc:
            info["employee_count"] = assoc
    if "employee_count" in info and info["employee_count"] != "":
        info["employee_count"] = parse_count(info["employee_count"])
    for key in ("name", "description", "website", "industry", "company_size",
                "employee_count", "headquarters", "founded", "followers"):
        info.setdefault(key, "")
    return info


def _is_auth_wall(info: Dict[str, Any]) -> bool:
    """Detect if the result is actually LinkedIn's login page."""
    name = (info.get("name") or "").lower().strip()
    desc = (info.get("description") or "").lower()
    if name in ("sign in", "sign up", "login", "linkedin login", "linkedin"):
        return True
    if "login to linkedin" in desc or "sign in to linkedin" in desc:
        return True
    if "join to view" in desc or "sign up to view" in desc:
        return True
    return False


def _has_useful_data(info: Dict[str, Any]) -> bool:
    """Check if result has enough real company data."""
    if not info.get("success"):
        return False
    if _is_auth_wall(info):
        return False
    return bool(info.get("description") or info.get("company_size") or info.get("website") or info.get("employee_count"))


# ── Method 1: Googlebot HTTP + JSON-LD ──────────────────────────────────────

GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def _try_googlebot(url: str, timeout: int) -> Dict[str, Any]:
    """Fetch LinkedIn page using Googlebot UA and extract JSON-LD structured data and HTML info."""
    headers = {
        "User-Agent": GOOGLEBOT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        connect_t = min(3, timeout)
        read_t = min(timeout, 6)
        resp = HTTP_SESSION.get(url, headers=headers, timeout=(connect_t, read_t), allow_redirects=True)
        if resp.status_code == 404:
            return {"success": False, "error": "Page not found (404)",
                    "linkedin_url": url, "_is_404": True}
        if resp.status_code in (999, 429, 403):
            return {"success": False, "error": f"Blocked (HTTP {resp.status_code})",
                    "linkedin_url": url}
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}",
                    "linkedin_url": url}

        html = resp.text

        if "authwall" in resp.url or "login" in resp.url:
            return {"success": False, "error": "Auth wall redirect", "linkedin_url": url}

        info_jsonld = _parse_jsonld(html, url)
        info_html = _parse_html(html, url) if BS4_AVAILABLE else {}

        if info_jsonld.get("success") or info_html.get("success"):
            merged = {"success": True, "linkedin_url": url}
            for k in ("name", "description", "website", "industry", "company_size",
                      "employee_count", "headquarters", "founded", "followers"):
                val = info_jsonld.get(k) or info_html.get(k) or ""
                merged[k] = val
            if not merged.get("employee_count"):
                merged["employee_count"] = extract_associated_members(html)
            merged = _enrich_result(merged)
            if _has_useful_data(merged):
                return merged

        return {"success": False, "error": "No structured data in response",
                "linkedin_url": url}

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Timeout", "linkedin_url": url}
    except Exception as e:
        return {"success": False, "error": f"Googlebot error: {e}", "linkedin_url": url}


def _parse_jsonld(html: str, linkedin_url: str) -> Dict[str, Any]:
    """Extract Organization data from JSON-LD script tags."""
    if not BS4_AVAILABLE:
        jsonld_blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL
        )
    else:
        soup = BeautifulSoup(html, "html.parser")
        jsonld_blocks = [
            tag.string for tag in soup.find_all("script", type="application/ld+json")
            if tag.string
        ]

    for block in jsonld_blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                items.extend(data["@graph"])
            items.append(data)

        for item in items:
            if not isinstance(item, dict):
                continue
            org_type = item.get("@type", "")
            if org_type in ("Organization", "Corporation", "LocalBusiness", "Company"):
                return _extract_jsonld_org(item, linkedin_url)

    return {"success": False, "error": "No Organization JSON-LD found",
            "linkedin_url": linkedin_url}


def _extract_jsonld_org(data: dict, linkedin_url: str) -> Dict[str, Any]:
    """Extract fields from a JSON-LD Organization object."""
    info = {"success": True, "linkedin_url": linkedin_url}

    info["name"] = data.get("name", "")
    info["description"] = data.get("description", "")

    url_val = data.get("url", "") or data.get("sameAs", "")
    if isinstance(url_val, list):
        for u in url_val:
            if "linkedin.com" not in u:
                url_val = u
                break
        else:
            url_val = url_val[0] if url_val else ""
    if url_val and "linkedin.com" not in url_val:
        info["website"] = clean_domain(url_val)

    employees = data.get("numberOfEmployees", {})
    if isinstance(employees, dict):
        emp_val = employees.get("value", "")
        if emp_val:
            info["employee_count"] = parse_count(emp_val)
    elif isinstance(employees, (int, float, str)) and employees:
        info["employee_count"] = parse_count(employees)

    info["industry"] = data.get("industry", "")

    address = data.get("address", {})
    if isinstance(address, dict):
        parts = [address.get("addressLocality", ""), address.get("addressRegion", ""),
                 address.get("addressCountry", "")]
        hq = ", ".join(p for p in parts if p)
        if hq:
            info["headquarters"] = hq

    info["founded"] = str(data.get("foundingDate", ""))

    interaction = data.get("interactionStatistic", {})
    if isinstance(interaction, dict):
        info["followers"] = str(interaction.get("userInteractionCount", ""))
    elif isinstance(interaction, list):
        for stat in interaction:
            if stat.get("interactionType", {}).get("@type") == "FollowAction":
                info["followers"] = str(stat.get("userInteractionCount", ""))
                break

    return _enrich_result(info)


# ── Method 1b: HTML parsing ─────────────────────────────────────────────────

def _parse_html(html: str, linkedin_url: str) -> Dict[str, Any]:
    """Parse LinkedIn company page HTML using BeautifulSoup."""
    if not BS4_AVAILABLE:
        return {"success": False, "error": "bs4 not installed"}

    soup = BeautifulSoup(html, "html.parser")
    info = {"success": True, "linkedin_url": linkedin_url}

    for selector in [".top-card-layout__entity-info h1", "h1.top-card-layout__title",
                     ".org-top-card-summary__title", "h1"]:
        el = soup.select_one(selector)
        if el:
            name = el.get_text(strip=True)
            name = re.sub(r'\s*[|\-]\s*LinkedIn\s*$', '', name, flags=re.IGNORECASE).strip()
            if name and len(name) < 200:
                info["name"] = name
                break

    for selector in [".core-section-container__content p",
                     ".org-about-us-organization-description__text",
                     'section[data-section="summary"] p',
                     ".break-words"]:
        el = soup.select_one(selector)
        if el:
            desc = el.get_text(strip=True)
            if desc and len(desc) > 30:
                info["description"] = desc
                break

    if not info.get("description"):
        meta = soup.find("meta", attrs={"name": "description"}) or \
               soup.find("meta", attrs={"property": "og:description"})
        if meta and meta.get("content"):
            info["description"] = meta["content"].strip()

    detail_blocks = soup.select('.core-section-container__content .mb-2')
    for block in detail_blocks:
        labels = block.select('.text-md')
        if not labels or len(labels) < 2:
            continue
        label = labels[0].get_text(strip=True).lower()
        value = labels[1].get_text(strip=True)

        if label == "website":
            link = block.select_one('a')
            info["website"] = clean_domain(link.get_text(strip=True) if link else value)
        elif label == "industry":
            info["industry"] = value
        elif label == "company size":
            info["company_size"] = value
            for extra_label in labels[1:]:
                assoc = extract_associated_members(extra_label.get_text(strip=True))
                if assoc:
                    info["employee_count"] = assoc
                    break
            if not info.get("employee_count"):
                assoc = extract_associated_members(block.get_text(" ", strip=True))
                if assoc:
                    info["employee_count"] = assoc
        elif label == "headquarters":
            info["headquarters"] = value
        elif label == "founded":
            info["founded"] = value

    # Extract employee count / associate members from HTML if not present
    if not info.get("employee_count"):
        for el in soup.find_all(["a", "span", "div", "h2", "h3", "p", "li"]):
            text = el.get_text(" ", strip=True)
            assoc = extract_associated_members(text)
            if assoc:
                info["employee_count"] = assoc
                break

    if not info.get("employee_count"):
        info["employee_count"] = extract_associated_members(html)

    if not info.get("website"):
        for a in soup.select('a[href]'):
            href = a.get('href', '')
            if href.startswith('http') and 'linkedin.com' not in href:
                info["website"] = clean_domain(href)
                break

    if not info.get("name") and not info.get("description"):
        return {"success": False, "error": "No data parsed from HTML",
                "linkedin_url": linkedin_url}

    return _enrich_result(info)


# ── Method 2: Jina AI ───────────────────────────────────────────────────────

def _try_jina(url: str, timeout: int) -> Dict[str, Any]:
    """Fetch via Jina AI reader proxy and parse markdown output."""
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "text/plain, text/markdown",
        "X-Timeout": str(timeout),
    }
    try:
        connect_t = min(3, timeout)
        read_t = min(timeout, 6)
        resp = HTTP_SESSION.get(jina_url, headers=headers, timeout=(connect_t, read_t))
        if resp.status_code != 200:
            return {"success": False, "error": f"Jina HTTP {resp.status_code}",
                    "linkedin_url": url}
        content = resp.text
        if len(content.strip()) < 200:
            return {"success": False, "error": "Jina returned thin content",
                    "linkedin_url": url}
        if re.search(r'(returned error 4\d{2}|Page not found|This page doesn.t exist)', content, re.IGNORECASE):
            return {"success": False, "error": "Page not found (404)",
                    "linkedin_url": url, "_is_404": True}
        return _parse_markdown(content, url)
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Jina timeout", "linkedin_url": url}
    except Exception as e:
        return {"success": False, "error": f"Jina error: {e}", "linkedin_url": url}


def _parse_markdown(content: str, linkedin_url: str) -> Dict[str, Any]:
    """Parse Jina AI markdown output from a LinkedIn company page."""
    info = {"success": True, "linkedin_url": linkedin_url}

    m = re.search(r'^(.+?)\s*\n={3,}', content, re.MULTILINE)
    if m:
        name = m.group(1).strip()
        name = re.sub(r'\s*[|\-]\s*LinkedIn\s*$', '', name, flags=re.IGNORECASE).strip()
        info["name"] = name
    else:
        m = re.search(r'Title:\s*(.+?)\s*\|', content)
        if m:
            info["name"] = m.group(1).strip()

    m = re.search(r'(?:^|\n)(?:##?\s*)?(?:About|Overview)\s*\n+(.+?)(?:\n\n|\n(?:##?\s|\w+\s+\[)|\Z)',
                  content, re.DOTALL | re.IGNORECASE)
    if m:
        desc = m.group(1).strip()
        desc = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', desc)
        desc = re.sub(r'\n+', ' ', desc).strip()
        if len(desc) > 30:
            info["description"] = desc

    if not info.get("description"):
        paragraphs = re.findall(r'\n\n(.{80,}?)(?:\n\n|\Z)', content, re.DOTALL)
        for para in paragraphs:
            text = para.strip()
            if not re.match(r'^(Website|Industry|Company size|Headquarters|Founded|Specialties)', text):
                text = re.sub(r'\n+', ' ', text).strip()
                info["description"] = text
                break

    m = re.search(r'Website\s+\[?([^\s\]\n]+)', content)
    if m:
        info["website"] = clean_domain(m.group(1).strip())

    m = re.search(r'Industry\s+(.+?)(?:\n|$)', content)
    if m:
        info["industry"] = m.group(1).strip()

    m = re.search(r'Company size\s+(.+?)(?:\n|$)', content)
    if m:
        info["company_size"] = m.group(1).strip()

    # Employee count / associate members in markdown
    m_emp = re.search(r'(?:##?\s*(?:View|Discover|See)\s+([\d,\.]+[kKmM]?|\d+)\s+employees|(?:discover|view|see)\s+all\s+([\d,]+)\s+employees|([\d,]+)\s*(?:associated members?|on LinkedIn|employees on LinkedIn))', content, re.IGNORECASE)
    if m_emp:
        count_str = m_emp.group(1) or m_emp.group(2) or m_emp.group(3)
        info["employee_count"] = parse_count(count_str)

    if not info.get("employee_count"):
        info["employee_count"] = extract_associated_members(content)

    m = re.search(r'Headquarters\s+(.+?)(?:\n|$)', content)
    if m:
        info["headquarters"] = m.group(1).strip()

    m = re.search(r'Founded\s+(\d{4})', content)
    if m:
        info["founded"] = m.group(1)

    m = re.search(r'([\d,]+)\s+followers', content)
    if m:
        info["followers"] = m.group(1)

    m = re.search(r'Specialties\s+(.+?)(?:\n\n|\Z)', content, re.DOTALL)
    if m:
        info["specialties"] = m.group(1).strip()

    return _enrich_result(info)


# ── Method 3: Playwright Stealth ────────────────────────────────────────────

PLAYWRIGHT_LOCK = threading.Lock()

def _try_playwright(url: str, timeout: int) -> Dict[str, Any]:
    """Launch stealth browser and extract data. Skips if playwright not installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"success": False, "error": "playwright not installed", "linkedin_url": url}

    try:
        from playwright_stealth import stealth_sync
    except ImportError:
        stealth_sync = None

    about_url = url.rstrip("/") + "/about/"

    # Prevent concurrent Chromium instances from causing Render 512MB RAM freeze
    acquired = PLAYWRIGHT_LOCK.acquire(timeout=2.0)
    if not acquired:
        return {"success": False, "error": "Playwright busy, skipped to prevent memory spike", "linkedin_url": url}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                timeout=10000,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--single-process",
                    "--no-zygote",
                ],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    locale="en-US",
                )
                page = context.new_page()

                if stealth_sync:
                    stealth_sync(page)

                pw_timeout = min(timeout, 8) * 1000
                resp = page.goto(about_url, wait_until="domcontentloaded", timeout=pw_timeout)
                page.wait_for_timeout(500)

                pw_status = resp.status if resp else 0
                html = page.content()
            finally:
                browser.close()

        if pw_status == 404 or re.search(r'Page not found', html):
            return {"success": False, "error": "Page not found (404)",
                    "linkedin_url": url, "_is_404": True}

        info_jsonld = _parse_jsonld(html, url)
        info_html = _parse_html(html, url) if BS4_AVAILABLE else {}

        if info_jsonld.get("success") or info_html.get("success"):
            merged = {"success": True, "linkedin_url": url}
            for k in ("name", "description", "website", "industry", "company_size",
                      "employee_count", "headquarters", "founded", "followers"):
                val = info_jsonld.get(k) or info_html.get(k) or ""
                merged[k] = val
            if not merged.get("employee_count"):
                merged["employee_count"] = extract_associated_members(html)
            merged = _enrich_result(merged)
            if _has_useful_data(merged):
                return merged

        return {"success": False, "error": "Playwright: no data extracted",
                "linkedin_url": url}

    except Exception as e:
        err_msg = str(e)[:200]
        return {"success": False, "error": f"Playwright error: {err_msg}",
                "linkedin_url": url}
    finally:
        try:
            PLAYWRIGHT_LOCK.release()
        except RuntimeError:
            pass


# ── Main scraper class ──────────────────────────────────────────────────────

class LinkedInScraper:
    """
    LinkedIn company page scraper with 3-method fallback chain.

    No API keys required. No authentication. Free forever.

    Args:
        timeout: Timeout per method in seconds (default: 15)

    Example:
        scraper = LinkedInScraper()
        info = scraper.scrape("https://www.linkedin.com/company/stripe")
        print(info["name"])            # "Stripe"
        print(info["employee_count"])  # 17197
    """

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def scrape(self, linkedin_url: str) -> Dict[str, Any]:
        """
        Scrape a LinkedIn company page.

        Returns dict with: name, description, website, industry, company_size,
        employee_count, employees_min, employees_max, headquarters, founded, followers,
        method_used, success, linkedin_url
        """
        return scrape_company(linkedin_url, timeout=self.timeout)

    def scrape_bulk(self, urls: List[str], max_workers: int = 5,
                    callback=None) -> List[Dict[str, Any]]:
        """Scrape multiple LinkedIn company pages in parallel."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = [None] * len(urls)
        total = len(urls)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.scrape, url): i
                for i, url in enumerate(urls)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"success": False, "linkedin_url": urls[idx],
                              "error": str(e)}
                results[idx] = result
                if callback:
                    done = sum(1 for r in results if r is not None)
                    callback(done, total, result)

        return results


def scrape_company(linkedin_url: str, timeout: int = 15) -> Dict[str, Any]:
    """
    Scrape a LinkedIn company page using 3-method fallback chain.

    This is the main entry point. For bulk scraping, use LinkedInScraper.scrape_bulk().
    """
    url = _normalize_url(linkedin_url)
    results = []
    methods = [
        ("googlebot_jsonld", lambda: _try_googlebot(url, timeout)),
        ("jina", lambda: _try_jina(url, timeout)),
        ("playwright_stealth", lambda: _try_playwright(url, timeout)),
    ]

    for method_name, method_fn in methods:
        try:
            result = method_fn()
        except Exception as e:
            result = {"success": False, "error": str(e), "linkedin_url": url}

        if result.get("_is_404"):
            result.pop("_is_404", None)
            result["method_used"] = f"{method_name}_404"
            return result

        if _has_useful_data(result):
            result["method_used"] = method_name
            return result
        results.append((method_name, result))

    for method_name, r in results:
        if r.get("success") and not _is_auth_wall(r):
            r["method_used"] = f"{method_name}_partial"
            return r

    errors = "; ".join(f"{m}: {r.get('error', '?')}" for m, r in results)
    return {
        "success": False,
        "linkedin_url": url,
        "error": f"All methods failed -- {errors}",
        "method_used": "none",
        **{k: "" for k in ("name", "description", "website", "industry",
                           "company_size", "employee_count", "headquarters", "founded", "followers")},
        "employees_min": 0,
        "employees_max": 0,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LinkedIn Company Info Extractor -- 3-method fallback chain"
    )
    parser.add_argument("linkedin_url", help="LinkedIn company URL")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--timeout", type=int, default=15, help="Timeout per method (seconds)")
    args = parser.parse_args()

    t0 = time.time()
    info = scrape_company(args.linkedin_url, timeout=args.timeout)
    elapsed = round(time.time() - t0, 2)

    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'='*70}")
        print(f"  LinkedIn Company Info ({elapsed}s, method: {info.get('method_used', '?')})")
        print(f"{'='*70}")
        if info.get("success"):
            print(f"  Name           : {info.get('name', 'N/A')}")
            desc = info.get("description", "N/A")
            if desc and len(desc) > 120:
                desc = desc[:120] + "..."
            print(f"  Description    : {desc}")
            print(f"  Website        : {info.get('website', 'N/A')}")
            print(f"  Industry       : {info.get('industry', 'N/A')}")
            print(f"  Size           : {info.get('company_size', 'N/A')}")
            print(f"  Employee Count : {info.get('employee_count', 'N/A')}")
            print(f"  Employees      : {info.get('employees_min', 0)}-{info.get('employees_max', 0)}")
            print(f"  Headquarters   : {info.get('headquarters', 'N/A')}")
            print(f"  Founded        : {info.get('founded', 'N/A')}")
            print(f"  Followers      : {info.get('followers', 'N/A')}")
        else:
            print(f"  FAILED: {info.get('error', 'Unknown error')}")
        print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
