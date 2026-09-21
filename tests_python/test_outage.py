import json
import logging
import socket
from types import SimpleNamespace

from website_test_pipeline import runner
from website_test_pipeline.flowgen import file_name
from website_test_pipeline.flowresults import apply_pytest_results, match_results
from website_test_pipeline.runner import is_outage, run_flow, run_verify


def test_the_browsers_network_errors_are_recognised_as_an_outage_and_page_errors_are_not():
    for text in ("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x.test", "net::ERR_CONNECTION_REFUSED", "net::ERR_INTERNET_DISCONNECTED",
                 "net::ERR_CONNECTION_TIMED_OUT", "net::ERR_ADDRESS_UNREACHABLE", "net::ERR_PROXY_CONNECTION_FAILED"):
        assert is_outage(text), text
    for text in ("control not found", "Timeout 2000ms exceeded", "expected url to match", "", None, "net::ERR_CERT_DATE_INVALID"):
        assert not is_outage(text), text


def test_a_start_page_that_cannot_be_reached_is_flagged_unreachable(monkeypatch):
    class _Page:
        def goto(self, *a, **k):
            raise RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://gone.test/")

    result = run_flow(_Page(), {"id": "f", "start_url": "https://gone.test/", "steps": [{"kind": "click", "name": "Go"}]})
    assert result["unreachable"] is True and not result["ok"] and "could not open start page" in result["error"]

    class _Broken:
        def goto(self, *a, **k):
            raise RuntimeError("Page.goto: net::ERR_ABORTED")

    assert run_flow(_Broken(), {"id": "f", "start_url": "u", "steps": []}).get("unreachable") is False


def test_pytest_failures_caused_by_an_outage_are_not_recorded_against_the_flow():
    flow = {"id": "x--a", "status": "verified", "start_url": "https://x.test/", "steps": []}
    other = {"id": "x--b", "status": "verified", "start_url": "https://x.test/", "steps": []}
    rows = {"finished_at": "t", "tests": [
        {"nodeid": f"t/{file_name(flow)}::test[chromium]", "status": "failed", "error": "E   Error: net::ERR_NAME_NOT_RESOLVED at https://x.test/"},
        {"nodeid": f"t/{file_name(other)}::test[chromium]", "status": "failed", "error": "E   AssertionError: Page URL expected"}]}
    assert list(match_results(rows, [flow, other])) == ["x--b"]                          # the outage row is ignored
    doc, ratings = {"flows": [flow, other]}, {"ratings": {}}
    apply_pytest_results(doc, ratings, rows, "now")
    assert "x--a" not in ratings["ratings"] and len(ratings["ratings"]["x--b"]) == 1
    assert doc["flows"][0]["status"] == "verified"


def test_a_passing_row_is_never_dropped_because_of_the_outage_filter():
    flow = {"id": "x--a", "status": "verified", "start_url": "u", "steps": []}
    rows = {"tests": [{"nodeid": f"t/{file_name(flow)}::t", "status": "passed", "error": "net::ERR_NAME_NOT_RESOLVED"}]}
    assert list(match_results(rows, [flow])) == ["x--a"]


def test_verify_against_an_unreachable_site_changes_and_records_nothing(tmp_path):
    """Real browser, real refused connection: a port that nothing listens on."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                                                                          # now nothing listens on it
    flow = {"id": "demo", "status": "verified", "source": "explorer", "goal": "g", "start_url": f"http://127.0.0.1:{port}/",
            "outcome": {"effect": "navigates"}, "steps": [{"kind": "click", "selector": None, "name": "Go"}]}
    flows = tmp_path / "flows.json"
    flows.write_text(json.dumps({"version": 1, "flows": [flow]}), encoding="utf-8")
    ratings = tmp_path / "ratings.json"
    settings = SimpleNamespace(flows_file=flows, ratings_file=ratings, headless=True, navigation_timeout_ms=5000,
                               intents_file=tmp_path / "i.json")
    code = run_verify(settings, logging.getLogger("t"))
    assert code == 3
    assert json.loads(flows.read_text(encoding="utf-8"))["flows"][0]["status"] == "verified"    # not demoted
    assert not ratings.exists()                                                                  # no failed run recorded
