"""Example: Scrape multiple LinkedIn company pages in parallel."""

from linkedin_extractor import LinkedInScraper

urls = [
    "https://www.linkedin.com/company/stripe",
    "https://www.linkedin.com/company/hubspot",
    "https://www.linkedin.com/company/datadog",
]

scraper = LinkedInScraper(timeout=15)
results = scraper.scrape_bulk(urls, max_workers=3)

print(f"{'Company':20} {'Industry':25} {'Employees':15} {'Method'}")
print("-" * 80)
for r in results:
    if r["success"]:
        emp = f"{r['employees_min']}-{r['employees_max']}"
        print(f"{r['name']:20} {r.get('industry', 'N/A'):25} {emp:15} {r['method_used']}")
    else:
        print(f"{'FAILED':20} {r.get('error', '?')[:55]}")
