"""Saved model configurations: private to one person, the key encrypted at rest and never returned, usable to start a run."""
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.secretbox import KEY_FILE, SecretBox, SecretBoxError, load_secretbox
from api.settings import AppSettings

CODE = "bootstrap-code"
PW = "correct horse battery"
KEY = "sk-live-VERY-SECRET-key-123456"

FAKE = r"""
import json, os
from pathlib import Path
run = Path(os.environ["DATA_DIR"]) / "runs" / os.environ["SITE"]
(run / "artifacts").mkdir(parents=True, exist_ok=True)
(run / "artifacts" / "model_env.json").write_text(json.dumps({k: os.environ.get(k) for k in ("MODEL_PROVIDER", "MODEL_NAME", "API_URL", "API_KEY")}))
"""


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    opened = []

    def make(**overrides):
        settings = AppSettings(data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", FAKE], poll_interval_s=0.05,
                               idle_interval_s=0.05, max_jobs_per_day=50, **overrides)
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]))
        client.__enter__()
        opened.append(client)
        assert client.post("/api/setup", json={"username": "root", "password": PW, "code": CODE}).status_code == 200
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def person(admin, name):
    assert admin.post("/api/users", json={"username": name, "password": PW}).status_code == 201
    browser = TestClient(admin.app)
    assert browser.post("/api/login", json={"username": name, "password": PW}).status_code == 200
    return browser


