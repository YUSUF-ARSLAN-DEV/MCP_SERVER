import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from website_test_pipeline import netguard


def test_private_loopback_link_local_and_metadata_addresses_are_refused_and_public_ones_allowed():
    for blocked in ("http://127.0.0.1:8000/", "http://localhost/", "http://10.1.2.3/", "http://192.168.0.5/x", "http://169.254.169.254/latest/meta-data/",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://printer.local/", "http://db.internal/"):
        assert not netguard.url_allowed(blocked, resolver=lambda h: []), blocked
    assert netguard.url_allowed("https://93.184.216.34/page")
    assert netguard.url_allowed("data:text/html,hi") and netguard.url_allowed("about:blank") and netguard.url_allowed("blob:https://x/1")
    assert not netguard.url_allowed("file:///etc/passwd") and not netguard.url_allowed("ftp://93.184.216.34/")


def test_a_public_name_that_resolves_to_a_private_address_is_refused():
    netguard._cache.clear()
    assert not netguard.host_allowed("sneaky.example", resolver=lambda host: ["93.184.216.34", "10.0.0.7"])
    netguard._cache.clear()
    assert netguard.host_allowed("fine.example", resolver=lambda host: ["93.184.216.34"])
    netguard._cache.clear()


def test_the_guard_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("WTP_BLOCK_PRIVATE", raising=False)
    assert not netguard.enabled()
    monkeypatch.setenv("WTP_BLOCK_PRIVATE", "1")
    assert netguard.enabled()


class _Secret(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Secret.hits += 1
        body = b"<html><body>internal secret</body></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)


def test_a_real_browser_cannot_be_sent_to_a_private_address_by_a_redirect_or_a_script(monkeypatch):
    """The page asked for is on 127.0.0.1 too (a test needs somewhere to stand), so the guard is pointed at it with a resolver
    that calls only the first host public: everything else on 127.0.0.1 is private and must never be fetched."""
    result = {}

    def work():
        try:
            from playwright.sync_api import sync_playwright
            secret = HTTPServer(("127.0.0.1", 0), _Secret)
            threading.Thread(target=secret.serve_forever, daemon=True).start()
            target = f"http://127.0.0.1:{secret.server_port}/"
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch()
                except Exception as exc:
                    result["skip"] = str(exc)
                    return
                context = browser.new_context()
                netguard.guard(context)
                page = context.new_page()
                page.set_content(f"<script>fetch('{target}').then(r => document.title = 'LEAKED').catch(() => document.title = 'blocked')</script>")
                page.wait_for_timeout(800)
                result["title"], result["hits"] = page.title(), _Secret.hits
                browser.close()
            secret.shutdown()
        except BaseException as exc:
            result["error"] = exc
    thread = threading.Thread(target=work)
    thread.start(); thread.join()
    if "skip" in result:
        pytest.skip(result["skip"])
    if "error" in result:
        raise result["error"]
    assert result["hits"] == 0 and result["title"] == "blocked"
