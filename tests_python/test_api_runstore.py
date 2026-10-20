"""A run's summary and files are kept in the database; the run's scratch folder is deleted once they are safely in."""
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import runstore
from api.main import create_app
from api.settings import AppSettings


PW = "correct horse battery"
KEY = "sk-live-VERY-SECRET-key-123456"

FAKE = r"""
import json, os, sys, time
from pathlib import Path
url = os.environ["SEED_URL"]
run = Path(os.environ["DATA_DIR"]) / "runs" / os.environ["SITE"]
art = run / "artifacts"
(art / "report").mkdir(parents=True, exist_ok=True)
(art / "evidence" / "t1").mkdir(parents=True, exist_ok=True)
(art / "report" / "full-report.docx").write_bytes(b"PK-docx-bytes")
report = {"recommendation": "CONDITIONAL GO", "run_id": "run-1", "conditions": ["c"],
          "counts": {"executed_tests": 10, "passed": 8, "failed": 2, "pass_rate_percent": 80, "journeys_total": 2, "journeys_passed": 1, "open_defects": 1},
          "requirements": {"total": 4, "not_backed": 3}, "defects": [{"id": "DEF-001", "title": "t"}], "taxonomy": {"defect": 1},
          "environment": {"Browser": "Chromium", "Browser version": "130.0", "Playwright": "1.63"}}
(art / "report" / "report-data.json").write_text(json.dumps(report) + " " * 6000)
(art / "evidence" / "t1" / "01.png").write_bytes(b"\x89PNG-screenshot-bytes")
(art / "a.inventory.json").write_text("{}")
(art / "b.inventory.json").write_text("{}")
(run / "tests").mkdir(exist_ok=True)
(run / "tests" / "a_test.py").write_text("def test_x():\n    pass\n")
(run / "tests" / "__pycache__").mkdir(exist_ok=True)
(run / "tests" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"pyc")
(run / "auth").mkdir(exist_ok=True)
(run / "auth" / "state.default.json").write_text('{"cookies": "SECRET-SESSION-TOKEN"}')
(art / "x.storage_state.json").write_text("SECRET-STORAGE-STATE")
(run / ".env").write_text("API_KEY=SECRET-ENV-KEY")
(run / "human").mkdir(exist_ok=True)
(run / "human" / "answer-1.json").write_text('{"action": "submit", "code": "SECRET-TYPED-CODE"}')      # where a typed answer really goes
(run / "tool.exe").write_bytes(b"MZ")
if "huge" in url:
    (art / "evidence" / "t1" / "huge.png").write_bytes(b"x" * (2 * 1024 * 1024))
if "fail" in url:
    print("the model is unreachable", flush=True)
    sys.exit(2)
if "slow" in url:
    time.sleep(60)
"""


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    opened = []

    def make(data_dir=None, setup=True, **overrides):
        settings = AppSettings(**{"data_dir": data_dir or tmp_path, "command": [sys.executable, "-c", FAKE], "poll_interval_s": 0.05,
                                  "idle_interval_s": 0.05, "max_jobs_per_day": 50, "require_model": False, "store_files_in_db": True, **overrides})
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        if setup:
            assert client.post("/api/signup", json={"username": "root", "password": PW}).status_code == 200
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def person(admin, name):
    browser = TestClient(admin.app)
    assert browser.post("/api/signup", json={"username": name, "password": PW}).status_code == 200
    return browser


def run(client, url="example.com", **body):
    job = client.post("/api/jobs", json={"url": url, **body}).json()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        now = client.get(f"/api/jobs/{job['id']}").json()
        if now["status"] in ("done", "failed", "cancelled"):
            return now
        time.sleep(0.05)
    raise AssertionError(now)


def stored_paths(tmp_path, job_id):
    with sqlite3.connect(tmp_path / "app.db") as conn:
        return [r[0] for r in conn.execute("SELECT path FROM run_files WHERE job_id = ? ORDER BY path", (job_id,))]


# ------------------------------------------------------------------------------------------------ what a run was

