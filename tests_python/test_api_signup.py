"""Anyone may create their own account: no code, no invitation. Open registration is protected by limits, so it is not a way
around the per-person limits on runs."""
import sys

import pytest
from fastapi.testclient import TestClient

from authhelp import PW
from api.main import create_app
from api.settings import AppSettings


@pytest.fixture
def make_app(tmp_path):
    opened = []

    def make(**overrides):
        settings = AppSettings(**{"data_dir": tmp_path, "command": [sys.executable, "-c", "pass"], "poll_interval_s": 0.05,
                                  "idle_interval_s": 0.05, "require_model": False, **overrides})
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def stranger(client):
    return TestClient(client.app)


def test_a_stranger_can_create_an_account_with_nothing_but_a_username_and_password(make_app):
    client = make_app()
    done = stranger(client).post("/api/signup", json={"username": "dana", "password": PW})
    assert done.status_code == 200 and done.json()["authenticated"] is True


def test_an_extra_role_field_is_ignored_because_there_are_no_roles(make_app):
    client = make_app()
    sneaky = stranger(client).post("/api/signup", json={"username": "mallory", "password": PW, "role": "admin"})
    assert sneaky.status_code == 200 and "role" not in sneaky.text
    # and there is nothing for such a field to have unlocked
    assert stranger(client).get("/api/users").status_code in (404, 405)


def test_one_address_can_only_create_a_few_accounts_an_hour(make_app):
    client = make_app(signups_per_address_per_hour=3)
    codes = [stranger(client).post("/api/signup", json={"username": f"person{i}", "password": PW}).status_code for i in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_a_failed_attempt_does_not_use_up_the_hourly_allowance(make_app):
    client = make_app(signups_per_address_per_hour=3)
    for _ in range(6):
        assert stranger(client).post("/api/signup", json={"username": "x", "password": PW}).status_code == 422
    assert stranger(client).post("/api/signup", json={"username": "fine1", "password": PW}).status_code == 200


def test_a_daily_cap_stops_registration_for_everyone_and_zero_closes_it(make_app):
    client = make_app(max_signups_per_day=2, signups_per_address_per_hour=100)
    assert stranger(client).post("/api/signup", json={"username": "first1", "password": PW}).status_code == 200
    assert stranger(client).post("/api/signup", json={"username": "second", "password": PW}).status_code == 200
    capped = stranger(client).post("/api/signup", json={"username": "third1", "password": PW})
    assert capped.status_code == 429 and "not taking new accounts" in capped.text
    closed = make_app(max_signups_per_day=0)
    assert stranger(closed).post("/api/signup", json={"username": "anyone", "password": PW}).status_code == 429


def test_signed_up_people_are_as_isolated_as_any_others(make_app):
    client = make_app(signups_per_address_per_hour=100)
    a, b = stranger(client), stranger(client)
    a.post("/api/signup", json={"username": "ann", "password": PW})
    b.post("/api/signup", json={"username": "ben", "password": PW})
    job = a.post("/api/jobs", json={"url": "ann-site.example.com"}).json()
    assert [j["id"] for j in a.get("/api/jobs").json()] == [job["id"]] and b.get("/api/jobs").json() == []
    assert b.get(f"/api/jobs/{job['id']}").status_code == 404
