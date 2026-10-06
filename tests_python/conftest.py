"""Shared test setup: report tests must not launch a real browser just to read its version."""
import os

import pytest

os.environ.setdefault("WTP_SKIP_BROWSER_PROBE", "1")


@pytest.fixture(autouse=True)
def _default_report_owner(monkeypatch):
    """A test default, re-applied before every test rather than once at collection time, because
    website_test_pipeline.config re-runs .env's load_dotenv(override=True) on every import AND on every
    importlib.reload (test_datadir.py reloads it deliberately, to test DATA_DIR) - either one clobbers a
    plain os.environ.setdefault() done once up front. monkeypatch.setenv also means a test that wants the
    "no owner configured" failure path can still monkeypatch.delenv it for just that test, same as before."""
    monkeypatch.setenv("REPORT_OWNER", "Test Owner")