def test_a_finished_run_carries_a_summary_of_what_it_came_to(make_app):
    client = make_app()
    job = run(client, body_model := None) if False else run(client, model={"provider": "openai", "model": "qwen-x", "api_key": "k",
                                                                            "api_url": "https://llm.example.org/v1/chat/completions"})
    s = job["summary"]
    assert (s["verdict"], s["tests_ran"], s["tests_passed"], s["tests_failed"], s["pass_rate"]) == ("CONDITIONAL GO", 10, 8, 2, 80)
    assert (s["journeys_passed"], s["journeys_total"], s["open_defects"], s["findings_total"]) == (1, 2, 1, 1)
    assert s["model_name"] == "qwen-x" and s["code_version"] and isinstance(s["duration_s"], int) and s["duration_s"] >= 0
    assert s["files_stored"] > 0 and s["bytes_stored"] > 0
    listed = client.get("/api/jobs").json()[0]
    assert listed["summary"]["verdict"] == "CONDITIONAL GO"


def test_the_full_record_names_the_model_options_code_version_browser_and_exit_code(make_app):
    client = make_app()
    job = run(client, options={"max_pages": 7, "probe_max": 2},
              model={"provider": "google", "model": "gemini-x", "api_key": KEY})
    info = client.get(f"/api/jobs/{job['id']}/info").json()
    row = info["info"]
    assert info["stored_in_database"] is True and info["job"]["id"] == job["id"]
    assert (row["model_provider"], row["model_name"], row["exit_code"], row["pages_explored"], row["browser"]) == ("google", "gemini-x", 0, 2, "Chromium 130.0")
    assert (row["requirements_total"], row["requirements_backed"]) == (4, 1)
    assert row["details"]["options"]["max_pages"] == 7 and row["details"]["taxonomy"] == {"defect": 1}
    assert KEY not in json.dumps(info)                                         # no key anywhere in what is recorded


def test_a_run_that_is_still_going_has_no_summary_yet(make_app):
    client = make_app()
    job = client.post("/api/jobs", json={"url": "slow.example.com"}).json()
    deadline = time.monotonic() + 10
    while client.get(f"/api/jobs/{job['id']}").json()["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.get(f"/api/jobs/{job['id']}").json()["summary"] is None
    assert client.get(f"/api/jobs/{job['id']}/info").json()["info"] is None
    client.post(f"/api/jobs/{job['id']}/cancel")


def test_a_failed_run_keeps_its_reason_and_its_log(make_app, tmp_path):
    client = make_app()
    job = run(client, "fail.example.com")
    assert job["status"] == "failed" and "the model is unreachable" in job["error"]          # read before the folder was deleted
    assert "job.log" in stored_paths(tmp_path, job["id"])
    assert b"the model is unreachable" in client.get(f"/api/jobs/{job['id']}/files/job.log").content
    assert job["summary"]["exit_code"] == 2


# ------------------------------------------------------------------------------------------------ what it produced

def test_the_files_move_into_the_database_and_the_scratch_folder_is_deleted(make_app, tmp_path):
    client = make_app()
    job = run(client)
    assert not (tmp_path / "runs" / job["site"]).exists()
    paths = stored_paths(tmp_path, job["id"])
    assert {"artifacts/report/full-report.docx", "artifacts/report/report-data.json", "artifacts/evidence/t1/01.png",
            "artifacts/a.inventory.json", "tests/a_test.py", "job.log"} <= set(paths)
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"


def test_files_come_back_byte_for_byte_through_the_same_endpoints(make_app):
    client = make_app()
    job = run(client)
    base = f"/api/jobs/{job['id']}/files"
    assert [f["path"] for f in client.get(base).json()] == ["artifacts/report/full-report.docx", "artifacts/report/report-data.json"]
    docx = client.get(base + "/artifacts/report/full-report.docx")
    assert docx.content == b"PK-docx-bytes" and "full-report.docx" in docx.headers["content-disposition"] and "attachment" in docx.headers["content-disposition"]
    data = client.get(base + "/artifacts/report/report-data.json")
    assert data.headers["content-type"].startswith("application/json") and data.json()["recommendation"] == "CONDITIONAL GO"
    assert client.get(base + "/artifacts/evidence/t1/01.png").content == b"\x89PNG-screenshot-bytes"
    assert client.get(base + "/artifacts/evidence/t1/01.png").headers["content-type"] == "image/png"


def test_text_is_compressed_in_the_database_and_nothing_is_lost(make_app, tmp_path):
    client = make_app()
    job = run(client)
    with sqlite3.connect(tmp_path / "app.db") as conn:
        size, stored, encoding, sha = conn.execute("SELECT size, stored_size, encoding, sha256 FROM run_files WHERE path = 'artifacts/report/report-data.json'").fetchone()
        png_encoding = conn.execute("SELECT encoding FROM run_files WHERE path = 'artifacts/evidence/t1/01.png'").fetchone()[0]
    assert encoding == "zlib" and stored < size / 3 and png_encoding == "raw"                 # already-compressed types are left alone
    content = client.get(f"/api/jobs/{job['id']}/files/artifacts/report/report-data.json").content
    assert len(content) == size and hashlib.sha256(content).hexdigest() == sha                # the recorded hash matches what comes back


def test_the_data_survives_a_restart_of_the_server(make_app, tmp_path):
    first = make_app()
    job = run(first)
    first.__exit__(None, None, None)
    second = make_app(setup=False)
    assert second.post("/api/login", json={"username": "root", "password": PW}).status_code == 200
    assert second.get(f"/api/jobs/{job['id']}/files/artifacts/report/full-report.docx").content == b"PK-docx-bytes"
    assert second.get("/api/jobs").json()[0]["summary"]["verdict"] == "CONDITIONAL GO"


# ------------------------------------------------------------------------------------------------ what is never kept

def test_saved_logins_keys_typed_answers_and_env_files_are_never_stored(make_app, tmp_path):
    client = make_app()
    job = run(client, model={"provider": "google", "model": "gemini-x", "api_key": KEY})
    paths = stored_paths(tmp_path, job["id"])
    assert not [p for p in paths if p.split("/")[0] in {"auth", "secrets", "human"} or p.endswith((".storage_state.json", ".env")) or "__pycache__" in p]
    everything = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".key")
    for secret in (b"SECRET-SESSION-TOKEN", b"SECRET-STORAGE-STATE", b"SECRET-ENV-KEY", b"SECRET-TYPED-CODE", KEY.encode()):
        assert secret not in everything, secret                                                  # not in the database, not on disk anywhere


