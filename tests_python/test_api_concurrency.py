"""Different people's runs execute side by side, up to a limit, and one person's cancel or failure never touches another's."""
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.settings import AppSettings


PW = "correct horse battery"

FAKE_PIPELINE = r"""
import os, sys, time
from pathlib import Path
run = Path(os.environ["DATA_DIR"]) / "runs" / os.environ["SITE"]
(run / "artifacts").mkdir(parents=True, exist_ok=True)
url = os.environ["SEED_URL"]
(run / "artifacts" / "started.txt").write_text(str(time.time()))
if "slow" in url:
    time.sleep(60)
if "fail" in url:
    print("the model is unreachable", flush=True)
    sys.exit(2)
"""


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    opened = []

    def make(**overrides):
        settings = AppSettings(require_model=False, data_dir=tmp_path, command=[sys.executable, "-c", FAKE_PIPELINE],
                               poll_interval_s=0.05, idle_interval_s=0.05, max_jobs_per_day=50, **overrides)
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        assert client.post("/api/signup", json={"username": "root", "password": PW}).status_code == 200
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def person(admin, name):
    browser = TestClient(admin.app)
    assert browser.post("/api/signup", json={"username": name, "password": PW}).status_code == 200
    return browser


def status(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()["status"]


def wait_status(client, job_id, wanted, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status(client, job_id) in wanted:
            return status(client, job_id)
        time.sleep(0.05)
    raise AssertionError(f"{job_id} is {status(client, job_id)}, wanted {wanted}")


def start(client, url):
    r = client.post("/api/jobs", json={"url": url})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_two_peoples_runs_execute_at_the_same_time(make_app):
    admin = make_app()
    alice, bob = person(admin, "alice"), person(admin, "bob")
    a, b = start(alice, "slow-a.example.com"), start(bob, "slow-b.example.com")
    assert wait_status(alice, a, ("running",)) == "running" and wait_status(bob, b, ("running",)) == "running"
    assert status(alice, a) == "running"                          # still both at once, not one after the other


def test_past_the_limit_a_run_waits_its_turn_and_starts_when_there_is_room(make_app):
    admin = make_app(max_concurrent_jobs=1)
    alice, bob = person(admin, "alice"), person(admin, "bob")
    a, b = start(alice, "slow-a.example.com"), start(bob, "slow-b.example.com")
    wait_status(alice, a, ("running",))
    time.sleep(0.5)
    assert status(bob, b) == "queued"                             # one at a time, as the limit says
    assert alice.post(f"/api/jobs/{a}/cancel").json()["status"] == "cancelled"
    assert wait_status(bob, b, ("running",)) == "running"         # room appeared, the waiting run started


def test_the_limit_is_configurable_and_a_third_run_queues_behind_two(make_app):
    admin = make_app(max_concurrent_jobs=2)
    alice, bob = person(admin, "alice"), person(admin, "bob")
    a, b = start(alice, "slow-a.example.com"), start(bob, "slow-b.example.com")
    wait_status(alice, a, ("running",)), wait_status(bob, b, ("running",))
    c = start(admin, "slow-c.example.com")
    time.sleep(0.5)
    assert status(admin, c) == "queued"
    bob.post(f"/api/jobs/{b}/cancel")
    assert wait_status(admin, c, ("running",)) == "running"


def test_cancelling_one_persons_run_leaves_the_others_running(make_app):
    admin = make_app()
    alice, bob = person(admin, "alice"), person(admin, "bob")
    a, b = start(alice, "slow-a.example.com"), start(bob, "slow-b.example.com")
    wait_status(alice, a, ("running",)), wait_status(bob, b, ("running",))
    alice.post(f"/api/jobs/{a}/cancel")
    time.sleep(1.0)
    assert status(alice, a) == "cancelled" and status(bob, b) == "running"


def test_one_persons_failing_run_does_not_disturb_anothers(make_app):
    admin = make_app()
    alice, bob = person(admin, "alice"), person(admin, "bob")
    b = start(bob, "slow-b.example.com")
    a = start(alice, "fail-a.example.com")
    assert wait_status(alice, a, ("failed",)) == "failed"
    assert "unreachable" in alice.get(f"/api/jobs/{a}").json()["error"]
    assert status(bob, b) == "running"


def test_a_finished_run_frees_its_slot_for_the_next_in_line(make_app):
    admin = make_app(max_concurrent_jobs=1)
    alice, bob = person(admin, "alice"), person(admin, "bob")
    a, b = start(alice, "quick-a.example.com"), start(bob, "quick-b.example.com")
    assert wait_status(alice, a, ("done",)) == "done" and wait_status(bob, b, ("done",)) == "done"
