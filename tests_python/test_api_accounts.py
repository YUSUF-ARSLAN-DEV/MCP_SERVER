"""Accounts: the first administrator, sign-in, roles, and - above all - that one person can never see or touch another
person's runs."""
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.settings import AppSettings

CODE = "bootstrap-code"
PW = "correct horse battery"

FAKE_PIPELINE = r"""
import os, time
from pathlib import Path
run = Path(os.environ["DATA_DIR"]) / "runs" / os.environ["SITE"]
(run / "artifacts" / "report").mkdir(parents=True, exist_ok=True)
(run / "artifacts" / "report" / "r.docx").write_bytes(b"private-report")
import json
(run / "artifacts" / "env.json").write_text(json.dumps(dict(os.environ)))
if "slow" in os.environ["SEED_URL"]:
    time.sleep(60)
"""


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    opened = []

    def make(**overrides):
        settings = AppSettings(data_dir=tmp_path, access_code=overrides.pop("access_code", CODE), command=[sys.executable, "-c", FAKE_PIPELINE],
                               poll_interval_s=0.05, idle_interval_s=0.05, max_jobs_per_day=50, **overrides)
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def browser(client):
    """Another person's browser: its own cookies, the same running server."""
    return TestClient(client.app)


def setup_admin(client, name="root", pw=PW):
    r = client.post("/api/setup", json={"username": name, "password": pw, "code": CODE})
    assert r.status_code == 200, r.text
    return client


