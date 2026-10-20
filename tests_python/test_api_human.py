import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.main import create_app, downloadable
from api.settings import AppSettings

CODE = "open-sesame"

# A stand-in for the pipeline that really uses the web channel: it asks a person, then records what it got.
# (Only key names and lengths are written for a login: the test then proves the password reached nothing else.)
FAKE_PIPELINE = r"""
import json, os
from pathlib import Path
from website_test_pipeline import authpopup, humanstep
from website_test_pipeline.authpopup import AuthRequest
from website_test_pipeline.humanstep import HumanRequest
site, data, url = os.environ["SITE"], Path(os.environ["DATA_DIR"]), os.environ["SEED_URL"]
art = data / "runs" / site / "artifacts"
art.mkdir(parents=True, exist_ok=True)
out = {}
ok, why = humanstep.can_ask()
if not ok:
    out["asked"] = False
elif "login" in url:
    fields = [{"type": "email", "name": "email", "label": "Email", "required": True},
              {"type": "password", "name": "pw", "label": "Password", "required": True}]
    got = authpopup.ask_credentials(AuthRequest(url, "login", fields, "first try"), lambda: b"SHOT")
    out["login"] = None if got is None else {"keys": sorted(got.values), "remember": got.remember, "pw_len": len(got.values["pw"])}
else:
    fields = [{"selector": "#n", "label": "Name", "type": "text"}]
    got = humanstep.ask_code(HumanRequest(url, "the code", fields=fields), lambda: b"IMG", lambda: None)
    out["captcha"] = None if got is None else {"code_len": len(got.code), "values": got.values}
(art / "asked.json").write_text(json.dumps(out))
"""


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    opened = []

    def make(**overrides):
        settings = AppSettings(require_model=False, data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", FAKE_PIPELINE],
                               poll_interval_s=0.05, idle_interval_s=0.05, **overrides)
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        assert client.post("/api/login", json={"code": CODE}).status_code == 200
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def wait_for(client, job_id, statuses, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job stayed {job['status']}: {job}")


def asked(tmp_path, job):
    return json.loads((tmp_path / "runs" / job["site"] / "artifacts" / "asked.json").read_text())


def everything_on_disk(tmp_path) -> bytes:
    return b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_a_captcha_is_put_to_the_person_answered_and_the_run_carries_on(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))

    question = client.get(f"/api/jobs/{job['id']}/human").json()["request"]
    assert question["kind"] == "captcha" and question["label"] == "the code" and question["can_reload"] is True
    assert question["fields"][0]["selector"] == "#n" and question["fields"][0]["default"] == "Test"
    assert question["expires_at"] > question["created_at"]
    image = client.get(question["image"])
    assert image.status_code == 200 and image.content == b"IMG" and image.headers["cache-control"] == "no-store"

    assert client.post(f"/api/jobs/{job['id']}/human", json={"action": "refresh"}).status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/human", json={"action": "submit", "code": "  "}).status_code == 422
    stray = client.post(f"/api/jobs/{job['id']}/human", json={"action": "submit", "code": "Xk39Q", "values": {"#other": "x"}})
    assert stray.status_code == 422
    sent = client.post(f"/api/jobs/{job['id']}/human", json={"action": "submit", "code": "Xk39Q", "values": {"#n": "Sam"}})
    assert sent.status_code == 200

    done = wait_for(client, job["id"], ("done",))
    assert done["progress_pct"] == 100
    assert asked(tmp_path, job) == {"captcha": {"code_len": 5, "values": {"#n": "Sam"}}}
    assert client.get(f"/api/jobs/{job['id']}/human").json() == {"request": None}
    assert client.post(f"/api/jobs/{job['id']}/human", json={"action": "skip"}).status_code == 404
    assert b"Xk39Q" not in everything_on_disk(tmp_path)               # the code never reached the database or a log


