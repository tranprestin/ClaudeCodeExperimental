"""
app.py - web front end for scraper.py.

    python app.py                 # http://127.0.0.1:5000
    ALLOW_PRIVATE_URLS=1 python app.py   # also allow localhost targets (local testing only)

Routes
    GET  /                        the UI (URL box, editable category table, results table)
    GET  /api/categories          default category definitions
    POST /api/scrape              {url, categories, max_pages, delay, render} -> {meta, records, log}
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from dataclasses import asdict
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

import scraper

app = Flask(__name__)
logging.getLogger("scraper").setLevel(logging.INFO)  # so the UI log panel gets INFO lines

MAX_PAGES_LIMIT = int(os.environ.get("MAX_PAGES_LIMIT", "10"))
MIN_DELAY = float(os.environ.get("MIN_DELAY", "1.0"))
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS") == "1"
# Serverless hosts (Vercel) have no browser; the scraper then reports that JS rendering is unavailable.


class _RequestLogCapture(logging.Handler):
    """Collect the scraper's log lines for the current request thread only."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.thread_id = threading.get_ident()
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread_id:
            self.lines.append(self.format(record))


def validate_public_url(url: str) -> str:
    """Block SSRF: refuse non-http(s) schemes and private/loopback/link-local targets."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Please enter a URL")
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Only http(s) URLs are supported")
    if ALLOW_PRIVATE_URLS:
        return url
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host {p.hostname!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("That address points to a private network and can't be scraped from here")
    return url


@app.get("/")
def index():
    return render_template("index.html", max_pages_limit=MAX_PAGES_LIMIT, min_delay=MIN_DELAY)


@app.get("/api/categories")
def default_categories():
    return jsonify([asdict(c) for c in scraper.DEFAULT_CATEGORIES])


@app.post("/api/scrape")
def api_scrape():
    body = request.get_json(silent=True) or {}
    try:
        url = validate_public_url(body.get("url", ""))
        cats = scraper.load_categories(body.get("categories") or None)
        if not cats:
            raise ValueError("Enable at least one category")
        max_pages = max(1, min(int(body.get("max_pages", 3)), MAX_PAGES_LIMIT))
        delay = max(MIN_DELAY, float(body.get("delay", 1.5)))
        render = str(body.get("render", "auto"))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # bad regex etc.
        return jsonify({"error": f"Invalid category definition: {exc}"}), 400

    capture = _RequestLogCapture()
    logger = logging.getLogger("scraper")
    logger.addHandler(capture)
    try:
        result = scraper.scrape(url, cats, max_pages=max_pages, delay=delay, render=render,
                                url_guard=validate_public_url)
    except ValueError as exc:
        return jsonify({"error": str(exc), "log": capture.lines}), 400
    except Exception as exc:
        logger.exception("Unexpected error")
        return jsonify({"error": f"Scrape failed: {type(exc).__name__}: {exc}", "log": capture.lines}), 500
    finally:
        logger.removeHandler(capture)
    result["columns"] = scraper.table_columns(result)
    result["log"] = capture.lines
    return jsonify(result)


if __name__ == "__main__":
    scraper.setup_logging()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
