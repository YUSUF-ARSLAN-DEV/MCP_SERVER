"""Anyone may create their own account once the server has an administrator - no shared password - but only a member's,
and with limits so open registration is not a way around the per-person limits."""
import sys

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.settings import AppSettings

CODE = "bootstrap-code"
PW = "correct horse battery"


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    opened = []

    def make(setup=True, **overrides):
        settings = AppSettings(require_model=False, data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", "pass"],
                               poll_interval_s=0.05, idle_interval_s=0.05, **overrides)
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        if setup:
            assert client.post("/api/setup", json={"username": "root", "password": PW, "code": CODE}).status_code == 200
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def stranger(client):
    return TestClient(client.app)


def test_anyone_can_create_a_member_account_without_any_code_and_is_signed_in(make_app):
    admin = make_app()
    new = stranger(admin)
    state = new.get("/api/session").json()
    assert state["signup"] == "open" and state["accounts"] and not state["authenticated"] and not state["setup_needed"]
    done = new.post("/api/signup", json={"username": "Dana", "password": PW})
    assert done.status_code == 200 and done.json()["user"] == {"id": done.json()["user"]["id"], "username": "dana", "role": "member"}
    assert PW not in done.text
    assert new.get("/api/session").json()["user"]["username"] == "dana"           # signed in straight away
    assert new.get("/api/users").status_code == 403                                 # a member, never an administrator
    assert [u["username"] for u in admin.get("/api/users").json()] == ["root", "dana"]


def test_the_role_cannot_be_chosen_at_signup(make_app):
    admin = make_app()
    sneaky = stranger(admin).post("/api/signup", json={"username": "mallory", "password": PW, "role": "admin"})
    assert sneaky.status_code == 200 and sneaky.json()["user"]["role"] == "member"


@pytest.mark.parametrize("name,pw,reason", [("ab", PW, "username"), ("has space", PW, "username"), ("dana12", "short", "at least"),
                                            ("dana12dana", "dana12dana", "same as the username"), ("dana12", "aaaaaaaaaaaa", "repetitive")])
def test_a_bad_username_or_password_is_refused_with_a_reason(make_app, name, pw, reason):
    r = stranger(make_app()).post("/api/signup", json={"username": name, "password": pw})
    assert r.status_code == 422 and reason in r.text


def test_a_taken_name_is_refused_whatever_its_capitals(make_app):
    admin = make_app()
    assert stranger(admin).post("/api/signup", json={"username": "ROOT", "password": PW}).status_code == 409


def test_before_the_first_account_signup_points_to_setup_instead(make_app):
    fresh = make_app(setup=False)
    r = fresh.post("/api/signup", json={"username": "dana", "password": PW})
    assert r.status_code == 409 and "first account" in r.text
    assert fresh.get("/api/users").status_code in (401, 409)


def test_a_server_can_close_registration(make_app):
    admin = make_app(signup="closed")
    state = stranger(admin).get("/api/session").json()
    assert state["signup"] == "closed"
    refused = stranger(admin).post("/api/signup", json={"username": "dana", "password": PW})
    assert refused.status_code == 403 and "closed" in refused.text
    assert admin.post("/api/users", json={"username": "dana", "password": PW}).status_code == 201     # an administrator still can


def test_one_address_can_only_create_a_few_accounts_an_hour(make_app):
    admin = make_app()
    codes = [stranger(admin).post("/api/signup", json={"username": f"person{i}", "password": PW}).status_code for i in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_a_daily_cap_stops_registration_for_everyone(make_app):
    admin = make_app(max_signups_per_day=3)             # root counts as one
    assert stranger(admin).post("/api/signup", json={"username": "first1", "password": PW}).status_code == 200
    assert stranger(admin).post("/api/signup", json={"username": "second", "password": PW}).status_code == 200
    capped = stranger(admin).post("/api/signup", json={"username": "third1", "password": PW})
    assert capped.status_code == 429 and "limit for today" in capped.text


def test_a_failed_attempt_does_not_use_up_the_hourly_allowance(make_app):
    admin = make_app()
    for _ in range(5):
        assert stranger(admin).post("/api/signup", json={"username": "x", "password": PW}).status_code == 422
    assert stranger(admin).post("/api/signup", json={"username": "fine1", "password": PW}).status_code == 200


def test_signed_up_people_are_as_isolated_as_any_others(make_app):
    admin = make_app()
    a, b = stranger(admin), stranger(admin)
    a.post("/api/signup", json={"username": "ann", "password": PW})
    b.post("/api/signup", json={"username": "ben", "password": PW})
    job = a.post("/api/jobs", json={"url": "ann-site.example.com"}).json()
    assert [j["id"] for j in a.get("/api/jobs").json()] == [job["id"]] and b.get("/api/jobs").json() == []
    assert b.get(f"/api/jobs/{job['id']}").status_code == 404
