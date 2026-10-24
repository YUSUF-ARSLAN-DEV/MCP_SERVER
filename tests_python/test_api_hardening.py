import sys

import pytest
from fastapi.testclient import TestClient

from api import hardening
from api.main import create_app
from api.settings import AppSettings


@pytest.fixture
def make_client(tmp_path):
    opened = []

    def make(base_url="http://testserver", **overrides):
        settings = AppSettings(require_model=False, data_dir=tmp_path, command=[sys.executable, "-c", "pass"], **overrides)
        client = TestClient(create_app(settings, resolver=lambda host: ["93.184.216.34"]), base_url=base_url)
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def test_every_response_carries_the_headers_that_close_the_page_off(make_client):
    h = make_client().get("/api/health").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["content-security-policy"] and "default-src 'self'" in h["content-security-policy"]
    assert h["cache-control"] == "no-store"                            # a person's runs and reports are never cached
    assert "strict-transport-security" not in h                        # plain http: HSTS would be meaningless


def test_the_web_page_itself_is_protected_too(make_client):
    response = make_client().get("/")
    assert response.status_code == 200 and "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_hsts_is_sent_over_https_and_the_session_cookie_is_secure_without_anyone_setting_a_flag(make_client):
    client = make_client(base_url="https://testserver")
    assert "strict-transport-security" in client.get("/api/health").headers
    made = client.post("/api/signup", json={"username": "tester1", "password": "correct horse battery"})
    assert made.status_code == 200 and "secure" in made.headers["set-cookie"].lower() and "httponly" in made.headers["set-cookie"].lower()


def test_a_session_cookie_over_plain_http_is_not_marked_secure_so_a_local_install_still_works(make_client):
    made = make_client().post("/api/signup", json={"username": "tester1", "password": "correct horse battery"})
    assert made.status_code == 200 and "secure" not in made.headers["set-cookie"].lower()


def test_a_trusted_proxy_saying_https_makes_the_cookie_secure(make_client):
    client = make_client(trust_proxy=True)
    made = client.post("/api/signup", json={"username": "tester1", "password": "correct horse battery"}, headers={"X-Forwarded-Proto": "https"})
    assert "secure" in made.headers["set-cookie"].lower()


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://testserver.evil.example"])
def test_a_write_from_another_site_is_refused_before_any_route_runs(make_client, origin):
    response = make_client().post("/api/login", json={"username": "x", "password": "y"}, headers={"Origin": origin})
    assert response.status_code == 403 and "Cross-site" in response.text


def test_a_write_from_this_servers_own_page_is_accepted_and_so_is_one_with_no_origin_at_all(make_client):
    client = make_client()
    assert client.post("/api/login", json={"username": "x", "password": "y"}, headers={"Origin": "http://testserver"}).status_code == 401
    assert client.post("/api/login", json={"username": "x", "password": "y"}).status_code == 401      # curl, the MCP adapter, tests


def test_reads_are_never_blocked_by_the_origin_rule(make_client):
    assert make_client().get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 200


def test_a_huge_request_is_refused_up_front(make_client):
    response = make_client().post("/api/login", content=b"{}", headers={"Content-Length": str(hardening.MAX_BODY_BYTES + 1), "Content-Type": "application/json"})
    assert response.status_code == 413


def test_extra_origins_can_be_allowed_for_a_proxy_that_rewrites_the_host():
    extra = hardening.allowed_origins({"ALLOWED_ORIGINS": "https://tester.example.com/, https://b.example.com"})
    assert hardening.is_same_origin("https://tester.example.com", "10.0.0.5:8000", extra)
    assert not hardening.is_same_origin("https://evil.example", "10.0.0.5:8000", extra)


def _job(**options):
    return {"options": {"max_pages": 3, **options}, "user_id": None, "url": "https://example.com/", "site": "example.com-abc123", "run_dir": "x"}


def test_a_run_started_by_the_api_may_only_browse_the_public_internet_unless_the_operator_allows_private_targets(tmp_path):
    from api.db import Database
    from api.jobs import JobRunner
    settings = AppSettings(require_model=False, data_dir=tmp_path)
    runner = JobRunner(Database(settings.db_path), settings)
    assert runner.environment(_job())["WTP_BLOCK_PRIVATE"] == "1"
    allowed = AppSettings(require_model=False, data_dir=tmp_path, allow_private_targets=True)
    assert "WTP_BLOCK_PRIVATE" not in JobRunner(Database(allowed.db_path), allowed).environment(_job())
