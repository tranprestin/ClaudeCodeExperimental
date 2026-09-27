"""Tiny local web server for the fixture sites (used by tests and demos).

    python tests/fixture_server.py 8765   # then scrape http://127.0.0.1:8765/table-1.html
"""
from __future__ import annotations

import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures"


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/cards":  # numbered pagination: /cards?page=N -> cards-N.html
            page = parse_qs(u.query).get("page", ["1"])[0]
            self.path = f"/cards-{page}.html"
        super().do_GET()

    def log_message(self, *args) -> None:  # keep test output quiet
        pass


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    srv, base = start(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"Serving fixtures at {base}  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