def add_user(admin, name, pw=PW, role="member"):
    r = admin.post("/api/users", json={"username": name, "password": pw, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def sign_in(client, name, pw=PW):
    r = client.post("/api/login", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return client


def wait_for(client, job_id, statuses=("done", "failed", "cancelled"), timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(job)


# ------------------------------------------------------------------------------------------------ first account

def test_a_fresh_server_asks_for_setup_and_only_the_access_code_holder_can_do_it(make_app):
    client = make_app()
    state = client.get("/api/session").json()
    assert state["setup_needed"] is True and state["accounts"] is False and state["needs_code"] is True and state["authenticated"] is False
    wrong = client.post("/api/setup", json={"username": "root", "password": PW, "code": "nope"})
    assert wrong.status_code == 401
    assert client.get("/api/users").status_code == 401                      # nothing was created


def test_setup_creates_the_administrator_signs_them_in_and_can_only_happen_once(make_app):
    client = make_app()
    done = client.post("/api/setup", json={"username": "Root", "password": PW, "code": CODE})
    assert done.status_code == 200 and done.json()["user"]["username"] == "root" and done.json()["user"]["role"] == "admin"
    assert "password" not in done.text and PW not in done.text
    state = client.get("/api/session").json()
    assert state["authenticated"] and state["user"]["username"] == "root" and state["accounts"] and not state["setup_needed"]
    again = browser(client).post("/api/setup", json={"username": "evil", "password": PW, "code": CODE})
    assert again.status_code == 409


@pytest.mark.parametrize("name,pw,reason", [
    ("ab", PW, "username"), ("has space", PW, "username"), ("../etc", PW, "username"),
    ("root", "short", "at least"), ("root", "root", "at least"), ("rootroot12", "rootroot12", "same as the username"),
    ("root", "aaaaaaaaaaaa", "repetitive"), ("root", "x" * 300, "at most"),
])
def test_a_bad_username_or_password_is_refused_with_a_reason(make_app, name, pw, reason):
    r = make_app().post("/api/setup", json={"username": name, "password": pw, "code": CODE})
    assert r.status_code in (422,) and reason in r.text


def test_without_an_access_code_setup_is_only_allowed_from_the_servers_own_machine(make_app):
    client = make_app(access_code="")
    refused = client.post("/api/setup", json={"username": "root", "password": PW})
    assert refused.status_code == 403 and "machine the server runs on" in refused.text      # the test client is not loopback
    assert make_app(access_code="", trust_proxy=True).post("/api/setup", json={"username": "root", "password": PW}).status_code == 403


def test_once_accounts_exist_the_shared_access_code_no_longer_opens_anything(make_app):
    client = setup_admin(make_app())
    stranger = browser(client)
    assert stranger.post("/api/login", json={"code": CODE}).status_code == 401
    assert stranger.get("/api/jobs", headers={"x-access-code": CODE}).status_code == 401
    assert stranger.get("/api/jobs").status_code == 401


# ------------------------------------------------------------------------------------------------ signing in

def test_sign_in_works_and_a_wrong_password_looks_the_same_as_an_unknown_user(make_app):
    admin = setup_admin(make_app())
    add_user(admin, "alice")
    ok = browser(admin).post("/api/login", json={"username": "ALICE", "password": PW})
    assert ok.status_code == 200 and ok.json()["user"]["role"] == "member"
    wrong = browser(admin).post("/api/login", json={"username": "alice", "password": "wrong-password-1"})
    unknown = browser(admin).post("/api/login", json={"username": "nobody", "password": PW})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()


def test_guessing_one_account_is_throttled(make_app):
    admin = setup_admin(make_app())
    add_user(admin, "alice")
    codes = [browser(admin).post("/api/login", json={"username": "alice", "password": f"guess-number-{i}"}).status_code for i in range(12)]
    assert 429 in codes and codes[0] == 401


def test_a_tampered_or_foreign_session_cookie_is_refused(make_app):
    admin = setup_admin(make_app())
    forged = browser(admin)
    forged.cookies.set("wtp_session", "u.someone.0.9999999999.deadbeef")
    assert forged.get("/api/jobs").status_code == 401
    assert browser(admin).get("/api/jobs").status_code == 401


def test_logout_clears_the_cookie(make_app):
    admin = setup_admin(make_app())
    assert admin.get("/api/jobs").status_code == 200
    admin.post("/api/logout")
    assert admin.get("/api/jobs").status_code == 401


def test_changing_your_password_ends_your_other_sessions_but_not_this_one(make_app):
    admin = setup_admin(make_app())
    add_user(admin, "alice")
    laptop, phone = sign_in(browser(admin), "alice"), sign_in(browser(admin), "alice")
    assert laptop.post("/api/password", json={"current": "wrong-wrong-1", "new": "another good password"}).status_code == 401
    assert laptop.post("/api/password", json={"current": PW, "new": "short"}).status_code == 422
    assert laptop.post("/api/password", json={"current": PW, "new": "another good password"}).status_code == 200
    assert laptop.get("/api/jobs").status_code == 200 and phone.get("/api/jobs").status_code == 401
    assert browser(admin).post("/api/login", json={"username": "alice", "password": PW}).status_code == 401
    assert browser(admin).post("/api/login", json={"username": "alice", "password": "another good password"}).status_code == 200


def test_disabling_an_account_ends_its_sessions_and_blocks_sign_in(make_app):
    admin = setup_admin(make_app())
    alice = add_user(admin, "alice")
    session = sign_in(browser(admin), "alice")
    assert admin.patch(f"/api/users/{alice['id']}", json={"disabled": True}).json()["disabled"] is True
    assert session.get("/api/jobs").status_code == 401
    assert browser(admin).post("/api/login", json={"username": "alice", "password": PW}).status_code == 401
    admin.patch(f"/api/users/{alice['id']}", json={"disabled": False})
    assert browser(admin).post("/api/login", json={"username": "alice", "password": PW}).status_code == 200


# ------------------------------------------------------------------------------------------------ roles

def test_only_an_administrator_manages_users_and_hashes_are_never_returned(make_app):
    admin = setup_admin(make_app())
    add_user(admin, "alice")
    alice = sign_in(browser(admin), "alice")
    for call in (lambda: alice.get("/api/users"), lambda: alice.post("/api/users", json={"username": "bob12", "password": PW}),
                 lambda: alice.patch("/api/users/x", json={"disabled": True})):
        assert call().status_code == 403
    listing = admin.get("/api/users")
    assert [u["username"] for u in listing.json()] == ["root", "alice"]
    assert "hash" not in listing.text and "scrypt" not in listing.text and PW not in listing.text
    assert admin.post("/api/users", json={"username": "alice", "password": PW}).status_code == 409


def test_the_last_active_administrator_cannot_be_removed(make_app):
    admin = setup_admin(make_app())
    me = admin.get("/api/session").json()["user"]["id"]
    assert admin.patch(f"/api/users/{me}", json={"role": "member"}).status_code == 409
    assert admin.patch(f"/api/users/{me}", json={"disabled": True}).status_code == 409
    second = add_user(admin, "second", role="admin")
    assert admin.patch(f"/api/users/{second['id']}", json={"role": "member"}).status_code == 200
    assert admin.patch(f"/api/users/{me}", json={"password": "a brand new password"}).status_code == 200


# ------------------------------------------------------------------------------------------------ everyone's runs are their own

def two_people(make_app):
    admin = setup_admin(make_app())
    add_user(admin, "alice"), add_user(admin, "bob")
    return admin, sign_in(browser(admin), "alice"), sign_in(browser(admin), "bob")


def test_each_person_sees_only_their_own_runs_and_an_administrator_sees_all(make_app):
    admin, alice, bob = two_people(make_app)
    a = alice.post("/api/jobs", json={"url": "alice-site.example.com"}).json()
    b = bob.post("/api/jobs", json={"url": "bob-site.example.com"}).json()
    assert [j["id"] for j in alice.get("/api/jobs").json()] == [a["id"]]
    assert [j["id"] for j in bob.get("/api/jobs").json()] == [b["id"]]
    assert {j["id"] for j in admin.get("/api/jobs").json()} == {a["id"], b["id"]}


def test_someone_elses_run_is_a_404_for_every_way_of_reaching_it(make_app, tmp_path):
    admin, alice, bob = two_people(make_app)
    mine = alice.post("/api/jobs", json={"url": "alice-site.example.com"}).json()
    assert wait_for(alice, mine["id"])["status"] == "done"
    base = f"/api/jobs/{mine['id']}"
    for method, path in (("get", base), ("post", base + "/cancel"), ("get", base + "/events"), ("get", base + "/files"),
                         ("get", base + "/files/artifacts/report/r.docx"), ("get", base + "/human"), ("delete", base + "/session")):
        assert getattr(bob, method)(path).status_code == 404, (method, path)
    assert alice.get(base + "/files/artifacts/report/r.docx").content == b"private-report"
    assert admin.get(base + "/files/artifacts/report/r.docx").content == b"private-report"        # an administrator may
    assert alice.get(base).json()["status"] == "done"                                              # nothing of theirs was touched


def test_one_persons_running_job_does_not_block_another_from_starting_theirs(make_app):
    admin, alice, bob = two_people(make_app)
    first = alice.post("/api/jobs", json={"url": "slow.example.com"})
    assert first.status_code == 201
    assert alice.post("/api/jobs", json={"url": "again.example.com"}).status_code == 409        # their own limit still applies
    assert bob.post("/api/jobs", json={"url": "bob-site.example.com"}).status_code == 201       # but it is theirs alone


def test_a_reused_site_folder_is_per_person_so_nobody_works_in_anothers(make_app, tmp_path):
    admin, alice, bob = two_people(make_app)
    a = alice.post("/api/jobs", json={"url": "shared.example.com", "options": {"reuse_workspace": True}}).json()
    wait_for(alice, a["id"])
    b = bob.post("/api/jobs", json={"url": "shared.example.com", "options": {"reuse_workspace": True}}).json()
    assert a["site"] != b["site"] and a["site"].endswith("-shared.example.com") and b["site"].endswith("-shared.example.com")


def test_the_pipeline_never_receives_the_access_code_session_secret_or_any_password_material(make_app, tmp_path):
    import json
    admin, alice, bob = two_people(make_app)
    job = alice.post("/api/jobs", json={"url": "alice-site.example.com"}).json()
    wait_for(alice, job["id"])
    env = json.loads((tmp_path / "runs" / job["site"] / "artifacts" / "env.json").read_text())
    assert "ACCESS_CODE" not in env and "SESSION_SECRET" not in env
    blob = json.dumps(env)
    assert PW not in blob and "scrypt$" not in blob and CODE not in blob
