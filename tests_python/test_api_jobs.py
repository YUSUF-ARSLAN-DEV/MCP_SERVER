import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.db import Database
from api.main import create_app, downloadable
from api.settings import AppSettings

CODE = "open-sesame"

# A stand-in for `website_test_pipeline.cli all`: reads the same environment, writes the same files, exits like it.
FAKE_PIPELINE = r"""
import json, os, sys, time
from pathlib import Path
site, data, url = os.environ["SITE"], Path(os.environ["DATA_DIR"]), os.environ["SEED_URL"]
run = data / "runs" / site
art = run / "artifacts"
art.mkdir(parents=True, exist_ok=True)
def step(phase, fraction, note=""):
    with (art / "progress.jsonl").open("a") as h:
        h.write(json.dumps({"phase": phase, "fraction": fraction, "note": note}) + "\n")
(art / "env.json").write_text(json.dumps({"url": url, "pages": os.environ.get("CRAWL_MAX_PAGES"),
    "probe": os.environ.get("EXPLORE_PROBE_MAX"), "has_code": "ACCESS_CODE" in os.environ, "job": os.environ.get("WTP_JOB")}))
step("Explore", 0.1, "page 1")
if "slow" in url:
    time.sleep(60)
(art / "report").mkdir(exist_ok=True)
(art / "report" / "r.docx").write_bytes(b"docx-bytes")
(run / "auth").mkdir(exist_ok=True)
(run / "auth" / "state.storage_state.json").write_text("{}")
print("working", flush=True)
if "fail" in url:
    print("the model is unreachable", flush=True)
    sys.exit(2)
step("Report", 1.0, "done")
"""


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    opened = []

    def make(**overrides):
        settings = AppSettings(data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", FAKE_PIPELINE],
                               poll_interval_s=0.05, idle_interval_s=0.05, **overrides)
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def signed_in(client):
    assert client.post("/api/login", json={"code": CODE}).status_code == 200
    return client


