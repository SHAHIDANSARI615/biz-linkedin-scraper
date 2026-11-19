from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="company-linkedin-info-extractor",
    version="0.1.0",
    description="Extract company info from LinkedIn pages without authentication. Free, no API keys.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Shan Venkat",
    url="https://github.com/shanvel-katesh/company-linkedin-info-extractor",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "requests>=2.25.0",
        "beautifulsoup4>=4.9.0",
    ],
    extras_require={
        "full": [
            "playwright>=1.20.0",
            "playwright-stealth>=1.0.0",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Internet :: WWW/HTTP :: Indexing/Search",
    ],
    keywords="linkedin scraper company info extractor b2b",
    project_urls={
        "Bug Reports": "https://github.com/shanvel-katesh/company-linkedin-info-extractor/issues",
        "Source": "https://github.com/shanvel-katesh/company-linkedin-info-extractor",
    },
)
