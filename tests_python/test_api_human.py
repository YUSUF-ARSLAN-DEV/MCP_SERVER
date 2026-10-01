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
        settings = AppSettings(data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", FAKE_PIPELINE],
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
