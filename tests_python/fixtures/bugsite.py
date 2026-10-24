"""A tiny website with KNOWN, seeded defects, to measure the pipeline against ground truth.

A report is only worth something if it finds what is really broken and stays quiet about what is not. This site
has both: the bugs are listed in SEEDED so a test can check each one is found (recall), and /clean has none so a test
can check nothing is invented (precision).
"""
from __future__ import annotations
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HOME = """<!doctype html><html lang="en"><head><title>Seeded shop</title></head><body><main>
<h1>Seeded shop</h1>
<p>A demo site with a few deliberate faults.</p>
<nav><a href="/clean">Clean page</a> <a href="/gone">Old brochure</a> <a href="/boom">Broken report</a> <a href="/blank">Empty page</a></nav>
<img src="/missing.png" alt="Logo" width="40" height="40">
<button type="button" id="more" onclick="document.body.insertAdjacentHTML('beforeend','<p id=extra>More details</p>')">Show details</button>
<button type="button" id="fault" onclick="setTimeout(()=>{throw new Error('seeded script fault')},0)">Apply discount</button>
<button type="button" id="dead" onclick="location.href='/gone'">Open the manual</button>
<button type="button" id="del" onclick="document.title='deleted'">Delete my account</button>
</main></body></html>"""

CLEAN = """<!doctype html><html lang="en"><head><title>Clean page</title></head><body><main>
<h1>Clean page</h1><p>Nothing is wrong here.</p>
<a href="/">Home</a>
<label for="q">Search</label><input id="q" type="text">
<button type="button" id="ok" onclick="document.body.insertAdjacentHTML('beforeend','<p>Done</p>')">Show more</button>
</main></body></html>"""

# What each page should be reported for. "/clean" must produce no P1/P2 finding at all.
SEEDED = {
    "/": {"broken_link", "broken_image", "click_script_error", "click_error_page"},
    "/boom": {"page_http_error"},
    "/blank": {"blank_page"},
    "/clean": set(),
}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str = "", head: bool = False) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _route(self, head: bool) -> None:
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, HOME, head)
        elif path == "/clean":
            self._send(200, CLEAN, head)
        elif path == "/boom":
            self._send(500, "<html><body><h1>Internal Server Error</h1><p>The report could not be built.</p></body></html>", head)
        elif path == "/blank":
            self._send(200, "<!doctype html><html><head><title>Empty</title></head><body></body></html>", head)
        else:
            self._send(404, "<html><body><h1>Not found</h1></body></html>", head)

    def do_GET(self):
        self._route(False)

    def do_HEAD(self):
        self._route(True)

    def log_message(self, *args):
        pass


def serve() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
