"""Ground-truth benchmark for the no-model layer: every seeded fault is found, and the clean page is left alone."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import bugsite  # noqa: E402


def _measure(base: str) -> dict[str, list]:
    """Explore + sweep each page in a real browser (own thread: another module can leave a loop running)."""
    box: dict = {}

    def work():
        try:
            from playwright.sync_api import sync_playwright
            from website_test_pipeline.explorer import explore
            from website_test_pipeline.pagechecks import PageHealth, health_findings, sweep_findings
            from website_test_pipeline.sweep import sweep_page
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch()
                except Exception as exc:
                    box["skip"] = f"no chromium here: {exc}"
                    return
                out = {}
                for path in bugsite.SEEDED:
                    page = browser.new_page()
                    url = base + path
                    health = PageHealth(page, url)
                    page.goto(url, wait_until="domcontentloaded")
                    inv = explore(page, url, 3, None, health=health)
                    inv.swept = sweep_page(page, url, inv.controls, 12)
                    data = inv.__dict__
                    out[path] = health_findings(data) + sweep_findings(data)
                    page.close()
                browser.close()
                box["findings"] = out
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    if "skip" in box:
        pytest.skip(box["skip"])
    if "error" in box:
        raise box["error"]
    return box["findings"]


@pytest.fixture(scope="module")
def measured():
    server, base = bugsite.serve()
    try:
        yield _measure(base)
    finally:
        server.shutdown()


@pytest.mark.parametrize("path", [p for p, kinds in bugsite.SEEDED.items() if kinds])
def test_every_seeded_fault_is_found(measured, path):
    found = {f.kind for f in measured[path]}
    missing = bugsite.SEEDED[path] - found
    assert not missing, f"{path}: not found {sorted(missing)}; found {sorted(found)}"


def test_the_clean_page_gets_no_serious_finding(measured):
    serious = [(f.kind, f.severity) for f in measured["/clean"] if f.severity in {"P1", "P2"}]
    assert serious == [], f"false alarm(s) on a page with nothing wrong: {serious}"


def test_nothing_is_reported_beyond_the_seeded_faults_on_any_page(measured):
    extra = {path: sorted({f.kind for f in findings if f.severity in {"P1", "P2"}} - bugsite.SEEDED[path])
             for path, findings in measured.items()}
    assert all(not kinds for kinds in extra.values()), f"unexpected serious findings: {extra}"


def test_the_destructive_button_is_never_clicked(measured):
    # sweep skips "Delete my account"; if it had been clicked the page title would be 'deleted' and a later finding would not exist
    assert not any("Delete" in f.summary for f in measured["/"])
