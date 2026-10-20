"""A person can pick the model for one run from the web page: provider, model, address, key. The key travels to the
run's environment and nowhere else - not the database, not any response, not a file that outlives the run."""
import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.settings import AppSettings

CODE = "open-sesame"
KEY = "sk-test-SECRET-1234567890"

FAKE_PIPELINE = r"""
import json, os, time
from pathlib import Path
run = Path(os.environ["DATA_DIR"]) / "runs" / os.environ["SITE"]
(run / "artifacts").mkdir(parents=True, exist_ok=True)
(run / "artifacts" / "model_env.json").write_text(json.dumps({k: os.environ.get(k) for k in ("MODEL_PROVIDER", "MODEL_NAME", "API_URL", "API_KEY")}))
if "slow" in os.environ["SEED_URL"]:
    time.sleep(60)
"""


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    for name in ("MODEL_PROVIDER", "MODEL_NAME", "API_URL", "API_KEY"):
        monkeypatch.delenv(name, raising=False)
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


def finish(client, job_id, statuses=("done", "failed", "cancelled")):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"stuck: {job}")


def model_env(tmp_path, job):
    return json.loads((tmp_path / "runs" / job["site"] / "artifacts" / "model_env.json").read_text())


@pytest.mark.parametrize("choice,expected_url", [
    ({"provider": "openai", "model": "qwen3-coder", "api_url": "https://llm.example.org/v1/chat/completions", "api_key": KEY},
     "https://llm.example.org/v1/chat/completions"),
    ({"provider": "anthropic", "model": "claude-x", "api_key": KEY}, "https://api.anthropic.com"),
    ({"provider": "google", "model": "gemini-2.5-flash", "api_key": KEY}, "https://generativelanguage.googleapis.com"),
])
def test_each_provider_reaches_the_run_with_its_model_address_and_key(make_client, tmp_path, choice, expected_url):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "example.com", "model": choice}).json()
    assert finish(client, job["id"])["status"] == "done"
    assert model_env(tmp_path, job) == {"MODEL_PROVIDER": choice["provider"], "MODEL_NAME": choice["model"], "API_URL": expected_url, "API_KEY": KEY}


def test_the_key_is_in_no_response_no_database_row_and_no_file_once_the_run_started(make_client, tmp_path):
    client = make_client()
    created = client.post("/api/jobs", json={"url": "example.com", "model": {"provider": "google", "model": "gemini-x", "api_key": KEY}})
    assert KEY not in created.text
    job = created.json()
    done = finish(client, job["id"])
    assert KEY not in json.dumps(done) and KEY not in client.get("/api/jobs").text
    with sqlite3.connect(tmp_path / "app.db") as conn:
        rows = [str(r) for r in conn.execute("SELECT * FROM jobs")]
    assert not any(KEY in r for r in rows)
    stored = json.loads(sqlite3.connect(tmp_path / "app.db").execute("SELECT options FROM jobs").fetchone()[0])
    assert stored["model"] == {"provider": "google", "model": "gemini-x", "api_url": "", "key_set": True}
    assert not (tmp_path / "runs" / job["site"] / "secrets").exists()
    leaked = [p for p in (tmp_path / "runs" / job["site"]).rglob("*") if p.is_file() and KEY in p.read_text(errors="ignore")
              and p.name != "model_env.json"]
    assert leaked == []


def test_cancelling_a_run_leaves_no_key_behind(make_client, tmp_path):
    client = make_client()
    job = client.post("/api/jobs", json={"url": "slow.example.com", "model": {"provider": "anthropic", "model": "claude-x", "api_key": KEY}}).json()
    finish(client, job["id"], statuses=("running",))
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert not (tmp_path / "runs" / job["site"] / "secrets").exists()


@pytest.mark.parametrize("choice,reason", [
    ({"provider": "anthropic", "model": "claude-x"}, "API_KEY"),
    ({"provider": "google", "model": "gemini-x", "api_key": " "}, "API_KEY"),
    ({"provider": "openai", "model": "m"}, "chat-completions address"),
    ({"provider": "openai", "model": "m", "api_url": "http://localhost:11434/v1/chat/completions"}, "Model address"),
    ({"provider": "openai", "model": "m", "api_url": "http://169.254.169.254/v1"}, "Model address"),
    ({"provider": "openai", "model": "m", "api_url": "ftp://x.test/v1"}, "Model address"),
])
def test_an_incomplete_or_unsafe_model_choice_is_refused_with_a_reason(make_client, tmp_path, choice, reason):
    client = make_client()
    refused = client.post("/api/jobs", json={"url": "example.com", "model": choice})
    assert refused.status_code == 422 and reason in refused.text
    assert client.get("/api/jobs").json() == [] and not (tmp_path / "runs").exists()      # nothing was queued or written


@pytest.mark.parametrize("bad", [{"provider": "skynet", "model": "m"}, {"provider": "openai", "model": ""},
                                 {"provider": "openai", "model": "bad name; rm -rf /"}])
def test_an_unknown_provider_or_a_malformed_model_name_is_refused(make_client, bad):
    assert make_client().post("/api/jobs", json={"url": "example.com", "model": bad}).status_code == 422


def test_a_local_model_server_is_allowed_only_when_the_operator_says_so(make_client, tmp_path):
    local = {"provider": "openai", "model": "llama3", "api_url": "http://localhost:11434/v1/chat/completions"}
    client = make_client(allow_private_model_urls=True)
    job = client.post("/api/jobs", json={"url": "example.com", "model": local})
    assert job.status_code == 201
    finish(client, job.json()["id"])
    assert model_env(tmp_path, job.json())["API_URL"] == "http://localhost:11434/v1/chat/completions"


def test_every_run_must_name_its_own_model_there_is_no_server_default(make_client, tmp_path):
    client = make_client()
    refused = client.post("/api/jobs", json={"url": "example.com"})
    assert refused.status_code == 422 and "Choose a model" in refused.text
    assert client.get("/api/jobs").json() == [] and not (tmp_path / "runs").exists()


def test_a_server_that_opts_out_lets_a_run_use_its_own_model_for_one_person_on_their_own_machine(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv("API_URL", "https://own.example/v1/chat/completions")
    monkeypatch.setenv("MODEL_NAME", "own-model")
    monkeypatch.setenv("API_KEY", "own-key")
    client = make_client(require_model=False)
    job = client.post("/api/jobs", json={"url": "example.com"}).json()
    finish(client, job["id"])
    assert model_env(tmp_path, job) == {"MODEL_PROVIDER": None, "MODEL_NAME": "own-model", "API_URL": "https://own.example/v1/chat/completions", "API_KEY": "own-key"}


def test_a_run_with_its_own_model_never_sees_the_servers_model_key(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "THE-SERVERS-KEY")
    monkeypatch.setenv("API_URL", "https://server.example/v1/chat/completions")
    monkeypatch.setenv("MODEL_NAME", "servers-model")
    client = make_client()
    job = client.post("/api/jobs", json={"url": "example.com", "model": {"provider": "google", "model": "gemini-x", "api_key": KEY}}).json()
    finish(client, job["id"])
    env = model_env(tmp_path, job)
    assert env["API_KEY"] == KEY and env["MODEL_NAME"] == "gemini-x" and "THE-SERVERS-KEY" not in json.dumps(env)