def test_sign_in_details_reach_the_run_once_and_are_stored_nowhere(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "login.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    question = client.get(f"/api/jobs/{job['id']}/human").json()["request"]
    assert question["kind"] == "credentials" and question["notice"] == "first try"
    assert [(f["key"], f["type"]) for f in question["fields"]] == [("email", "email"), ("pw", "password")]

    answer = {"action": "submit", "values": {"email": "me@x.test", "pw": "hunter-22-secret"}}
    assert client.post(f"/api/jobs/{job['id']}/human", json=answer).status_code == 200
    wait_for(client, job["id"], ("done",))

    assert asked(tmp_path, job) == {"login": {"keys": ["email", "pw"], "remember": False, "pw_len": 16}}
    assert b"hunter-22-secret" not in everything_on_disk(tmp_path)
    assert list((tmp_path / "runs" / job["site"] / "human").glob("answer-*")) == []


def test_skipping_tells_the_run_nobody_answered(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    assert client.post(f"/api/jobs/{job['id']}/human", json={"action": "skip"}).status_code == 200
    wait_for(client, job["id"], ("done",))
    assert asked(tmp_path, job) == {"captcha": None}


def test_a_run_waiting_for_a_person_can_be_cancelled(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert client.get(f"/api/jobs/{job['id']}/human").json() == {"request": None}
    assert client.post(f"/api/jobs/{job['id']}/human", json={"action": "skip"}).status_code == 404


def test_a_run_that_may_not_ask_never_waits_for_anyone(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com", "options": {"ask_human": False}}).json()
    wait_for(client, job["id"], ("done",))
    assert asked(tmp_path, job) == {"asked": False}


def test_the_question_folder_is_never_downloadable(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    run_dir = tmp_path / "runs" / job["site"]
    assert (run_dir / "human" / "request-1.json").is_file()
    assert downloadable(run_dir, "human/request-1.json") is None
    assert client.get(f"/api/jobs/{job['id']}/files/human/request-1.json").status_code == 404
    client.post(f"/api/jobs/{job['id']}/human", json={"action": "skip"})


def test_forgetting_the_saved_login_deletes_it_but_not_while_a_run_is_going(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    assert client.delete(f"/api/jobs/{job['id']}/session").status_code == 409
    client.post(f"/api/jobs/{job['id']}/human", json={"action": "skip"})
    wait_for(client, job["id"], ("done",))

    saved = tmp_path / "runs" / job["site"] / "auth"
    saved.mkdir()
    (saved / "state.default.json").write_text("{}")
    assert client.delete(f"/api/jobs/{job['id']}/session").json() == {"removed": True}
    assert not saved.exists()
    assert client.delete(f"/api/jobs/{job['id']}/session").json() == {"removed": False}


# ------------------------------------------------------------------------------------------------ what became of each question

def history(client, job):
    return client.get(f"/api/jobs/{job['id']}/human/history").json()


def wait_closed(client, job, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = history(client, job)
        if rows and rows[-1]["state"] == "closed":
            return rows[-1]
        time.sleep(0.1)
    raise AssertionError(history(client, job))


def test_the_page_can_see_a_question_move_from_waiting_to_sent_to_used_by_the_run(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    waiting = history(client, job)
    assert [(r["seq"], r["kind"], r["state"], r["outcome"]) for r in waiting] == [(1, "captcha", "open", "")]
    assert waiting[0]["url"] == "https://captcha.example.com/" and waiting[0]["answered_at"] is None
    client.post(f"/api/jobs/{job['id']}/human", json={"action": "submit", "code": "Xk39Q", "values": {"#n": "Sam"}})
    final = wait_closed(client, job)
    assert (final["state"], final["action"], final["outcome"]) == ("closed", "submit", "submitted") and final["answered_at"]
    wait_for(client, job["id"], ("done",))
    assert history(client, job) == [final]
    assert "Xk39Q" not in json.dumps(history(client, job)) and "Sam" not in json.dumps(history(client, job))   # never what was typed


def test_a_skipped_question_is_recorded_as_skipped(make_client):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    client.post(f"/api/jobs/{job['id']}/human", json={"action": "skip"})
    final = wait_closed(client, job)
    assert (final["action"], final["outcome"]) == ("skip", "skipped")


def test_a_question_nobody_answered_in_time_is_recorded_as_a_timeout(make_client, monkeypatch):
    monkeypatch.setenv("WTP_HUMAN_TIMEOUT_S", "10")          # the shortest the run allows
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    final = wait_closed(client, job, timeout=40)
    assert (final["action"], final["outcome"]) == ("", "timeout")


def test_cancelling_a_run_that_was_waiting_records_that_the_run_ended_first(make_client):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "captcha.example.com"}).json()
    wait_for(client, job["id"], ("needs_human",))
    client.post(f"/api/jobs/{job['id']}/cancel")
    rows = history(client, job)
    assert [(r["state"], r["outcome"]) for r in rows] == [("closed", "ended")]


def test_only_the_owner_or_an_administrator_can_read_a_runs_question_history(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    settings = AppSettings(require_model=False, data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", FAKE_PIPELINE],
                           poll_interval_s=0.05, idle_interval_s=0.05)
    pw = "correct horse battery"
    with TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"])) as admin:
        assert admin.post("/api/setup", json={"username": "root", "password": pw, "code": CODE}).status_code == 200
        for name in ("alice", "bob"):
            assert admin.post("/api/users", json={"username": name, "password": pw}).status_code == 201
        alice, bob = TestClient(admin.app), TestClient(admin.app)
        alice.post("/api/login", json={"username": "alice", "password": pw})
        bob.post("/api/login", json={"username": "bob", "password": pw})
        job = alice.post("/api/jobs", json={"url": "captcha.example.com"}).json()
        wait_for(alice, job["id"], ("needs_human",))
        assert len(history(alice, job)) == 1 and len(history(admin, job)) == 1
        assert bob.get(f"/api/jobs/{job['id']}/human/history").status_code == 404
        alice.post(f"/api/jobs/{job['id']}/cancel")


def test_a_database_from_before_outcomes_were_kept_is_upgraded_in_place(tmp_path):
    import sqlite3
    from api.db import Database
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT NOT NULL, site TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, stage TEXT NOT NULL DEFAULT '', progress_pct INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '', run_dir TEXT NOT NULL,
                options TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE human_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}', answer TEXT, state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,
                answered_at TEXT, UNIQUE (job_id, seq));
            INSERT INTO human_requests (job_id, seq, kind, payload, answer, state, created_at) VALUES ('j1', 1, 'captcha', '{"url": "https://x.test/"}', 'submit', 'closed', 't');
        """)
    db = Database(path)
    rows = db.human_history("j1")
    assert rows[0]["outcome"] == "" and rows[0]["answer"] == "submit" and rows[0]["payload"]["url"] == "https://x.test/"
    db.set_human_state("j1", 1, "closed", outcome="submitted")
    assert db.human_history("j1")[0]["outcome"] == "submitted" and db.human_history("j1")[0]["answer"] == "submit"      # left alone
    Database(path)                                                                                                        # opening twice is fine