def finish(client, job_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(job)


GEMINI = {"name": "Gemini flash", "provider": "google", "model": "gemini-3.1-flash-lite", "api_key": KEY}


# ------------------------------------------------------------------------------------------------ the key is secret

def test_a_saved_key_is_never_returned_and_never_stored_in_plain(make_app, tmp_path):
    client = make_app()
    created = client.post("/api/models", json=GEMINI)
    assert created.status_code == 201 and created.json()["key_set"] is True and KEY not in created.text
    assert KEY not in client.get("/api/models").text
    raw = open(tmp_path / "app.db", "rb").read() + b"".join(open(p, "rb").read() for p in tmp_path.glob("app.db-*"))
    assert KEY.encode() not in raw                                 # not in the database file, nor its write-ahead log
    with sqlite3.connect(tmp_path / "app.db") as conn:
        stored = conn.execute("SELECT key_enc FROM model_profiles").fetchone()[0]
    assert stored and stored != KEY and stored.startswith("gAAAA")  # a Fernet token
    assert KEY not in json.dumps(client.get("/api/jobs").json())


def test_the_stored_key_decrypts_to_what_was_saved_with_the_servers_key(make_app, tmp_path):
    client = make_app()
    client.post("/api/models", json=GEMINI)
    with sqlite3.connect(tmp_path / "app.db") as conn:
        stored = conn.execute("SELECT key_enc FROM model_profiles").fetchone()[0]
    assert load_secretbox(tmp_path).decrypt(stored) == KEY


def test_a_server_secrets_key_from_the_environment_is_used_instead_of_a_key_file(make_app, tmp_path):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    client = make_app(secrets_key=key)
    client.post("/api/models", json=GEMINI)
    assert not (tmp_path / KEY_FILE).exists()
    with sqlite3.connect(tmp_path / "app.db") as conn:
        stored = conn.execute("SELECT key_enc FROM model_profiles").fetchone()[0]
    assert SecretBox(key.encode()).decrypt(stored) == KEY


# ------------------------------------------------------------------------------------------------ yours alone

def test_nobody_else_can_see_change_test_delete_or_run_with_your_saved_model(make_app):
    admin = make_app()
    alice, bob = person(admin, "alice"), person(admin, "bob")
    mine = alice.post("/api/models", json=GEMINI).json()
    assert bob.get("/api/models").json() == [] and admin.get("/api/models").json() == []       # not even an administrator
    for method, path, body in (("patch", f"/api/models/{mine['id']}", {"name": "stolen"}), ("delete", f"/api/models/{mine['id']}", None),
                               ("post", f"/api/models/{mine['id']}/test", None)):
        assert getattr(bob, method)(path, **({"json": body} if body else {})).status_code == 404, path
    assert bob.post("/api/jobs", json={"url": "example.com", "model_id": mine["id"]}).status_code == 404
    assert [m["id"] for m in alice.get("/api/models").json()] == [mine["id"]]


def test_two_people_may_use_the_same_name_but_you_may_not_repeat_your_own(make_app):
    admin = make_app()
    alice, bob = person(admin, "alice"), person(admin, "bob")
    assert alice.post("/api/models", json=GEMINI).status_code == 201
    assert bob.post("/api/models", json=GEMINI).status_code == 201
    assert alice.post("/api/models", json={**GEMINI, "name": "gemini FLASH"}).status_code == 409


# ------------------------------------------------------------------------------------------------ managing them

def test_update_keeps_the_saved_key_unless_a_new_one_is_sent(make_app, tmp_path):
    client = make_app()
    mid = client.post("/api/models", json=GEMINI).json()["id"]
    renamed = client.patch(f"/api/models/{mid}", json={"name": "Renamed", "model": "gemini-x"})
    assert renamed.status_code == 200 and renamed.json()["key_set"] is True and renamed.json()["model"] == "gemini-x"
    with sqlite3.connect(tmp_path / "app.db") as conn:
        stored = conn.execute("SELECT key_enc FROM model_profiles").fetchone()[0]
    assert load_secretbox(tmp_path).decrypt(stored) == KEY                      # untouched
    client.patch(f"/api/models/{mid}", json={"api_key": "a-new-key-9999"})
    with sqlite3.connect(tmp_path / "app.db") as conn:
        stored = conn.execute("SELECT key_enc FROM model_profiles").fetchone()[0]
    assert load_secretbox(tmp_path).decrypt(stored) == "a-new-key-9999"


def test_delete_removes_it_and_a_second_delete_is_a_404(make_app):
    client = make_app()
    mid = client.post("/api/models", json=GEMINI).json()["id"]
    assert client.delete(f"/api/models/{mid}").status_code == 200
    assert client.get("/api/models").json() == [] and client.delete(f"/api/models/{mid}").status_code == 404


@pytest.mark.parametrize("body,reason", [
    ({"name": "x", "provider": "anthropic", "model": "claude-x"}, "API_KEY"),
    ({"name": "x", "provider": "openai", "model": "m"}, "chat-completions address"),
    ({"name": "x", "provider": "openai", "model": "m", "api_url": "http://localhost:11434/v1/chat/completions"}, "Model address"),
    ({"name": "x", "provider": "openai", "model": "m", "api_url": "http://10.0.0.5/v1"}, "Model address"),
])
def test_an_incomplete_or_unsafe_configuration_is_refused_with_a_reason_and_nothing_is_saved(make_app, body, reason):
    client = make_app()
    refused = client.post("/api/models", json=body)
    assert refused.status_code == 422 and reason in refused.text
    assert client.get("/api/models").json() == []


@pytest.mark.parametrize("bad", [{"name": "", "provider": "google", "model": "m"}, {"name": "x", "provider": "skynet", "model": "m"},
                                 {"name": "x", "provider": "google", "model": "bad name; drop"}])
def test_a_malformed_configuration_is_refused(make_app, bad):
    assert make_app().post("/api/models", json={**bad, "api_key": KEY}).status_code == 422


def test_you_can_save_only_so_many(make_app):
    client = make_app()
    for i in range(20):
        assert client.post("/api/models", json={**GEMINI, "name": f"m{i}"}).status_code == 201
    assert client.post("/api/models", json={**GEMINI, "name": "one too many"}).status_code == 422


def test_without_an_account_there_is_nowhere_to_save_them(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    settings = AppSettings(data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", FAKE])
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/login", json={"code": CODE}).status_code == 200      # the old shared-code mode
        assert client.get("/api/models").status_code == 409
        assert client.post("/api/jobs", json={"url": "example.com", "model_id": "x"}).status_code in (409, 422)


# ------------------------------------------------------------------------------------------------ running with one

def test_a_run_started_from_a_saved_model_gets_its_provider_model_address_and_decrypted_key(make_app, tmp_path):
    client = make_app()
    mid = client.post("/api/models", json=GEMINI).json()["id"]
    job = client.post("/api/jobs", json={"url": "example.com", "model_id": mid})
    assert job.status_code == 201 and KEY not in job.text
    done = finish(client, job.json()["id"])
    assert done["status"] == "done" and KEY not in json.dumps(done)
    env = json.loads((tmp_path / "runs" / job.json()["site"] / "artifacts" / "model_env.json").read_text())
    assert env == {"MODEL_PROVIDER": "google", "MODEL_NAME": "gemini-3.1-flash-lite", "API_URL": "https://generativelanguage.googleapis.com", "API_KEY": KEY}
    with sqlite3.connect(tmp_path / "app.db") as conn:
        options = json.loads(conn.execute("SELECT options FROM jobs").fetchone()[0])
    assert options["model"]["profile"] == {"id": mid, "name": "Gemini flash"} and KEY not in json.dumps(options)
    assert not (tmp_path / "runs" / job.json()["site"] / "secrets").exists()


def test_a_saved_model_and_a_one_off_one_together_is_refused(make_app):
    client = make_app()
    mid = client.post("/api/models", json=GEMINI).json()["id"]
    both = client.post("/api/jobs", json={"url": "example.com", "model_id": mid, "model": {"provider": "google", "model": "m", "api_key": KEY}})
    assert both.status_code == 422 and "not both" in both.text


def test_a_lost_master_key_is_reported_clearly_not_as_a_crash(make_app, tmp_path):
    client = make_app()
    mid = client.post("/api/models", json=GEMINI).json()["id"]
    (tmp_path / KEY_FILE).write_bytes(__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key())
    # a server restarted with a different key file cannot read old keys: simulate by opening a fresh app over the same data
    settings = AppSettings(data_dir=tmp_path, access_code=CODE, command=[sys.executable, "-c", FAKE])
    with TestClient(create_app(settings)) as fresh:
        assert fresh.post("/api/login", json={"username": "root", "password": PW}).status_code == 200
        refused = fresh.post("/api/jobs", json={"url": "example.com", "model_id": mid})
        assert refused.status_code == 409 and "Enter the key again" in refused.text and KEY not in refused.text


# ------------------------------------------------------------------------------------------------ "test connection"

class _Models(BaseHTTPRequestHandler):
    seen = []
    status = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        body = json.dumps({"data": [{"id": "served-1"}, {"id": "served-2"}]} if self.status == 200 else {"error": "no"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def models_server():
    _Models.seen, _Models.status = [], 200
    httpd = HTTPServer(("127.0.0.1", 0), _Models)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1/chat/completions"
    httpd.shutdown()


def test_testing_a_connection_says_whether_the_address_key_and_model_work(make_app, models_server):
    client = make_app(allow_private_model_urls=True)
    good = client.post("/api/models", json={"name": "local", "provider": "openai", "model": "served-2", "api_url": models_server, "api_key": KEY}).json()["id"]
    result = client.post(f"/api/models/{good}/test").json()
    assert result["ok"] is True and "'served-2' is available" in result["message"] and KEY not in json.dumps(result)
    assert _Models.seen[-1][0] == "/v1/models" and _Models.seen[-1][1]["authorization"] == f"Bearer {KEY}"
    missing = client.post("/api/models", json={"name": "ghost", "provider": "openai", "model": "ghost", "api_url": models_server}).json()["id"]
    said = client.post(f"/api/models/{missing}/test").json()
    assert said["ok"] is False and "not offered" in said["message"] and "served-1" in said["message"]
    _Models.status = 401
    assert "did not accept the key" in client.post(f"/api/models/{good}/test").json()["message"]


def test_testing_an_unreachable_address_says_so_without_leaking_anything(make_app):
    client = make_app(allow_private_model_urls=True)
    mid = client.post("/api/models", json={"name": "dead", "provider": "openai", "model": "m", "api_url": "http://127.0.0.1:9/v1/chat/completions", "api_key": KEY}).json()["id"]
    result = client.post(f"/api/models/{mid}/test").json()
    assert result["ok"] is False and "Could not reach the server" in result["message"] and KEY not in json.dumps(result)


# ------------------------------------------------------------------------------------------------ the helper itself

def test_the_secret_box_round_trips_and_refuses_a_wrong_key_or_a_tampered_value(tmp_path):
    from cryptography.fernet import Fernet
    box = SecretBox(Fernet.generate_key())
    token = box.encrypt(KEY)
    assert token != KEY and box.decrypt(token) == KEY and box.encrypt(KEY) != token        # a fresh random IV each time
    with pytest.raises(SecretBoxError):
        SecretBox(Fernet.generate_key()).decrypt(token)
    with pytest.raises(SecretBoxError):
        box.decrypt(token[:-4] + "AAAA")


def test_the_key_file_is_created_once_owner_only_and_reused(tmp_path):
    first = load_secretbox(tmp_path)
    path = tmp_path / KEY_FILE
    assert path.is_file()
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert load_secretbox(tmp_path).decrypt(first.encrypt("hello")) == "hello"            # same key on the next start
    with pytest.raises(SecretBoxError, match="SECRETS_KEY"):
        load_secretbox(tmp_path, "not-a-valid-key")
