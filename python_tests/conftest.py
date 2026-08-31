"""Fixtures and hooks that turn a pytest run into report source data.

- ``evidence_dir``: a persistent per-test folder the generated specs write
  step screenshots into (via website_test_pipeline.evidence).
- a makereport hook that records every test outcome to
  python_artifacts/test_results.json for report.py to consume.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "python_artifacts"
EVIDENCE_ROOT = ARTIFACTS / "evidence"
RESULTS_FILE = ARTIFACTS / "test_results.json"


def _slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", nodeid).strip("-") or "test"


@pytest.fixture
def evidence_dir(request) -> Path:
    directory = EVIDENCE_ROOT / _slug(request.node.nodeid)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def pytest_sessionstart(session) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    session._collected_results = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" and not (report.when == "setup" and report.outcome != "passed"):
        return

    module = getattr(item, "module", None)
    doc = (item.function.__doc__ or "").strip() if getattr(item, "function", None) else ""
    status = report.outcome  # passed | failed | skipped
    if report.when == "setup" and report.failed:
        status = "error"
    record = {
        "nodeid": item.nodeid,
        "title": doc or item.nodeid.split("::")[-1].split("[")[0],
        "url": getattr(module, "URL", None),
        "status": status,
        "duration": round(getattr(report, "duration", 0.0), 3),
        "error": None if report.passed else _text(report.longrepr),
    }
    results = getattr(item.session, "_collected_results", None)
    if results is not None:
        results.append(record)


def pytest_sessionfinish(session, exitstatus) -> None:
    results = getattr(session, "_collected_results", [])
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exit_status": int(exitstatus),
        "totals": {
            "total": len(results),
            "passed": sum(r["status"] == "passed" for r in results),
            "failed": sum(r["status"] in {"failed", "error"} for r in results),
        },
        "tests": results,
    }
    RESULTS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _text(longrepr) -> str:
    if longrepr is None:
        return ""
    text = str(longrepr)
    return text if len(text) <= 8000 else text[:4000] + "\n...\n" + text[-3000:]