def test_a_type_that_is_not_kept_is_listed_as_left_out_with_the_reason(make_app, tmp_path):
    client = make_app()
    job = run(client)
    omitted = client.get(f"/api/jobs/{job['id']}/info").json()["info"]["details"]["omitted"]
    assert {"path": "tool.exe", "size": 2, "reason": "type not kept"} in omitted
    assert "tool.exe" not in stored_paths(tmp_path, job["id"])


def test_a_file_over_the_limit_is_left_out_and_says_so(make_app, tmp_path):
    client = make_app(max_stored_file_mb=1)
    job = run(client, "huge.example.com")
    info = client.get(f"/api/jobs/{job['id']}/info").json()["info"]
    assert any(o["path"].endswith("huge.png") and "larger than 1 MB" in o["reason"] for o in info["details"]["omitted"])
    assert info["files_omitted"] >= 2 and "artifacts/evidence/t1/huge.png" not in stored_paths(tmp_path, job["id"])
    assert "artifacts/report/full-report.docx" in stored_paths(tmp_path, job["id"])             # the important ones are unaffected


def test_when_a_run_hits_its_size_limit_the_report_is_kept_before_the_big_evidence(make_app, tmp_path):
    client = make_app(max_stored_run_mb=1)
    job = run(client, "huge.example.com")
    paths = stored_paths(tmp_path, job["id"])
    assert "artifacts/report/full-report.docx" in paths and "artifacts/report/report-data.json" in paths


# ------------------------------------------------------------------------------------------------ who may read it

def test_what_a_visitor_may_read_is_the_same_whether_it_is_on_disk_or_in_the_database(make_app, tmp_path):
    client = make_app()
    job = run(client)
    base = f"/api/jobs/{job['id']}/files/"
    assert client.get(base + "tests/a_test.py").status_code == 404                  # stored, but a generated spec is not offered for download
    for bad in ("../app.db", "artifacts/../../app.db", "%2e%2e/app.db", "/etc/passwd", "artifacts\\..\\..\\app.db", "C:/Windows/win.ini",
                "auth/state.default.json", "human/answer-1.json", "secrets/model.json", ".env", "tool.exe", "artifacts/nope.png"):
        assert client.get(base + bad).status_code == 404, bad