def wait_for(client, job_id, statuses=("done", "failed", "cancelled"), timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job stayed {job['status']}: {job}")


def test_the_api_refuses_everything_without_the_access_code(make_client):
    client = make_client()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/jobs", json={"url": "example.com"}).status_code == 401
    assert client.post("/api/login", json={"code": "wrong"}).status_code == 401
    assert client.get("/api/jobs", headers={"x-access-code": CODE}).status_code == 200      # a script may send the code
    signed_in(client)
    assert client.get("/api/session").json()["authenticated"] is True
    assert client.get("/api/jobs").status_code == 200


def test_a_server_with_no_access_code_refuses_instead_of_standing_open(tmp_path):
    with TestClient(create_app(AppSettings(data_dir=tmp_path, access_code=""))) as client:
        assert client.get("/api/jobs").status_code == 503
        assert client.post("/api/login", json={"code": ""}).status_code == 503
        assert client.get("/api/health").status_code == 200


def test_too_many_wrong_codes_are_throttled(make_client):
    client = make_client()
    codes = [client.post("/api/login", json={"code": "nope"}).status_code for _ in range(10)]
    assert codes[:8] == [401] * 8 and codes[-1] == 429


def test_a_run_goes_from_queued_to_done_and_its_report_can_be_downloaded(make_client):
    client = signed_in(make_client())
    created = client.post("/api/jobs", json={"url": "example.com", "options": {"max_pages": 500, "probe_max": 2}})
    assert created.status_code == 201
    job = created.json()
    assert job["status"] == "queued" and job["url"] == "https://example.com/"

    done = wait_for(client, job["id"])
    assert done["status"] == "done" and done["progress_pct"] == 100 and done["started_at"] and done["finished_at"]
    assert (client.get("/api/jobs").json()[0]["id"]) == job["id"]

    files = client.get(f"/api/jobs/{job['id']}/files").json()
    assert [f["path"] for f in files] == ["artifacts/report/r.docx"]
    download = client.get(f"/api/jobs/{job['id']}/files/artifacts/report/r.docx")
    assert download.status_code == 200 and download.content == b"docx-bytes"


def test_the_pipeline_gets_the_job_settings_and_never_the_apis_secrets(make_client, tmp_path):
    client = signed_in(make_client())
    job = client.post("/api/jobs", json={"url": "example.com", "options": {"max_pages": 500, "probe_max": 2}}).json()
    wait_for(client, job["id"])
    env = json.loads((tmp_path / "runs" / job["site"] / "artifacts" / "env.json").read_text())
    assert env == {"url": "https://example.com/", "pages": "25", "probe": "2", "has_code": False, "job": "1"}


def test_every_fresh_run_gets_its_own_folder_and_reuse_keeps_the_sites_folder(make_client):
    client = signed_in(make_client())
    first = client.post("/api/jobs", json={"url": "example.com"}).json()
    wait_for(client, first["id"])
    second = client.post("/api/jobs", json={"url": "example.com", "options": {"reuse_workspace": True}}).json()
    assert first["site"] != second["site"] and first["site"].startswith("example.com-") and second["site"] == "example.com"


def test_a_failed_run_says_why(make_client):
    client = signed_in(make_client())
    job = client.post("/api/jobs", json={"url": "fail.example.com"}).json()
    failed = wait_for(client, job["id"])
    assert failed["status"] == "failed" and "the model is unreachable" in failed["error"]


def test_addresses_on_the_servers_own_network_are_refused_with_a_reason(make_client):
    client = signed_in(make_client())
    for url in ("http://127.0.0.1/", "http://localhost:8000", "http://169.254.169.254/latest/meta-data", "ftp://example.com"):
        response = client.post("/api/jobs", json={"url": url})
        assert response.status_code == 422 and response.json()["detail"], url
    assert client.get("/api/jobs").json() == []


def test_one_run_at_a_time_per_person_and_cancel_frees_them(make_client):
    client = signed_in(make_client())
    slow = client.post("/api/jobs", json={"url": "slow.example.com"}).json()
    wait_for(client, slow["id"], statuses=("running",))
    assert client.post("/api/jobs", json={"url": "example.com"}).status_code == 409

    cancelled = client.post(f"/api/jobs/{slow['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    time.sleep(0.5)                                                         # the killed run must not overwrite it as failed
    assert client.get(f"/api/jobs/{slow['id']}").json()["status"] == "cancelled"
    assert client.post("/api/jobs", json={"url": "example.com"}).status_code == 201


def test_the_daily_limit_stops_a_sixth_run(make_client):
    client = signed_in(make_client(max_jobs_per_day=1))
    first = client.post("/api/jobs", json={"url": "example.com"}).json()
    wait_for(client, first["id"])
    assert client.post("/api/jobs", json={"url": "example.com"}).status_code == 429


def test_a_run_over_the_time_limit_is_stopped_and_marked_failed(make_client):
    client = signed_in(make_client(job_timeout_s=1))
    job = client.post("/api/jobs", json={"url": "slow.example.com"}).json()
    failed = wait_for(client, job["id"])
    assert failed["status"] == "failed" and "time limit" in failed["error"]


def test_the_event_stream_ends_with_the_final_state(make_client):
    client = signed_in(make_client())
    job = client.post("/api/jobs", json={"url": "example.com"}).json()
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        payloads = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    assert payloads[-1]["status"] == "done" and payloads[-1]["id"] == job["id"]


def test_a_visitor_can_never_download_a_saved_login_or_leave_the_run_folder(make_client, tmp_path):
    client = signed_in(make_client())
    job = client.post("/api/jobs", json={"url": "example.com"}).json()
    wait_for(client, job["id"])
    run_dir = tmp_path / "runs" / job["site"]
    assert (run_dir / "auth" / "state.storage_state.json").is_file()

    assert client.get(f"/api/jobs/{job['id']}/files/auth/state.storage_state.json").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/files/%2e%2e/%2e%2e/app.db").status_code == 404
    assert downloadable(run_dir, "../../app.db") is None
    assert downloadable(run_dir, "auth/state.storage_state.json") is None
    assert downloadable(run_dir, "artifacts/report/r.docx") is not None
    assert client.get("/api/jobs/doesnotexist/files/x").status_code == 404


def test_a_restart_marks_runs_that_were_in_progress_as_failed(tmp_path):
    db = Database(tmp_path / "app.db")
    db.create_job("a1", "https://x.test/", "x.test-a1", tmp_path / "runs" / "a1", "u", {})
    db.create_job("b2", "https://y.test/", "y.test-b2", tmp_path / "runs" / "b2", "u", {})
    assert db.claim_next()["id"] == "a1"
    assert db.recover_interrupted() == 1
    assert db.get_job("a1")["status"] == "failed" and "restarted" in db.get_job("a1")["error"]
    assert db.get_job("b2")["status"] == "queued"
    db.finish_job("a1", "done")                                              # an ended job is never overwritten
    assert db.get_job("a1")["status"] == "failed"
