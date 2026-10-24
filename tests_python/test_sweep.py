import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from website_test_pipeline.coverage import compute_coverage
from website_test_pipeline.pagechecks import sweep_findings
from website_test_pipeline.sweep import touched

PAGE = """<!doctype html><html lang="en"><head><title>Home</title></head><body><main>
<h1>Shop</h1>
<a href="/ok">Fine page</a> <a href="/gone">Dead page</a> <a href="https://elsewhere.invalid/x">Elsewhere</a> <a href="mailto:a@b.c">Mail</a>
<button id="reveal" type="button" onclick="document.body.insertAdjacentHTML('beforeend','<button id=extra type=button>Extra</button>')">Show more</button>
<button id="noop" type="button">Nothing</button>
<button id="boom" type="button" onclick="setTimeout(()=>{throw new Error('kaput')},0)">Breaks</button>
<button id="del" type="button" onclick="document.title='deleted'">Delete account</button>
<button id="err" type="button" onclick="location.href='/gone'">Go to dead end</button>
<select id="pick"><option value="">Choose</option><option value="a">Alpha</option></select>
<input id="who" type="text" aria-label="Who">
</main></body></html>"""


class _Site(BaseHTTPRequestHandler):
    def _send(self, code, body=b""):
        self.send_response(code); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        self._send({"/": 200, "/ok": 200}.get(self.path, 404), PAGE.encode() if self.path == "/" else b"<html><body>x</body></html>")

    def do_HEAD(self):
        self._send({"/": 200, "/ok": 200}.get(self.path, 404))

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    server = HTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def _in_browser(fn):
    """Run fn(page) in a worker thread with its own Playwright. Another test module in the full run can leave an
    asyncio loop running on the main thread, and Playwright's sync API refuses to start inside one; a fresh
    thread has no loop, so these tests behave the same alone and in the full suite."""
    box = {}

    def work():
        sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch()
                except Exception as exc:
                    box["skip"] = f"no chromium here: {exc}"
                    return
                try:
                    fn(browser.new_page())
                finally:
                    browser.close()
        except BaseException as exc:        # re-raised on the test thread so pytest reports it
            box["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    if "skip" in box:
        pytest.skip(box["skip"])
    if "error" in box:
        raise box["error"]


def _controls(page):
    from website_test_pipeline.explorer import _CONTROL_SEL, _CONTROLS_JS
    return page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)


def test_the_sweep_checks_links_clicks_safe_buttons_and_skips_destructive_ones(site):
    _in_browser(lambda page: _sweep_checks(page, site))


def _sweep_checks(page, site):
    from website_test_pipeline.sweep import sweep_page
    page.goto(site)
    swept = sweep_page(page, site, _controls(page), max_controls=10)
    by_name = {i["name"]: i for i in swept}
    assert by_name["Fine page"]["outcome"] == "ok"
    assert by_name["Dead page"]["outcome"] == "broken" and by_name["Dead page"]["status"] == 404
    assert "Elsewhere" not in by_name and "Mail" not in by_name            # external and mailto are left alone
    assert by_name["Show more"]["outcome"] == "reveals"
    assert by_name["Nothing"]["outcome"] == "no_visible_change"
    assert by_name["Breaks"]["errors"] and "kaput" in by_name["Breaks"]["errors"][0]
    assert by_name["Go to dead end"]["outcome"] == "navigates" and by_name["Go to dead end"]["status"] == 404
    assert "Delete account" not in by_name                                  # destructive: never clicked
    assert by_name["Who"]["outcome"] == "filled"


def test_a_disabled_sweep_does_nothing(site):
    def check(page):
        from website_test_pipeline.sweep import sweep_page
        page.goto(site)
        assert sweep_page(page, site, _controls(page), max_controls=0) == []
    _in_browser(check)


def test_findings_name_the_broken_link_the_error_page_and_the_script_error():
    inv = {"url": "https://shop.example.com/", "swept": [
        {"name": "Dead page", "kind": "link", "outcome": "broken", "status": 404, "href": "https://shop.example.com/gone"},
        {"name": "Fine", "kind": "link", "outcome": "ok", "status": 200},
        {"name": "Go", "kind": "button", "outcome": "navigates", "status": 500, "to": "https://shop.example.com/x"},
        {"name": "Breaks", "kind": "button", "outcome": "no_visible_change", "errors": ["Error: kaput"]}]}
    got = {(f.kind, f.severity) for f in sweep_findings(inv)}
    assert got == {("broken_link", "P2"), ("click_error_page", "P1"), ("click_script_error", "P2")}


def test_a_control_not_reached_is_not_counted_as_touched():
    assert touched({"outcome": "ok"}) and touched({"outcome": "reveals"})
    assert not touched({"outcome": "not_reached"}) and not touched({"outcome": "unreachable"})


def test_coverage_counts_swept_controls_separately_from_journey_touched_ones():
    inv = {"url": "https://shop.example.com/", "controls": [
        {"tag": "button", "name": "Show more", "selector": "#reveal", "region": "content"},
        {"tag": "button", "name": "Other", "selector": "#other", "region": "content"}],
        "swept": [{"name": "Show more", "selector": "#reveal", "kind": "button", "outcome": "reveals"},
                  {"name": "Other", "selector": "#other", "kind": "button", "outcome": "not_reached"}]}
    cov = compute_coverage([inv], [])
    page = cov.pages[0]
    assert (page.total, page.touched, page.swept, page.untouched) == (2, 0, 1, ["Other"])
    assert cov.controls_swept == 1 and cov.controls_touched == 0
