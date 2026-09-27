#!/usr/bin/env python3
"""
scraper.py - configurable, category-driven web scraper.

Give it a URL and a list of *categories* (e.g. Year, Monetary Amount,
Project Name). It fetches the page (static HTML via requests/BeautifulSoup,
or a real browser via Playwright when the page needs JavaScript), finds the
records on the page (HTML tables, repeated cards/list items, or free-text
blocks), extracts one value per category per record, follows pagination and
writes everything to data.json.

Usage:
    python scraper.py https://pypi.org/sponsors/
    python scraper.py                       # prompts for the URL
    python scraper.py URL --categories categories.json --max-pages 5 --csv data.csv
    python scraper.py URL --add-category "Funder:text:from (?:the )?(.+?) in \\d{4}"

The same `scrape()` function powers the web UI in app.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("scraper")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36 DataScraper/1.0 (+polite; respects robots.txt)"
)
ROBOTS_AGENT = "DataScraper"

# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #

CATEGORY_TYPES = ("year", "money", "name", "number", "text")


@dataclass
class Category:
    """One column of the output table.

    type      how values are recognised and normalised (see CATEGORY_TYPES)
    keywords  hints matched against table headers and "Label: value" text
    pattern   optional regex run over each record's text; group 1 (or the
              whole match) becomes the value
    selector  optional CSS selector evaluated inside each record
    """

    name: str
    type: str = "text"
    keywords: list[str] = field(default_factory=list)
    pattern: str | None = None
    selector: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        self.type = (self.type or "text").lower()
        if self.type not in CATEGORY_TYPES:
            raise ValueError(f"Category {self.name!r}: unknown type {self.type!r} "
                             f"(expected one of {', '.join(CATEGORY_TYPES)})")
        if isinstance(self.keywords, str):
            self.keywords = [k.strip() for k in self.keywords.split(",") if k.strip()]
        self.keywords = [k.lower() for k in self.keywords]
        self.pattern = self.pattern or None
        self.selector = self.selector or None
        if self.pattern:
            re.compile(self.pattern)  # fail fast on a bad regex

    @property
    def is_signal(self) -> bool:
        """Can this category be *detected* in free text (used to find records)?"""
        return self.type in ("year", "money") or bool(self.pattern or self.selector)


DEFAULT_CATEGORIES: list[Category] = [
    Category("Project Name", "name",
             ["project", "name", "title", "program", "programme", "initiative", "scheme"]),
    Category("Year", "year",
             ["year", "fy", "fiscal", "date", "awarded", "completed", "opened", "start"]),
    Category("Monetary Amount", "money",
             ["amount", "cost", "budget", "funding", "value", "price", "award",
              "grant", "investment", "total", "usd", "$", "€", "£"]),
]


def load_categories(source: str | Path | list | None) -> list[Category]:
    """Load categories from a JSON file path, a list of dicts, or defaults."""
    if source is None:
        return [Category(**asdict(c)) for c in DEFAULT_CATEGORIES]
    if isinstance(source, (str, Path)):
        with open(source, encoding="utf-8") as fh:
            source = json.load(fh)
    cats = [c if isinstance(c, Category) else Category(**c) for c in source]
    return [c for c in cats if c.enabled and c.name.strip()]


def parse_category_spec(spec: str) -> Category:
    """'Name:type[:regex]' -> Category (CLI --add-category)."""
    parts = spec.split(":", 2)
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("use NAME:TYPE[:REGEX], e.g. 'Location:text:located in ([A-Z]\\w+)'")
    name, ctype = parts[0].strip(), parts[1].strip()
    pattern = parts[2] if len(parts) == 3 else None
    return Category(name=name, type=ctype, keywords=[name.lower()], pattern=pattern)


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #

YEAR_RE = re.compile(r"(?<![\d$€£.,])(?:FY\s?)?(1[89]\d{2}|20\d{2}|2100)(?![\d,])", re.I)

_CUR_SYMBOLS = {"us$": "USD", "a$": "AUD", "c$": "CAD", "$": "USD", "€": "EUR",
                "£": "GBP", "¥": "JPY", "₹": "INR", "dollars": "USD", "dollar": "USD",
                "euros": "EUR", "euro": "EUR", "pounds": "GBP"}
_MAGNITUDES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mm": 1e6, "million": 1e6,
               "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "tn": 1e12, "trillion": 1e12}
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MAG = r"thousand|million|billion|trillion|bn|mn|mm|tn|k|m|b|t"
_CODES = r"USD|EUR|GBP|CAD|AUD|JPY|INR|CHF|CNY|NZD|SGD|HKD"
MONEY_RE = re.compile(
    rf"""(?:
        (?P<pre>US\$|A\$|C\$|\$|€|£|¥|₹|\b(?:{_CODES})\b)\s?(?P<n1>{_NUM})(?:\s?(?P<m1>{_MAG})\b)?
      | (?P<n2>{_NUM})\s?(?P<m2>{_MAG})?\s?(?P<post>\b(?:{_CODES})\b|dollars?|euros?|pounds)
    )""",
    re.I | re.X,
)
NUMBER_RE = re.compile(rf"-?(?:{_NUM})")
HEADER_MULTIPLIER_RE = re.compile(r"\b(thousands?|millions?|billions?|bn|mn|\$\s?m|\$\s?bn|\$\s?k)\b|\(\s?(m|bn|k)\s?\)", re.I)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _to_float(num: str) -> float:
    return float(num.replace(",", ""))


def parse_money(text: str, default_multiplier: float = 1.0) -> tuple[float, str] | None:
    """'$1.2 million' -> (1200000.0, 'USD'). Returns None if no amount found."""
    m = MONEY_RE.search(text or "")
    if not m:
        return None
    num = m.group("n1") or m.group("n2")
    mag = (m.group("m1") or m.group("m2") or "").lower()
    cur = (m.group("pre") or m.group("post") or "").lower()
    value = _to_float(num) * _MAGNITUDES.get(mag, default_multiplier if not mag else 1.0)
    currency = _CUR_SYMBOLS.get(cur, cur.upper() or "")
    return (round(value, 2), currency)


def parse_year(text: str) -> int | None:
    # Remove money spans first so "$2,019" or "$2019" never reads as a year.
    stripped = MONEY_RE.sub(" ", text or "")
    m = YEAR_RE.search(stripped)
    return int(m.group(1)) if m else None


def parse_number(text: str, multiplier: float = 1.0) -> float | None:
    m = NUMBER_RE.search(text or "")
    return round(_to_float(m.group(0)) * multiplier, 4) if m else None


def header_multiplier(header: str) -> float:
    """'Cost (US$ millions)' -> 1e6, used when cells hold bare numbers."""
    m = HEADER_MULTIPLIER_RE.search(header or "")
    if not m:
        return 1.0
    word = (m.group(1) or m.group(2) or "").lower().replace("$", "").strip()
    word = word.rstrip("s")
    return _MAGNITUDES.get(word, 1.0)


def coerce(cat: Category, raw: str, multiplier: float = 1.0,
           bare_numbers: bool = True) -> tuple[Any, str | None]:
    """Normalise a raw string according to the category type.

    Returns (value, currency) - currency is only set for money categories.
    """
    raw = clean(raw)
    if not raw:
        return None, None
    if cat.type == "year":
        return parse_year(raw), None
    if cat.type == "money":
        money = parse_money(raw, multiplier)
        if money:
            return money
        if not bare_numbers:  # free text: "2019" or "21.1 million people" is not money
            return None, None
        # Bare number in a money column (currency implied by the header).
        num = parse_number(raw, multiplier)
        return (num, None) if num is not None else (None, None)
    if cat.type == "number":
        return parse_number(raw, multiplier), None
    if cat.type == "name":
        return raw[:200].rstrip(" .,;:-–—"), None
    return raw[:500], None


# --------------------------------------------------------------------------- #
# Fetching (static + browser), robots.txt, JS detection
# --------------------------------------------------------------------------- #

class FetchError(RuntimeError):
    pass


class GuardedSession(requests.Session):
    """requests.Session that validates every redirect target with `url_guard`."""

    url_guard: Callable[[str], Any] | None = None

    def get_redirect_target(self, resp):  # type: ignore[override]
        target = super().get_redirect_target(resp)
        if target and self.url_guard:
            self.url_guard(urljoin(resp.url, target))  # raises ValueError to abort
        return target


def build_session(user_agent: str = USER_AGENT, retries: int = 3,
                  url_guard: Callable[[str], Any] | None = None) -> requests.Session:
    session = GuardedSession()
    session.url_guard = url_guard
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    retry = Retry(total=retries, connect=retries, read=retries, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"), respect_retry_after_header=True,
                  raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class RobotsPolicy:
    """robots.txt checker supporting '*' and '$' wildcards (stdlib's doesn't)."""

    def __init__(self, session: requests.Session, agent: str = ROBOTS_AGENT):
        self.session, self.agent = session, agent.lower()
        self._cache: dict[str, list[tuple[bool, re.Pattern, int]]] = {}

    def _rules_for(self, origin: str) -> list[tuple[bool, re.Pattern, int]]:
        if origin in self._cache:
            return self._cache[origin]
        rules: list[tuple[bool, re.Pattern, int]] = []
        try:
            resp = self.session.get(origin + "/robots.txt", timeout=10)
            body = resp.text if resp.status_code == 200 else ""
        except requests.RequestException as exc:
            log.warning("Could not fetch robots.txt (%s) - assuming allowed", exc)
            body = ""
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        lines: list[tuple[bool, str]] = []
        last_was_agent = False
        for line in body.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and agents:
                    groups.append((agents, lines))
                    agents, lines = [], []
                agents.append(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if val:
                    lines.append((key == "allow", val))
        if agents:
            groups.append((agents, lines))
        chosen = [l for a, l in groups if any(x != "*" and x in self.agent for x in a)] or \
                 [l for a, l in groups if "*" in a]
        for group_lines in chosen:
            for allow, path in group_lines:
                regex = "^" + re.escape(path).replace(r"\*", ".*").replace(r"\$", "$")
                rules.append((allow, re.compile(regex), len(path)))
        self._cache[origin] = rules
        return rules

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        best: tuple[int, bool] = (-1, True)
        for allow, regex, length in self._rules_for(f"{p.scheme}://{p.netloc}"):
            if regex.match(path) and (length > best[0] or (length == best[0] and allow)):
                best = (length, allow)
        return best[1]


CHALLENGE_MARKERS = ("client challenge", "just a moment", "checking your browser",
                     "enable javascript", "please turn javascript on", "attention required",
                     "javascript is required", "you need to enable javascript")
SPA_ROOT_IDS = ("root", "app", "__next", "__nuxt", "svelte", "main-app")


def visible_text(soup: BeautifulSoup) -> str:
    body = soup.body or soup
    parts = []
    for s in body.find_all(string=True):
        if s.parent and s.parent.name in ("script", "style", "noscript", "template"):
            continue
        parts.append(s)
    return clean(" ".join(parts))


def needs_javascript(status: int, html: str) -> tuple[bool, str]:
    """Heuristic: does this page need a real browser to show its content?"""
    soup = BeautifulSoup(html or "", "lxml")
    title = clean(soup.title.get_text()) if soup.title else ""
    text = visible_text(soup)
    haystack = (title + " " + text[:600]).lower()
    scripts = soup.find_all("script")
    if any(m in haystack for m in CHALLENGE_MARKERS) and len(text) < 3000:
        return True, f"bot/JS challenge page detected (title={title!r})"
    if len(text) < 250 and scripts:
        return True, f"almost no visible text ({len(text)} chars) but {len(scripts)} <script> tags"
    for rid in SPA_ROOT_IDS:
        root = soup.find(id=rid)
        if root is not None and len(clean(root.get_text())) < 50 and len(text) < 1500:
            return True, f"empty single-page-app mount point #{rid}"
    if status in (403, 429, 503) and scripts:
        return True, f"HTTP {status} with scripts - likely JS-gated"
    return False, f"static HTML looks complete ({len(text)} chars of visible text)"


def _find_chromium() -> str | None:
    """Fallback browser binary when Playwright's bundled revision isn't installed."""
    candidates = [os.environ.get("CHROMIUM_EXECUTABLE", "")]
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") or Path.home() / ".cache/ms-playwright")
    candidates += [str(base / "chromium"), *map(str, sorted(base.glob("chromium-*/chrome-linux*/chrome"), reverse=True))]
    candidates += ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


class BrowserFetcher:
    """Lazily-started headless Chromium via Playwright."""

    def __init__(self, user_agent: str = USER_AGENT, timeout_ms: int = 30000,
                 url_guard: Callable[[str], Any] | None = None):
        self.user_agent, self.timeout_ms, self.url_guard = user_agent, timeout_ms, url_guard
        self._pw = self._browser = self.page = None
        self._host_ok: dict[str, bool] = {}

    def start(self) -> None:
        if self.page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # e.g. on Vercel where Playwright isn't installed
            raise FetchError("This page needs JavaScript rendering but Playwright is not "
                             "installed (pip install playwright && playwright install chromium)") from exc
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {"headless": True}
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:  # corporate/sandbox proxies: honour NO_PROXY so localhost stays direct
            bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
            bypass = ",".join(filter(None, ["localhost", "127.0.0.1", "::1", bypass]))
            launch["proxy"] = {"server": proxy, "bypass": bypass}
        try:
            self._browser = self._pw.chromium.launch(**launch)
        except Exception as exc:
            exe = _find_chromium()
            if not exe:
                self.close()
                raise FetchError(f"Could not launch Chromium ({exc}). Run: playwright install chromium") from exc
            log.info("Bundled Chromium unavailable - using %s", exe)
            self._browser = self._pw.chromium.launch(executable_path=exe, **launch)
        ctx = self._browser.new_context(user_agent=self.user_agent, ignore_https_errors=False)
        self.page = ctx.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        if self.url_guard:
            self.page.route("**/*", self._guard_route)
        log.info("Headless Chromium started")

    def _guard_route(self, route) -> None:
        """Block every browser request (redirects, XHR, iframes) the guard rejects."""
        url = route.request.url
        host = urlparse(url).netloc
        if host not in self._host_ok:
            try:
                self.url_guard(url)
                self._host_ok[host] = True
            except ValueError:
                self._host_ok[host] = False
                log.warning("  blocked browser request to %s", host)
        if self._host_ok[host] or url.startswith(("data:", "blob:")):
            route.continue_()
        else:
            route.abort()

    def get(self, url: str) -> tuple[int, str, str]:
        self.start()
        resp = self.page.goto(url, wait_until="domcontentloaded")
        self._settle()
        return (resp.status if resp else 0), self.page.content(), self.page.url

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            log.debug("networkidle not reached - continuing with current DOM")
        # Nudge lazy-loaded content.
        try:
            self.page.mouse.wheel(0, 20000)
            self.page.wait_for_timeout(500)
        except Exception:
            pass

    def click_next(self) -> bool:
        """Click a JS-only 'Next' control (no href). Returns True if the DOM changed."""
        selectors = ['button:has-text("Next")', '[aria-label*="next" i]:not([disabled])',
                     'a:has-text("Next")', 'button:has-text("›")', 'button:has-text("»")',
                     '[class*="next" i]:not([disabled])']
        before = self.page.content()
        for sel in selectors:
            loc = self.page.locator(sel).first
            try:
                if loc.count() and loc.is_visible() and loc.is_enabled():
                    if (loc.get_attribute("aria-disabled") or "").lower() == "true":
                        continue
                    loc.click()
                    self._settle()
                    return self.page.content() != before
            except Exception:
                continue
        return False

    def close(self) -> None:
        for obj in (self._browser, self._pw):
            try:
                if obj is self._pw and obj:
                    obj.stop()
                elif obj:
                    obj.close()
            except Exception:
                pass
        self._pw = self._browser = self.page = None


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

SKIP_TAGS = {"script", "style", "noscript", "template", "head", "nav", "footer", "form",
             "svg", "select", "option", "button", "iframe"}
MAX_RECORD_CHARS = 2500


def _cell_grid(table: Tag) -> list[list[Tag | None]]:
    """Expand a <table> into a rectangular grid honouring rowspan/colspan."""
    grid: list[list[Tag | None]] = []
    pending: dict[tuple[int, int], Tag] = {}
    for r, tr in enumerate(tr for tr in table.find_all("tr") if tr.find_parent("table") is table):
        row: list[Tag | None] = []
        col = 0
        cells = tr.find_all(["td", "th"], recursive=False)
        ci = 0
        while ci < len(cells) or (r, col) in pending:
            if (r, col) in pending:
                row.append(pending.pop((r, col)))
                col += 1
                continue
            cell = cells[ci]
            ci += 1
            try:
                cs = max(1, min(int(cell.get("colspan", 1)), 50))
                rs = max(1, min(int(cell.get("rowspan", 1)), 500))
            except ValueError:
                cs = rs = 1
            for dc in range(cs):
                row.append(cell)
                for dr in range(1, rs):
                    pending[(r + dr, col + dc)] = cell
            col += cs
        grid.append(row)
    return grid


def _keyword_score(header: str, cat: Category) -> int:
    h = header.lower()
    score = 0
    if cat.name.lower() == h or cat.name.lower() in h:
        score += 10
    for kw in cat.keywords:
        if not kw:
            continue
        if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", h):
            score += 3 + len(kw) // 4
    return score


def extract_from_tables(soup: BeautifulSoup, cats: list[Category], page_url: str,
                        min_fields: int) -> tuple[list[dict], set[int]]:
    records: list[dict] = []
    used_tables: set[int] = set()
    for t_index, table in enumerate(soup.find_all("table")):
        grid = _cell_grid(table)
        if len(grid) < 2:
            continue
        # Header row: first row made of <th>, else first row.
        h_idx = next((i for i, row in enumerate(grid[:3])
                      if row and sum(1 for c in row if c is not None and c.name == "th") >= len(row) / 2), 0)
        headers = [clean(c.get_text(" ")) if c is not None else "" for c in grid[h_idx]]
        body = [row for row in grid[h_idx + 1:] if len(row) == len(headers)]
        if not body:
            continue
        texts = [[clean(c.get_text(" ")) if c is not None else "" for c in row] for row in body]

        def col_ratio(ci: int, fn) -> float:
            vals = [r[ci] for r in texts if r[ci]]
            return sum(1 for v in vals if fn(v)) / len(vals) if vals else 0.0

        mapping: dict[str, int] = {}
        taken: set[int] = set()
        # 1) header keywords, 2) content-based fallback per type.
        for cat in sorted(cats, key=lambda c: c.type == "name"):  # name last: it's the vaguest
            scores = [(_keyword_score(h, cat), i) for i, h in enumerate(headers) if i not in taken]
            best = max(scores, default=(0, -1))
            if best[0] > 0:
                mapping[cat.name] = best[1]
                taken.add(best[1])
                continue
            ncols = len(headers)
            if cat.type == "year":
                cand = [(col_ratio(i, lambda v: parse_year(v) is not None and len(v) < 40), i)
                        for i in range(ncols) if i not in taken]
            elif cat.type == "money":
                cand = [(col_ratio(i, lambda v: parse_money(v) is not None), i)
                        for i in range(ncols) if i not in taken]
            elif cat.type == "name":
                cand = [(col_ratio(i, lambda v: len(re.findall(r"[A-Za-z]", v)) > 3), i)
                        for i in range(ncols) if i not in taken]
            else:
                cand = []
            best_c = max(cand, default=(0.0, -1))
            if best_c[0] >= 0.6:
                mapping[cat.name] = best_c[1]
                taken.add(best_c[1])

        signal_mapped = sum(1 for c in cats if c.name in mapping and c.type != "name")
        if len(mapping) < min(min_fields, len(cats)) or signal_mapped == 0:
            continue
        log.info("  table #%d: %d rows, columns mapped -> %s", t_index + 1, len(body),
                 ", ".join(f"{k}='{headers[v]}'" for k, v in mapping.items()))
        mults = {name: header_multiplier(headers[ci]) for name, ci in mapping.items()}
        added = 0
        for row_cells, row_text in zip(body, texts):
            rec: dict[str, Any] = {}
            filled = 0
            for cat in cats:
                if cat.name not in mapping:
                    rec[cat.name] = None
                    continue
                ci = mapping[cat.name]
                raw = row_text[ci]
                if cat.pattern:
                    m = re.search(cat.pattern, raw, re.I)
                    raw = (m.group(1) if m and m.groups() else m.group(0)) if m else ""
                value, cur = coerce(cat, raw, mults.get(cat.name, 1.0))
                rec[cat.name] = value
                if cur and "Currency" not in rec:
                    rec["Currency"] = cur
                filled += value is not None
            if filled >= min_fields:
                rec["source_url"] = page_url
                records.append(rec)
                added += 1
        if added:
            used_tables.add(id(table))
    return records, used_tables


def _block_text(el: Tag) -> str:
    return clean(el.get_text(" "))


def _signal_hits(text: str, cats: list[Category]) -> int:
    hits = 0
    for cat in cats:
        if cat.selector:
            continue  # evaluated per element, not per text
        if cat.pattern:
            hits += bool(re.search(cat.pattern, text, re.I))
        elif cat.type == "year":
            hits += parse_year(text) is not None
        elif cat.type == "money":
            hits += parse_money(text) is not None
    return hits


def _label_value(el: Tag, cat: Category) -> str | None:
    """Find 'Keyword: value' lines (or <dt>/<th> label + sibling) inside a record."""
    labels = [cat.name.lower(), *cat.keywords]
    for line in el.get_text("\n").split("\n"):
        line = clean(line)
        if ":" in line:
            label, _, value = line.partition(":")
            label = label.strip().lower()
            if (value.strip() and len(label) <= 30 and not re.search(r"[.!?]\s", label)
                    and any(re.search(rf"(?<![a-z]){re.escape(l)}(?![a-z])", label) for l in labels)):
                return value
    for lab in el.find_all(["dt", "th", "label", "strong", "b"]):
        t = clean(lab.get_text()).rstrip(":").lower()
        if t and t in labels:
            nxt = lab.find_next_sibling()
            if nxt is not None:
                return nxt.get_text(" ")
            tail = lab.next_sibling
            if isinstance(tail, NavigableString) and clean(tail).strip(":"):
                return str(tail).lstrip(": ")
    return None


def _record_value(el: Tag, text: str, cat: Category) -> tuple[Any, str | None]:
    """Try each strategy in priority order; the first one that parses wins."""
    if cat.selector:
        found = el.select_one(cat.selector)
        return coerce(cat, found.get_text(" ") if found else "", bare_numbers=False)
    if cat.pattern:
        m = re.search(cat.pattern, text, re.I)
        return coerce(cat, (m.group(1) if m.groups() else m.group(0)) if m else "", bare_numbers=False)
    candidates = []
    if cat.keywords:
        candidates.append(lambda: _label_value(el, cat))
    if cat.type in ("year", "money", "number"):
        candidates.append(lambda: text)
    elif cat.type == "name":
        candidates.append(lambda: _guess_name(el))
    for get in candidates:
        value, cur = coerce(cat, get() or "", bare_numbers=False)
        if value is not None:
            return value, cur
    return None, None


def _guess_name(el: Tag) -> str | None:
    for h in el.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        t = clean(h.get_text(" "))
        if t:
            return t
    for cand in el.select('[class*="title" i], [class*="name" i], [itemprop="name"]'):
        t = clean(cand.get_text(" "))
        if t and parse_money(t) is None:
            return t
    for s in el.find_all(["strong", "b"]):
        t = clean(s.get_text(" "))
        if len(t) > 3 and parse_money(t) is None and parse_year(t) is None:
            return t
    # First meaningful line of text (text before the first <br>/block child).
    for line in el.get_text("\n").split("\n"):
        t = clean(line)
        if len(re.findall(r"[A-Za-z]", t)) >= 4 and parse_money(t) is None:
            return t
    return None


def extract_from_blocks(soup: BeautifulSoup, cats: list[Category], page_url: str,
                        min_fields: int, skip_tables: set[int]) -> list[dict]:
    """Find repeated cards / list items / paragraphs that carry the requested data.

    1. Every element whose text matches >= min_signal detectable categories is a
       candidate; keep only the *smallest* such elements (no matching descendant).
    2. Grow each one upward to its record container while the parent holds no
       other match (so a card's heading is included, but the list isn't).
    """
    signal_cats = [c for c in cats if c.is_signal]
    if not signal_cats:
        log.warning("  none of the categories can be detected in free text "
                    "(add a year/money category, a regex pattern or a CSS selector)")
        return []
    # Free text is noisy: require every detectable category (up to 2) to be present.
    min_signal = min(len(signal_cats), 2)

    def skipped(el: Tag) -> bool:
        for p in el.parents:
            if p.name in SKIP_TAGS or (p.name == "table" and id(p) in skip_tables):
                return True
        return False

    matched: dict[int, Tag] = {}
    for el in soup.find_all(True):
        if el.name in SKIP_TAGS or el.name in ("html", "body", "table", "tbody", "thead", "tr"):
            continue
        text = _block_text(el)
        if not text or len(text) > MAX_RECORD_CHARS:
            continue
        if _signal_hits(text, signal_cats) >= min_signal and not skipped(el):
            matched[id(el)] = el
    has_matched_desc: set[int] = set()
    for el in matched.values():
        for p in el.parents:
            has_matched_desc.add(id(p))
    minimal = [el for el in matched.values() if id(el) not in has_matched_desc]

    # Count matches under each ancestor to know when growing would swallow siblings.
    under: dict[int, int] = {}
    for el in minimal:
        for p in el.parents:
            under[id(p)] = under.get(id(p), 0) + 1

    containers: list[Tag] = []
    seen: set[int] = set()
    for el in minimal:
        node = el
        while (not _is_record_boundary(node) and node.parent is not None
               and node.parent.name not in ("body", "html", "main", "[document]", "ul", "ol", "dl", "tbody")
               and under.get(id(node.parent), 0) == 1
               and len(_block_text(node.parent)) <= MAX_RECORD_CHARS):
            node = node.parent
        if id(node) not in seen:
            seen.add(id(node))
            containers.append(node)

    records: list[dict] = []
    signatures: list[tuple] = []
    for el in containers:
        text = _block_text(el)
        rec: dict[str, Any] = {}
        filled = 0
        for cat in cats:
            value, cur = _record_value(el, text, cat)
            rec[cat.name] = value
            if cur and "Currency" not in rec:
                rec["Currency"] = cur
            filled += value is not None
        if filled >= min_fields:
            rec["source_url"] = page_url
            records.append(rec)
            signatures.append((el.name, tuple(sorted(el.get("class", []))),
                               el.parent.name if el.parent else None))
    records, signatures = _drop_outliers(records, signatures, cats)
    if records:
        log.info("  %d record block(s) found in %s elements", len(records),
                 ", ".join(sorted({f"<{sig[0]}>" for sig in signatures})))
    return records


RECORD_TAGS = {"li", "article", "tr", "dd"}
RECORD_CLASS_RE = re.compile(r"card|item|result|entry|record|listing|project|grant|award|row", re.I)


def _is_record_boundary(el: Tag) -> bool:
    """True if `el` already looks like one whole record, so we stop growing."""
    if el.name in RECORD_TAGS:
        return True
    if RECORD_CLASS_RE.search(" ".join(el.get("class", []))):
        return True
    parent = el.parent
    if parent is not None:  # one of several same-shaped siblings -> a list item
        sig = (el.name, tuple(el.get("class", [])))
        same = sum(1 for c in parent.find_all(el.name, recursive=False)
                   if (c.name, tuple(c.get("class", []))) == sig)
        if same >= 2 and el.name not in ("p", "span", "small", "br"):
            return True
    return False


def _drop_outliers(records: list[dict], sigs: list[tuple],
                   cats: list[Category]) -> tuple[list[dict], list[tuple]]:
    """Drop one-off blocks that don't look like the page's repeated records.

    If >= 2 records share a DOM shape and fill a category every time, a record
    with a unique shape that lacks that category is almost always page chrome.
    """
    counts: dict[tuple, int] = {}
    for sig in sigs:
        counts[sig] = counts.get(sig, 0) + 1
    main = max(counts, key=counts.get, default=None)
    if main is None or counts[main] < 2:
        return records, sigs
    core = [c.name for c in cats
            if all(r.get(c.name) is not None for r, s in zip(records, sigs) if s == main)]
    keep = [counts[s] > 1 or all(r.get(n) is not None for n in core) for r, s in zip(records, sigs)]
    if not all(keep):
        log.info("  dropped %d outlier block(s) that don't match the repeated record layout",
                 keep.count(False))
    return ([r for r, k in zip(records, keep) if k], [s for s, k in zip(sigs, keep) if k])


def extract_records(html: str, cats: list[Category], page_url: str,
                    min_fields: int | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    if min_fields is None:
        min_fields = min(2, len(cats))
    table_recs, used = extract_from_tables(soup, cats, page_url, min_fields)
    block_recs = extract_from_blocks(soup, cats, page_url, min_fields, used)
    order = [c.name for c in cats] + ["Currency", "source_url"]
    return [{k: r[k] for k in order if k in r} for r in table_recs + block_recs]


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #

NEXT_TEXT_RE = re.compile(r"^\s*(next(\s*page)?\W{0,3}|[›»→]|>|>>|older( posts| entries)?|"
                          r"load more|more results)\s*$", re.I)
PAGE_PARAMS = ("page", "p", "pg", "paged", "pageNumber", "page_number", "offset", "start")


def _normalise(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", p.query, ""))


def find_next_url(html: str, current_url: str, visited: set[str]) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(current_url).netloc

    def ok(href: str | None) -> str | None:
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            return None
        full = urljoin(current_url, href)
        if urlparse(full).netloc != host or _normalise(full) in visited:
            return None
        return full

    candidates: list[str | None] = []
    for tag in soup.select('link[rel~="next"], a[rel~="next"]'):
        candidates.append(tag.get("href"))
    for a in soup.select('a[aria-label]'):
        if "next" in a["aria-label"].lower():
            candidates.append(a.get("href"))
    for a in soup.find_all("a", href=True):
        if NEXT_TEXT_RE.match(clean(a.get_text())) or NEXT_TEXT_RE.match(a.get("title", "") or "x"):
            candidates.append(a["href"])
    for el in soup.select('[class*="next" i]'):
        a = el if el.name == "a" else el.find("a", href=True)
        if a is not None and "disabled" not in " ".join(el.get("class", [])).lower():
            candidates.append(a.get("href"))
    for c in candidates:
        full = ok(c)
        if full:
            return full

    # Numbered pagination: a link to ?page=N+1 on the page.
    cur = urlparse(current_url)
    qs = parse_qs(cur.query)
    for param in PAGE_PARAMS:
        if param in qs and qs[param][0].isdigit():
            n = int(qs[param][0])
            step = 1 if param not in ("offset", "start") else None
            for a in soup.find_all("a", href=True):
                full = ok(a["href"])
                if not full:
                    continue
                q2 = parse_qs(urlparse(full).query)
                if param in q2 and q2[param][0].isdigit():
                    m = int(q2[param][0])
                    if (step and m == n + 1) or (not step and m > n):
                        return full
    return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def polite_sleep(delay: float) -> None:
    if delay > 0:
        t = delay * random.uniform(0.75, 1.35)
        log.info("  sleeping %.1fs (rate limit)", t)
        time.sleep(t)


def _dedupe(records: list[dict], cats: list[Category]) -> list[dict]:
    seen, out = set(), []
    for r in records:
        key = tuple(json.dumps(r.get(c.name), sort_keys=True) for c in cats)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def scrape(url: str, categories: Iterable[Category] | None = None, *, max_pages: int = 10,
           delay: float = 1.5, render: str = "auto", respect_robots: bool = True,
           min_fields: int | None = None, timeout: float = 20.0,
           url_guard: Callable[[str], Any] | None = None) -> dict:
    """Scrape `url` (and following pages). Returns {'meta': ..., 'records': [...]}.

    url_guard: optional callable that raises ValueError for URLs that must not be
    fetched (the web app uses it to block private-network targets, incl. redirects).
    """
    cats = list(categories) if categories is not None else load_categories(None)
    if not cats:
        raise ValueError("At least one category is required")
    if not re.match(r"^https?://", url or "", re.I):
        url = "https://" + (url or "").strip()
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"Not a valid URL: {url!r}")
    render = render.lower()
    if render not in ("auto", "static", "js"):
        raise ValueError("render must be auto, static or js")

    started = time.time()
    session = build_session(url_guard=url_guard)
    robots = RobotsPolicy(session)
    browser: BrowserFetcher | None = None
    warnings: list[str] = []
    records: list[dict] = []
    pages: list[dict] = []
    visited: set[str] = set()
    mode = "js" if render == "js" else "static"
    mode_reason = "forced by user" if render != "auto" else ""

    log.info("Target: %s", url)
    log.info("Categories: %s", ", ".join(f"{c.name} [{c.type}]" for c in cats))

    next_url: str | None = url
    clicked = False  # True when the browser already shows the next page (JS "Next" button)
    try:
        while next_url and len(pages) < max_pages:
            page_no = len(pages) + 1
            status, html, final_url = 0, "", next_url
            try:
                if clicked:
                    status, html, final_url = 200, browser.page.content(), browser.page.url
                    log.info("[page %d] (clicked JS 'Next') %s", page_no, final_url)
                else:
                    if respect_robots and not robots.allowed(next_url):
                        msg = (f"robots.txt disallows {next_url} - skipped "
                               "(use --ignore-robots only if you have permission)")
                        log.warning(msg)
                        warnings.append(msg)
                        break
                    if url_guard:
                        url_guard(next_url)
                    visited.add(_normalise(next_url))
                    log.info("[page %d] GET %s (%s)", page_no, next_url,
                             "playwright" if mode == "js" else "requests")
                    if mode == "static":
                        resp = session.get(next_url, timeout=timeout)
                        if "charset" not in resp.headers.get("Content-Type", "").lower():
                            resp.encoding = resp.apparent_encoding or "utf-8"
                        status, html, final_url = resp.status_code, resp.text, resp.url
                        log.info("  HTTP %d, %d KB", status, len(resp.content) // 1024)
                        if page_no == 1 and render == "auto":
                            js, why = needs_javascript(status, html)
                            mode_reason = why
                            log.info("  JS check: %s -> %s", why, "Playwright" if js else "BeautifulSoup")
                            if js:
                                mode = "js"
                    if mode == "js":
                        try:
                            browser = browser or BrowserFetcher(url_guard=url_guard)
                            status, html, final_url = browser.get(next_url)
                            log.info("  rendered with Chromium: HTTP %d, %d KB", status, len(html) // 1024)
                        except FetchError as exc:
                            if not (render == "auto" and html):
                                raise
                            # e.g. serverless host without a browser: use what static HTML we have
                            msg = f"{exc} - falling back to static HTML"
                            log.warning("  %s", msg)
                            warnings.append(msg)
                            mode, browser = "static", None
                    if status >= 400:
                        raise FetchError(f"HTTP {status}")
            except (requests.RequestException, FetchError, ValueError) as exc:
                msg = f"page {page_no}: fetch failed ({exc})"
                log.error("  %s", msg)
                warnings.append(msg)
                break
            except Exception as exc:  # Playwright timeouts, browser crashes...
                msg = f"page {page_no}: browser error ({type(exc).__name__}: {exc})"
                log.error("  %s", msg)
                warnings.append(msg)
                break
            clicked = False
            visited.add(_normalise(final_url))

            try:
                page_records = extract_records(html, cats, final_url, min_fields)
            except Exception as exc:
                log.exception("  extraction failed on page %d", page_no)
                warnings.append(f"page {page_no}: extraction error ({exc})")
                page_records = []

            # Auto mode: static page gave nothing but is script-heavy -> try the browser once.
            if (not page_records and mode == "static" and render == "auto" and page_no == 1
                    and len(re.findall(r"<script", html, re.I)) >= 5):
                log.info("  no records in static HTML but page is script-heavy - retrying with Playwright")
                try:
                    browser = BrowserFetcher(url_guard=url_guard)
                    status, html, final_url = browser.get(next_url)
                    page_records = extract_records(html, cats, final_url, min_fields)
                    if page_records:
                        mode, mode_reason = "js", "static HTML had no records; rendered DOM did"
                except Exception as exc:
                    warnings.append(f"browser fallback failed ({exc})")

            log.info("  -> %d record(s) on this page", len(page_records))
            records.extend(page_records)
            pages.append({"page": page_no, "url": final_url, "status": status, "records": len(page_records)})
            if len(pages) >= max_pages:
                if find_next_url(html, final_url, visited):
                    warnings.append(f"stopped at max_pages={max_pages}; more pages exist")
                break

            nxt = find_next_url(html, final_url, visited)
            if nxt:
                log.info("  pagination: next page -> %s", nxt)
                polite_sleep(delay)
                next_url = nxt
            elif mode == "js" and browser is not None and page_records and browser.click_next():
                log.info("  pagination: clicked a JS 'Next' control")
                polite_sleep(delay)
                clicked = True
            else:
                log.info("  pagination: no further pages found")
                next_url = None
    finally:
        if browser is not None:
            browser.close()

    before = len(records)
    records = _dedupe(records, cats)
    if before != len(records):
        log.info("Removed %d duplicate record(s)", before - len(records))

    # Null audit so problems are visible instead of silent.
    for cat in cats:
        nulls = sum(1 for r in records if r.get(cat.name) is None)
        if records and nulls:
            warnings.append(f"{nulls}/{len(records)} record(s) have no '{cat.name}'")
    if not records:
        warnings.append("no records found - try adding keywords, a regex pattern or a CSS selector")

    elapsed = round(time.time() - started, 2)
    log.info("Done: %d record(s) from %d page(s) in %.1fs", len(records), len(pages), elapsed)
    return {
        "meta": {
            "source_url": url,
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "render_mode": "playwright" if mode == "js" else "beautifulsoup",
            "render_reason": mode_reason,
            "pages_scraped": len(pages),
            "pages": pages,
            "record_count": len(records),
            "categories": [asdict(c) for c in cats],
            "elapsed_seconds": elapsed,
            "warnings": warnings,
        },
        "records": records,
    }


# --------------------------------------------------------------------------- #
# Output helpers + CLI
# --------------------------------------------------------------------------- #

def table_columns(result: dict) -> list[str]:
    cols = [c["name"] for c in result["meta"]["categories"]]
    if any("Currency" in r for r in result["records"]):
        cols.append("Currency")
    return cols


def format_table(result: dict, max_width: int = 60) -> str:
    cols = table_columns(result)
    rows = []
    for r in result["records"]:
        row = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float) and v.is_integer():
                v = f"{int(v):,}"
            elif isinstance(v, float):
                v = f"{v:,.2f}"
            s = "—" if v is None else str(v)
            row.append(s if len(s) <= max_width else s[: max_width - 1] + "…")
        rows.append(row)
    widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(cols)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |", line]
    out += ["| " + " | ".join(v.ljust(w) for v, w in zip(r, widths)) + " |" for r in rows]
    out.append(line)
    return "\n".join(out)


def write_csv(result: dict, path: str) -> None:
    import csv
    cols = table_columns(result) + ["source_url"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(result["records"])


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Category-driven web scraper -> data.json")
    ap.add_argument("url", nargs="?", help="page to scrape (prompted for if omitted)")
    ap.add_argument("-c", "--categories", help="JSON file with category definitions (default: built-in "
                                               "Project Name / Year / Monetary Amount)")
    ap.add_argument("-a", "--add-category", action="append", type=parse_category_spec, default=[],
                    metavar="NAME:TYPE[:REGEX]", help="add a category, e.g. 'Funder:text:from (?:the )?(.+?) in \\d{4}'")
    ap.add_argument("--only", help="comma-separated category names to keep, e.g. 'Year,Project Name'")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--delay", type=float, default=1.5, help="base seconds between requests (jittered)")
    ap.add_argument("--render", choices=("auto", "static", "js"), default="auto",
                    help="auto-detect JS need (default), force BeautifulSoup, or force Playwright")
    ap.add_argument("--min-fields", type=int, help="min non-null categories for a record (default 2)")
    ap.add_argument("--ignore-robots", action="store_true", help="skip robots.txt checks (only with permission)")
    ap.add_argument("-o", "--out", default="data.json")
    ap.add_argument("--csv", help="also write a CSV file")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    url = args.url or input("Website URL to scrape: ").strip()
    try:
        cats = load_categories(args.categories) + args.add_category
    except (OSError, ValueError, TypeError, re.error) as exc:
        log.error("Bad category configuration: %s", exc)
        return 2
    if args.only:
        keep = {s.strip().lower() for s in args.only.split(",")}
        cats = [c for c in cats if c.name.lower() in keep]

    try:
        result = scrape(url, cats, max_pages=args.max_pages, delay=args.delay, render=args.render,
                        respect_robots=not args.ignore_robots, min_fields=args.min_fields)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130

    Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Saved %d record(s) -> %s", len(result["records"]), args.out)
    if args.csv:
        write_csv(result, args.csv)
        log.info("Saved CSV -> %s", args.csv)
    if result["records"]:
        print("\n" + format_table(result))
    for w in result["meta"]["warnings"]:
        log.warning("%s", w)
    return 0 if result["records"] else 1


if __name__ == "__main__":
    sys.exit(main())
