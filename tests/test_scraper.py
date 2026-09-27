"""Tests for scraper.py - parsers, extraction and end-to-end runs on local fixture sites.

    pytest -q                 # all tests (browser test skipped if Chromium is unavailable)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_server  # noqa: E402
import scraper  # noqa: E402
from scraper import Category  # noqa: E402

EXPECTED = [
    ("Riverside Light Rail Extension", 2016), ("Harbor Bridge Retrofit", 2017),
    ("Northgate Water Treatment Plant", 2018), ("Solar Schools Initiative", 2019),
    ("Downtown Transit Hub", 2020), ("Coastal Flood Barrier", 2021),
    ("Rural Broadband Expansion", 2022), ("Central Library Renovation", 2023),
    ("EV Charging Corridor", 2024),
]


@pytest.fixture(scope="module")
def site():
    server, base = fixture_server.start()
    yield base
    server.shutdown()


# --- value parsing ---------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$170,000 in funding", (170000.0, "USD")),
    ("Budget €1.2 million", (1200000.0, "EUR")),
    ("£3bn", (3e9, "GBP")),
    ("USD 5,000", (5000.0, "USD")),
    ("2.5 million dollars", (2500000.0, "USD")),
    ("in 2019 and 2020", None),
    ("21.1 million people", None),
])
def test_parse_money(text, expected):
    assert scraper.parse_money(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("in 2019 and 2020", 2019), ("FY2021", 2021), ("$2,019 raised", None),
    ("$2019 raised", None), ("Awarded in 2016. Total grant: $412.5 million.", 2016), ("no year", None),
])
def test_parse_year(text, expected):
    assert scraper.parse_year(text) == expected


def test_header_multiplier():
    assert scraper.header_multiplier("Budget (US$ millions)") == 1e6
    assert scraper.header_multiplier("Cost ($bn)") == 1e9
    assert scraper.header_multiplier("Amount") == 1.0


def test_category_validation():
    with pytest.raises(ValueError):
        Category("X", "colour")
    with pytest.raises(Exception):
        Category("X", "text", pattern="(unclosed")
    assert scraper.parse_category_spec("Funder:text:from (.+)").pattern == "from (.+)"


# --- extraction ------------------------------------------------------------

def test_list_items_with_custom_regex_category():
    html = """<ul>
      <li>Project Alpha<br><small>With $10,000 in funding from the Acme Fund in 2019</small></li>
      <li>Project Beta<br><small>With $2.5 million in funding from Globex in 2021</small></li>
    </ul><div class="promo">Since 2015 we have partnered with Initech in 2016</div>"""
    cats = scraper.load_categories(None) + [Category("Funder", "text", pattern=r"from (?:the )?(.+?) in \d{4}")]
    recs = scraper.extract_records(html, cats, "http://x")
    assert [(r["Project Name"], r["Year"], r["Monetary Amount"], r["Funder"]) for r in recs] == [
        ("Project Alpha", 2019, 10000.0, "Acme Fund"),
        ("Project Beta", 2021, 2500000.0, "Globex"),
    ]


def test_label_value_cards():
    html = """<div class="item"><h2>Harbor Park</h2><p>Year: 2022</p><p>Cost: $4.5M</p></div>
              <div class="item"><h2>Metro Line</h2><p>Year: 2023</p><p>Cost: $900K</p></div>"""
    recs = scraper.extract_records(html, scraper.load_categories(None), "http://x")
    assert [(r["Project Name"], r["Year"], r["Monetary Amount"]) for r in recs] == [
        ("Harbor Park", 2022, 4500000.0), ("Metro Line", 2023, 900000.0)]


def test_table_with_rowspan():
    html = """<table><tr><th>Year</th><th>Project</th><th>Cost</th></tr>
      <tr><td rowspan="2">2020</td><td>A</td><td>$1,000</td></tr>
      <tr><td>B</td><td>$2,000</td></tr></table>"""
    recs = scraper.extract_records(html, scraper.load_categories(None), "http://x")
    assert [(r["Project Name"], r["Year"], r["Monetary Amount"]) for r in recs] == [
        ("A", 2020, 1000.0), ("B", 2020, 2000.0)]


def test_js_detection():
    assert scraper.needs_javascript(200, "<html><body><div id='root'></div><script>x()</script></body></html>")[0]
    assert scraper.needs_javascript(200, "<title>Client Challenge</title><script></script>")[0]
    assert not scraper.needs_javascript(200, "<p>" + "Plenty of real content here. " * 50 + "</p>")[0]


def test_robots_wildcards():
    class FakeResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /search*\nDisallow: /private/\nAllow: /private/ok$\n"

    class FakeSession:
        def get(self, *a, **k):
            return FakeResp()

    rp = scraper.RobotsPolicy(FakeSession())
    assert not rp.allowed("https://x.org/search/?q=a")
    assert not rp.allowed("https://x.org/private/data")
    assert rp.allowed("https://x.org/private/ok")
    assert rp.allowed("https://x.org/sponsors/")


# --- end to end on the fixture sites ----------------------------------------

def _rows(result):
    return [(r["Project Name"], r["Year"]) for r in result["records"]]


def test_static_table_pagination(site):
    res = scraper.scrape(f"{site}/table-1.html", delay=0, render="auto")
    assert res["meta"]["render_mode"] == "beautifulsoup"
    assert res["meta"]["pages_scraped"] == 3
    assert _rows(res) == EXPECTED
    # "Budget (US$ millions)" header scales bare numbers
    assert res["records"][0]["Monetary Amount"] == 412_500_000


def test_numbered_pagination_cards(site):
    res = scraper.scrape(f"{site}/cards?page=1", delay=0)
    assert res["meta"]["pages_scraped"] == 3
    assert _rows(res) == EXPECTED
    assert all(r["Currency"] == "USD" for r in res["records"])


def test_max_pages_limit(site):
    res = scraper.scrape(f"{site}/table-1.html", delay=0, max_pages=2)
    assert res["meta"]["pages_scraped"] == 2 and len(res["records"]) == 6
    assert any("max_pages" in w for w in res["meta"]["warnings"])


def test_url_guard_blocks(site):
    def guard(url):
        raise ValueError("blocked")
    res = scraper.scrape(f"{site}/table-1.html", delay=0, url_guard=guard)
    assert res["records"] == [] and any("blocked" in w for w in res["meta"]["warnings"])


def _browser_available() -> bool:
    try:
        b = scraper.BrowserFetcher()
        b.start()
        b.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _browser_available(), reason="Playwright/Chromium not available")
def test_spa_rendering_and_click_pagination(site):
    res = scraper.scrape(f"{site}/spa.html", delay=0, render="auto")
    assert res["meta"]["render_mode"] == "playwright"
    assert res["meta"]["pages_scraped"] == 3
    assert _rows(res) == EXPECTED
    assert all(r["Currency"] == "EUR" for r in res["records"])
