import json

import httpx
import pytest

from mcp_adapter.client import PipelineClient, PipelineError

REPORT = {"run_id": "run-1", "site": "https://x.test/", "recommendation": "GO",
          "confidence": {"level": "Partial", "note": "1 of 3 page(s) have no test result"},
          "conditions": ["1 journey(s) the run could not confirm either way (UNV-001), section 5."],
          "counts": {"executed_tests": 15, "passed": 15, "failed": 0, "skipped": 1, "pass_rate_percent": 100, "extra": 9},
          "defects": [{"id": "DEF-001", "category": "defect", "severity": "High", "status": "Open", "title": "Checkout dead",
                       "url": "https://x.test/c", "business_impact": "stops buying", "secret_field": "never passed on"},
                      {"id": "DEF-002", "category": "defect", "severity": "Medium", "status": "Closed", "title": "old", "url": "u",
                       "business_impact": "x"},
                      {"id": "UNV-001", "category": "unverified", "severity": "High", "status": "Open", "title": "j", "url": "u",
                       "business_impact": "x"}],
          "coverage": {"pages_total": 3, "pages_visited": 2, "controls_total": 14, "controls_touched": 3, "controls_swept": 10}}


def _api(calls, *, job_status="done", expire_once=False):
    state = {"expired": expire_once}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/login":
            body = json.loads(request.content)
            return httpx.Response(200, json={"authenticated": True}) if body["password"] == "pw" else httpx.Response(401, json={"detail": "Wrong username or password."})
        if state["expired"] and path.startswith("/api/jobs"):
            state["expired"] = False
            return httpx.Response(401, json={"detail": "Sign in first."})
        if path == "/api/jobs" and request.method == "POST":
            return httpx.Response(201, json={"id": "abc", "status": "queued", "echo": json.loads(request.content)})
        if path == "/api/jobs/abc":
            return httpx.Response(200, json={"id": "abc", "status": job_status, "stage": "report"})
        if path == "/api/jobs/abc/files":
            return httpx.Response(200, json=[{"path": "artifacts/report/full-report.docx", "size": 1}])
        if path == "/api/jobs/abc/files/artifacts/report/report-data.json":
            return httpx.Response(200, json=REPORT)
        return httpx.Response(404, json={"detail": "No such run."})
    return httpx.MockTransport(handler)


def _client(calls, **kw):
    return PipelineClient("http://pipeline.test", "me", "pw", transport=_api(calls, **kw))


def test_it_signs_in_once_then_reuses_the_session():
    calls = []
    client = _client(calls)
    client.run("abc"); client.run("abc")
    assert [c for c in calls if c[1] == "/api/login"] == [("POST", "/api/login")]


def test_a_wrong_password_is_a_clear_error_not_a_crash():
    client = PipelineClient("http://pipeline.test", "me", "nope", transport=_api([]))
    with pytest.raises(PipelineError, match="Sign-in failed .401.*Wrong username"):
        client.run("abc")


def test_it_refuses_to_start_without_an_account():
    with pytest.raises(PipelineError, match="PIPELINE_USERNAME"):
        PipelineClient("http://pipeline.test", transport=_api([])).run("abc")


def test_an_expired_session_signs_in_again_and_retries_once():
    calls = []
    assert _client(calls, expire_once=True).run("abc")["status"] == "done"
    assert [c[1] for c in calls].count("/api/login") == 2


def test_start_run_never_asks_a_person_by_default_because_an_agent_cannot_solve_a_captcha():
    sent = _client([]).start_run("https://x.test/", model_id="m1", max_pages=5)["echo"]
    assert sent == {"url": "https://x.test/", "model_id": "m1",
                    "options": {"ask_human": False, "reuse_workspace": False, "max_pages": 5}}


def test_an_api_error_carries_the_servers_own_reason():
    with pytest.raises(PipelineError, match="404: No such run"):
        _client([]).run("zzz")


def test_the_report_is_distilled_to_verdict_confidence_open_defects_and_counts():
    report = _client([]).report("abc")
    assert report["verdict"] == "GO" and report["confidence"]["level"] == "Partial"
    assert report["tests"] == {"executed_tests": 15, "passed": 15, "failed": 0, "skipped": 1, "pass_rate_percent": 100}
    assert [d["id"] for d in report["open_defects"]] == ["DEF-001"]            # closed and non-defect rows are left out
    assert "secret_field" not in report["open_defects"][0]
    assert report["other_items"]["unverified"] == 1 and report["coverage"]["controls_swept"] == 10
    assert report["files"] == ["artifacts/report/full-report.docx"]


def test_a_run_that_is_not_finished_has_no_report_and_says_why():
    with pytest.raises(PipelineError, match="is running .report"):
        _client([], job_status="running").report("abc")


def test_an_unreachable_api_is_reported_as_unreachable_with_a_hint_not_a_bare_failure():
    def refuse(request):
        raise httpx.ConnectError("refused")
    client = PipelineClient("http://pipeline.test", "me", "pw", transport=httpx.MockTransport(refuse))
    with pytest.raises(PipelineError, match="Could not reach the pipeline API at http://pipeline.test.*docker compose up"):
        client.run("abc")


def test_user_stories_are_sent_as_a_base64_markdown_file_and_omitted_when_blank():
    import base64
    with_stories = _client([]).start_run("https://x.test/", requirements="US-01: search works.")["echo"]
    (item,) = with_stories["requirements"]
    assert item["name"] == "user-stories.md" and base64.b64decode(item["data"]).decode() == "US-01: search works."
    assert "requirements" not in _client([]).start_run("https://x.test/", requirements="   ")["echo"]