def test_clean_relative_is_strict():
    ok = runstore.clean_relative
    assert ok("artifacts/report/full-report.docx") == "artifacts/report/full-report.docx" and ok("a\\b.json") == "a/b.json"
    for bad in ("", ".", "..", "../x.json", "a/../../x.json", "/x.json", "C:/x.json", "a\x00.json", "auth/x.json", "x/secrets/y.json",
                "y.storage_state.json", "run.py", "noext", "a/.env"):
        assert ok(bad) is None, bad


def test_only_the_person_who_ran_it_can_read_a_stored_run(make_app):
    first = make_app()
    alice, bob = person(first, "alice"), person(first, "bob")
    job = run(alice)
    base = f"/api/jobs/{job['id']}"
    for path in ("/info", "/files", "/files/artifacts/report/full-report.docx", "/files/job.log"):
        assert alice.get(base + path).status_code == 200, path
        assert bob.get(base + path).status_code == 404, path
        assert first.get(base + path).status_code == 404, path                    # the very first account is just another person
    assert bob.get("/api/jobs").json() == [] and first.get("/api/jobs").json() == []


# ------------------------------------------------------------------------------------------------ safety and the past

def test_a_failure_while_storing_never_fails_the_run_and_its_files_stay_where_they_are(make_app, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr("api.jobs.store_run", boom)
    client = make_app()
    job = run(client)
    assert job["status"] == "done" and (tmp_path / "runs" / job["site"] / "artifacts" / "report" / "full-report.docx").is_file()
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"
    assert client.get(f"/api/jobs/{job['id']}/files/artifacts/report/full-report.docx").content == b"PK-docx-bytes"     # served from disk


def test_runs_from_before_this_existed_are_moved_into_the_database_when_the_server_starts(tmp_path, monkeypatch):
    from api.db import Database
    db = Database(tmp_path / "app.db")
    old = tmp_path / "runs" / "old-run"
    (old / "artifacts" / "report").mkdir(parents=True)
    (old / "artifacts" / "report" / "report-data.json").write_text(json.dumps({"recommendation": "GO", "counts": {"executed_tests": 3, "passed": 3, "failed": 0, "pass_rate_percent": 100}}))
    (old / "auth").mkdir()
    (old / "auth" / "state.json").write_text("SECRET-OLD-LOGIN")
    db.create_job("oldjob000001", "https://old.test/", "old-run", old, "someone", {})
    db.finish_job("oldjob000001", "done")
    settings = AppSettings(data_dir=tmp_path, require_model=False, store_files_in_db=True, command=[sys.executable, "-c", "pass"],
                           poll_interval_s=0.05, idle_interval_s=0.05)
    with TestClient(create_app(settings)) as client:
        deadline = time.monotonic() + 15
        while not db.get_job("oldjob000001")["stored"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert db.get_job("oldjob000001")["stored"] == 1 and not old.exists()
        assert db.get_run_info("oldjob000001")["verdict"] == "GO"
        assert stored_paths(tmp_path, "oldjob000001") == ["artifacts/report/report-data.json"]
        assert b"SECRET-OLD-LOGIN" not in b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_with_storage_turned_off_files_stay_on_disk_exactly_as_before(make_app, tmp_path):
    client = make_app(store_files_in_db=False)
    job = run(client)
    assert (tmp_path / "runs" / job["site"] / "artifacts" / "report" / "full-report.docx").is_file()
    assert stored_paths(tmp_path, job["id"]) == [] and job["summary"]["verdict"] == "CONDITIONAL GO"        # the summary is still recorded
    assert client.get(f"/api/jobs/{job['id']}/files/artifacts/report/full-report.docx").content == b"PK-docx-bytes"


def test_a_container_with_no_git_history_reports_the_version_it_was_built_from(monkeypatch):
    runstore.code_version.cache_clear()
    monkeypatch.setenv("CODE_VERSION", "abc1234")
    assert runstore.code_version() == "abc1234"
    runstore.code_version.cache_clear()
    monkeypatch.setenv("CODE_VERSION", "unknown")                       # the Dockerfile's default when nothing was passed
    assert runstore.code_version() != "abc1234"                         # falls back to asking git
    runstore.code_version.cache_clear()
