"""Example: Scrape a single LinkedIn company page."""

from linkedin_extractor import scrape_company

info = scrape_company("https://www.linkedin.com/company/stripe")

if info["success"]:
    print(f"Name:         {info['name']}")
    print(f"Industry:     {info['industry']}")
    print(f"Size:         {info['company_size']}")
    print(f"Employees:    {info['employees_min']}-{info['employees_max']}")
    print(f"Website:      {info['website']}")
    print(f"Headquarters: {info['headquarters']}")
    print(f"Founded:      {info['founded']}")
    print(f"Method:       {info['method_used']}")
else:
    print(f"Failed: {info['error']}")
