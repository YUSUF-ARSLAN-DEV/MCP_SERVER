"""A run is one person's. The server's own credentials - its .env, other tools' keys, test-site logins - must not be
handed to it just because it runs on the same machine."""
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.jobs import run_environment
from api.main import create_app
from api.settings import AppSettings

SERVER_SECRETS = {
    "API_KEY": "server-model-key-aaa1", "GEMINI_API_KEY": "server-gemini-key-bbb2", "GEMINI_PROJECT": "server-gemini-project-ccc3",
    "AUTH_THE_INTERNET_HEROKUAPP_COM_DEFAULT_PASSWORD": "tomsmith-secret-ddd4", "AUTH_X_DEFAULT_USERNAME": "admin-user-eee5",
    "ACCESS_CODE": "bootstrap-code-fff6", "SESSION_SECRET": "signing-secret-ggg7", "SECONDARY_MODEL": "other-model-hhh8",
    "MY_SERVICE_TOKEN": "service-token-iii9", "GITHUB_TOKEN": "github-token-jjj0", "DB_PASSWORD": "db-password-kkk1",
    "AWS_SECRET_ACCESS_KEY": "aws-secret-lll2",
}
HARMLESS = {"PATH": "/usr/bin", "SystemRoot": "C:\\Windows", "TEMP": "/tmp", "CRAWL_MAX_PAGES": "5", "EXPLORE_PROBE_MAX": "3",
            "MODEL_TIMEOUT_MS": "300000", "MODEL_RETRIES": "4", "HEADLESS": "true", "REPORT_OWNER": "Someone"}


def test_secrets_stay_behind_and_ordinary_settings_come_along():
    env = run_environment({**SERVER_SECRETS, **HARMLESS, "API_URL": "https://s/v1", "MODEL_NAME": "m", "MODEL_PROVIDER": "openai"}, keep_server_model=False)
    assert set(env) == set(HARMLESS)


def test_a_run_that_opts_into_the_servers_model_gets_only_the_model_variables_not_the_other_secrets():
    env = run_environment({**SERVER_SECRETS, **HARMLESS, "API_URL": "https://s/v1", "MODEL_NAME": "m", "MODEL_PROVIDER": "openai"}, keep_server_model=True)
    assert env["API_KEY"] == "server-model-key-aaa1" and env["API_URL"] == "https://s/v1" and env["MODEL_NAME"] == "m"
    assert set(env) - set(HARMLESS) == {"API_KEY", "API_URL", "MODEL_NAME", "MODEL_PROVIDER"}


FAKE = r"""
import json, os
from pathlib import Path
run = Path(os.environ["DATA_DIR"]) / "runs" / os.environ["SITE"]
(run / "artifacts").mkdir(parents=True, exist_ok=True)
(run / "artifacts" / "env.json").write_text(json.dumps(dict(os.environ)))
"""


def test_a_real_run_process_receives_none_of_the_servers_secrets_and_its_report_names_its_owner(tmp_path, monkeypatch):
    for name, value in {**SERVER_SECRETS, **HARMLESS}.items():
        monkeypatch.setenv(name, value)
    settings = AppSettings(data_dir=tmp_path, command=[sys.executable, "-c", FAKE], poll_interval_s=0.05, idle_interval_s=0.05)
    with TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"])) as client:
        assert client.post("/api/signup", json={"username": "alice", "password": "correct horse battery"}).status_code == 200
        job = client.post("/api/jobs", json={"url": "example.com", "model": {"provider": "anthropic", "model": "claude-x", "api_key": "alices-own-key"}}).json()
        deadline = time.monotonic() + 20
        while client.get(f"/api/jobs/{job['id']}").json()["status"] not in ("done", "failed") and time.monotonic() < deadline:
            time.sleep(0.05)
    env = json.loads((tmp_path / "runs" / job["site"] / "artifacts" / "env.json").read_text())
    blob = json.dumps(env)
    for secret in SERVER_SECRETS.values():
        assert secret not in blob, secret
    assert env["API_KEY"] == "alices-own-key" and env["MODEL_PROVIDER"] == "anthropic"
    assert env["REPORT_OWNER"] == "alice"                                  # not "Someone", whoever set the server up
    assert env["CRAWL_MAX_PAGES"] and "PATH" in env
