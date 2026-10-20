"""Accounts: there are no administrators and no setup code. Anyone creates an account and signs in; everyone is just a user,
and each user sees and controls only their own runs."""
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from authhelp import PW, another_person, sign_up
from api.db import Database
from api.main import create_app
from api.settings import AppSettings

FAKE_PIPELINE = r"""
import json, os, time
from pathlib import Path
run = Path(os.environ["DATA_DIR"]) / "runs" / os.environ["SITE"]
(run / "artifacts" / "report").mkdir(parents=True, exist_ok=True)
(run / "artifacts" / "report" / "r.docx").write_bytes(b"private-report")
(run / "artifacts" / "env.json").write_text(json.dumps(dict(os.environ)))
if "slow" in os.environ["SEED_URL"]:
    time.sleep(60)
"""


@pytest.fixture
def make_app(tmp_path):
    opened = []

    def make(**overrides):
        settings = AppSettings(**{"data_dir": tmp_path, "command": [sys.executable, "-c", FAKE_PIPELINE], "poll_interval_s": 0.05,
                                  "idle_interval_s": 0.05, "max_jobs_per_day": 50, "require_model": False, **overrides})
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def wait_for(client, job_id, statuses=("done", "failed", "cancelled"), timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(job)


# ------------------------------------------------------------------------------------------------ there is no admin

def test_there_is_no_setup_step_no_access_code_and_no_user_management(make_app):
    client = make_app()
    state = client.get("/api/session").json()
    assert state["authenticated"] is False and state["user"] is None
    assert not {"setup_needed", "needs_code", "accounts", "signup", "open"} & set(state)        # nothing about a first account or a shared code
    for method, path in (("post", "/api/setup"), ("get", "/api/users"), ("post", "/api/users"), ("patch", "/api/users/x")):
        assert getattr(client, method)(path).status_code in (404, 405), path
    assert client.post("/api/login", json={"code": "anything"}).status_code == 422             # a code is not a way in


def test_the_database_has_no_roles_and_a_database_from_before_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member', disabled INTEGER NOT NULL DEFAULT 0, session_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, last_login_at TEXT);
            INSERT INTO users (id, username, password_hash, role, created_at) VALUES ('u1', 'old_admin', 'h', 'admin', 't');
        """)
    db = Database(path)
    with sqlite3.connect(path) as conn:
        assert [r[1] for r in conn.execute("PRAGMA table_info(users)")] == ["id", "username", "password_hash", "session_version", "created_at", "last_login_at"]
    assert db.get_user("u1")["username"] == "old_admin"                                          # the person is kept; only the role is gone
    fresh = Database(tmp_path / "new.db")
    with sqlite3.connect(tmp_path / "new.db") as conn:
        assert "role" not in [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    assert fresh.get_user("nobody") is None


# ------------------------------------------------------------------------------------------------ creating an account and signing in

def test_creating_an_account_signs_you_in_and_says_nothing_about_roles(make_app):
    client = make_app()
    done = client.post("/api/signup", json={"username": "Dana", "password": PW})
    assert done.status_code == 200 and done.json()["user"]["username"] == "dana" and "role" not in done.text
    assert PW not in done.text and "hash" not in done.text
    state = client.get("/api/session").json()
    assert state["authenticated"] and state["user"]["username"] == "dana" and set(state["user"]) == {"id", "username"}


@pytest.mark.parametrize("name,pw,reason", [
    ("ab", PW, "username"), ("has space", PW, "username"), ("../etc", PW, "username"),
    ("dana12", "short", "at least"), ("dana12dana", "dana12dana", "same as the username"),
    ("dana12", "aaaaaaaaaaaa", "repetitive"), ("dana12", "x" * 300, "at most")])
def test_a_bad_username_or_password_is_refused_with_a_reason(make_app, name, pw, reason):
    r = make_app().post("/api/signup", json={"username": name, "password": pw})
    assert r.status_code == 422 and reason in r.text


def test_a_taken_username_is_refused_whatever_its_capitals(make_app):
    client = make_app()
    sign_up(client, "dana")
    assert TestClient(client.app).post("/api/signup", json={"username": "DANA", "password": PW}).status_code == 409


def test_signing_in_works_and_a_wrong_password_looks_the_same_as_an_unknown_user(make_app):
    client = make_app()
    sign_up(client, "alice")
    ok = TestClient(client.app).post("/api/login", json={"username": "ALICE", "password": PW})
    assert ok.status_code == 200 and ok.json()["user"]["username"] == "alice"
    wrong = TestClient(client.app).post("/api/login", json={"username": "alice", "password": "wrong-password-1"})
    unknown = TestClient(client.app).post("/api/login", json={"username": "nobody", "password": PW})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()


def test_only_failed_attempts_count_so_a_team_on_one_address_is_never_locked_out_by_signing_in(make_app):
    client = make_app(signups_per_address_per_hour=100)
    for name in ("alice", "bob", "carol"):
        another_person(client, name)
    for _ in range(4):                                                    # twelve good sign-ins from one address in a minute
        for name in ("alice", "bob", "carol"):
            assert TestClient(client.app).post("/api/login", json={"username": name, "password": PW}).status_code == 200
    codes = [TestClient(client.app).post("/api/login", json={"username": "alice", "password": f"wrong-guess-{i}"}).status_code for i in range(12)]
    assert codes[:6] == [401] * 6 and 429 in codes                        # guessing is stopped after a handful of failures
    assert TestClient(client.app).post("/api/login", json={"username": "alice", "password": PW}).status_code == 429     # even the right password waits


def test_a_tampered_or_foreign_session_cookie_is_refused(make_app):
    client = make_app()
    forged = TestClient(client.app)
    forged.cookies.set("wtp_session", "u.someone.0.9999999999.deadbeef")
    assert forged.get("/api/jobs").status_code == 401
    assert TestClient(client.app).get("/api/jobs").status_code == 401


def test_signing_out_clears_the_session(make_app):
    client = sign_up(make_app(), "alice")
    assert client.get("/api/jobs").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/jobs").status_code == 401


def test_changing_your_password_ends_your_other_sessions_but_not_this_one(make_app):
    app = make_app()
    sign_up(app, "alice")
    laptop, phone = TestClient(app.app), TestClient(app.app)
    for browser in (laptop, phone):
        assert browser.post("/api/login", json={"username": "alice", "password": PW}).status_code == 200
    assert laptop.post("/api/password", json={"current": "wrong-wrong-1", "new": "another good password"}).status_code == 401
    assert laptop.post("/api/password", json={"current": PW, "new": "short"}).status_code == 422
    assert laptop.post("/api/password", json={"current": PW, "new": "another good password"}).status_code == 200
    assert laptop.get("/api/jobs").status_code == 200 and phone.get("/api/jobs").status_code == 401
    assert TestClient(app.app).post("/api/login", json={"username": "alice", "password": PW}).status_code == 401
    assert TestClient(app.app).post("/api/login", json={"username": "alice", "password": "another good password"}).status_code == 200


def test_people_stay_signed_in_across_a_server_restart(make_app, tmp_path):
    first = sign_up(make_app(), "alice")
    cookie = first.cookies.get("wtp_session")
    first.__exit__(None, None, None)
    second = make_app()                                                   # same data folder: the signing key was kept beside the database
    second.cookies.set("wtp_session", cookie)
    assert second.get("/api/session").json()["user"]["username"] == "alice"
    assert (tmp_path / "session.secret").is_file()


def test_a_session_secret_from_the_environment_is_used_instead(make_app, tmp_path):
    client = sign_up(make_app(session_secret="from-the-environment"), "alice")
    assert not (tmp_path / "session.secret").exists()
    other = make_app(session_secret="a-different-secret")
    other.cookies.set("wtp_session", client.cookies.get("wtp_session"))
    assert other.get("/api/jobs").status_code == 401                      # signed with another secret: not valid here


# ------------------------------------------------------------------------------------------------ everyone's runs are their own

def test_each_person_sees_only_their_own_runs_and_nobody_sees_everything(make_app):
    first = sign_up(make_app(), "alice")
    bob = another_person(first, "bob")
    a = first.post("/api/jobs", json={"url": "alice-site.example.com"}).json()
    b = bob.post("/api/jobs", json={"url": "bob-site.example.com"}).json()
    assert [j["id"] for j in first.get("/api/jobs").json()] == [a["id"]]
    assert [j["id"] for j in bob.get("/api/jobs").json()] == [b["id"]]


def test_someone_elses_run_is_a_404_for_every_way_of_reaching_it(make_app):
    alice = sign_up(make_app(), "alice")
    bob = another_person(alice, "bob")
    mine = alice.post("/api/jobs", json={"url": "alice-site.example.com"}).json()
    assert wait_for(alice, mine["id"])["status"] == "done"
    base = f"/api/jobs/{mine['id']}"
    for method, path in (("get", base), ("post", base + "/cancel"), ("get", base + "/events"), ("get", base + "/files"),
                         ("get", base + "/files/artifacts/report/r.docx"), ("get", base + "/human"), ("get", base + "/info"),
                         ("get", base + "/human/history"), ("delete", base + "/session")):
        assert getattr(bob, method)(path).status_code == 404, (method, path)
    assert alice.get(base + "/files/artifacts/report/r.docx").content == b"private-report"
    assert alice.get(base).json()["status"] == "done"                                             # nothing of theirs was touched


def test_one_persons_running_job_does_not_block_another_from_starting_theirs(make_app):
    alice = sign_up(make_app(), "alice")
    bob = another_person(alice, "bob")
    assert alice.post("/api/jobs", json={"url": "slow.example.com"}).status_code == 201
    assert alice.post("/api/jobs", json={"url": "again.example.com"}).status_code == 409          # their own limit still applies
    assert bob.post("/api/jobs", json={"url": "bob-site.example.com"}).status_code == 201         # but it is theirs alone


def test_a_reused_site_folder_is_per_person_so_nobody_works_in_anothers(make_app):
    alice = sign_up(make_app(), "alice")
    bob = another_person(alice, "bob")
    a = alice.post("/api/jobs", json={"url": "shared.example.com", "options": {"reuse_workspace": True}}).json()
    wait_for(alice, a["id"])
    b = bob.post("/api/jobs", json={"url": "shared.example.com", "options": {"reuse_workspace": True}}).json()
    assert a["site"] != b["site"] and a["site"].endswith("-shared.example.com") and b["site"].endswith("-shared.example.com")


def test_the_pipeline_never_receives_the_session_secret_or_any_password_material(make_app, tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("SESSION_SECRET", "the-servers-own-signing-secret")
    alice = sign_up(make_app(), "alice")
    job = alice.post("/api/jobs", json={"url": "alice-site.example.com"}).json()
    wait_for(alice, job["id"])
    env = json.loads((tmp_path / "runs" / job["site"] / "artifacts" / "env.json").read_text())
    blob = json.dumps(env)
    assert "SESSION_SECRET" not in env and "the-servers-own-signing-secret" not in blob
    assert PW not in blob and "scrypt$" not in blob
    assert env["REPORT_OWNER"] == "alice"                                                         # the report names whoever ran it
