# company-linkedin-info-extractor

Extract company information from LinkedIn pages without authentication. No API keys, no login required. 95%+ success rate using a 3-method fallback chain.

## How it works

```
LinkedIn URL → Method 1: Googlebot JSON-LD  (0.5-2s, ~65% success)
                    ↓ fail
               Method 2: Jina AI proxy      (1-3s, ~50% success)
                    ↓ fail
               Method 3: Playwright Stealth  (5-15s, ~90% success)
                    ↓
               Combined: ~95% success rate
```

Each method fails in different scenarios, so the chain catches what individual methods miss. If any method confirms a 404, the chain stops immediately.

## What you get

| Field | Example |
|-------|---------|
| `name` | Stripe |
| `description` | Stripe is a financial infrastructure platform... |
| `website` | stripe.com |
| `industry` | Financial Services |
| `company_size` | 1,001-5,000 employees |
| `employees_min` | 1001 |
| `employees_max` | 5000 |
| `headquarters` | San Francisco, California, US |
| `founded` | 2010 |
| `followers` | 1234567 |

## Installation

```bash
pip install company-linkedin-info-extractor
```

Or from source:

```bash
git clone https://github.com/shanvel-katesh/company-linkedin-info-extractor.git
cd company-linkedin-info-extractor
pip install -e .
```

**Optional** (improves success rate from ~70% to ~95%):

```bash
pip install playwright playwright-stealth
playwright install chromium
```

## Quick start

### Python

```python
from linkedin_extractor import scrape_company

info = scrape_company("https://www.linkedin.com/company/stripe")

print(info["name"])           # "Stripe"
print(info["industry"])       # "Financial Services"
print(info["employees_max"])  # 5000
print(info["website"])        # "stripe.com"
print(info["method_used"])    # "googlebot_jsonld"
```

### CLI

```bash
# Human-readable output
python -m linkedin_extractor.scraper "https://www.linkedin.com/company/stripe"

# JSON output
python -m linkedin_extractor.scraper "https://www.linkedin.com/company/stripe" --json
```

## Bulk scraping

```python
from linkedin_extractor import LinkedInScraper

scraper = LinkedInScraper(timeout=15)

results = scraper.scrape_bulk([
    "https://www.linkedin.com/company/stripe",
    "https://www.linkedin.com/company/hubspot",
    "https://www.linkedin.com/company/datadog",
], max_workers=3)

for r in results:
    if r["success"]:
        print(f"{r['name']:20} | {r['industry']:25} | {r['employees_min']}-{r['employees_max']}")
```

## Response format

```json
{
    "success": true,
    "linkedin_url": "https://www.linkedin.com/company/stripe",
    "name": "Stripe",
    "description": "Stripe is a financial infrastructure platform for businesses...",
    "website": "stripe.com",
    "industry": "Financial Services",
    "company_size": "1,001-5,000 employees",
    "employees_min": 1001,
    "employees_max": 5000,
    "headquarters": "San Francisco, California, US",
    "founded": "2010",
    "followers": "1234567",
    "method_used": "googlebot_jsonld"
}
```

## Methods explained

### Method 1: Googlebot JSON-LD (fastest)

Sends HTTP request with Googlebot user-agent. LinkedIn serves full structured data (JSON-LD) to known search engine crawlers for SEO indexing.

- Speed: 0.5-2 seconds
- Success rate: ~65%
- Fails when: LinkedIn blocks the IP or returns auth wall

### Method 2: Jina AI (free proxy)

Uses [Jina AI Reader](https://jina.ai/reader/) — a free web reader proxy that returns page content as markdown. Different infrastructure than Googlebot, so it fails at different times.

- Speed: 1-3 seconds
- Success rate: ~50%
- Fails when: Jina service is overloaded or LinkedIn blocks proxy IPs

### Method 3: Playwright Stealth (most reliable)

Full headless Chromium browser with anti-detection patches. Renders the page like a real user would.

- Speed: 5-15 seconds
- Success rate: ~90%
- Fails when: LinkedIn's anti-bot detection catches it (rare)
- **Optional dependency** — scraper works without it at ~70% success rate

## Smart features

- **Auth wall detection** — detects LinkedIn login/signup pages and rejects them
- **404 detection** — stops the chain immediately if page doesn't exist
- **Junk domain filtering** — strips google.com, bing.com, etc. from website field
- **Employee count parsing** — converts "1,001-5,000 employees" to `employees_min=1001, employees_max=5000`
- **URL normalization** — handles `/about`, `/jobs` suffixes and missing `https://`

## Comparison with paid tools

| Tool | Price | Method |
|------|-------|--------|
| Proxycurl | $0.01/profile | API |
| PhantomBuster | $69/month | Browser |
| LinkedIn Sales Nav | $80/month | Official |
| **This tool** | **$0** | **Googlebot + Jina + Playwright** |

## Rate limiting

LinkedIn may temporarily block IPs that make too many requests. For production use:

- Keep `max_workers` between 3-5
- Add delays between batches for 100+ companies
- Rotate IPs if doing 1000+ companies

## Limitations

- **No authentication required** — but LinkedIn can still block aggressive scraping
- **Company pages only** — does not scrape personal profiles
- **Public data only** — extracts what LinkedIn shows to search engines
- **Success rate depends on methods installed** — ~70% without Playwright, ~95% with it

## License

MIT
