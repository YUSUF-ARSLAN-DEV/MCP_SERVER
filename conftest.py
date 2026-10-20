"""Pipeline pytest plugin: evidence capture + result collection.

Active only when the pipeline runs pytest (it sets WTP_ARTIFACTS to the
workspace's artifacts dir). A plain `pytest` on the unit tests is unaffected.

- ``evidence_dir``: persistent per-test folder the generated specs write
  step screenshots into (via website_test_pipeline.evidence).
- a makereport hook that records every outcome to
  <WTP_ARTIFACTS>/test_results.json for report.py to consume.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ARTIFACTS = os.environ.get("WTP_ARTIFACTS")
ACTIVE = bool(_ARTIFACTS)
ARTIFACTS = Path(_ARTIFACTS) if _ARTIFACTS else Path.cwd() / "python_artifacts"
EVIDENCE_ROOT = ARTIFACTS / "evidence"
RESULTS_FILE = ARTIFACTS / "test_results.json"


def _slug(nodeid: str) -> str:
    """A filesystem-safe folder name for this test's evidence - capped well under Windows' 260-char MAX_PATH
    (a long nodeid otherwise produces a folder name so long that the file exists but neither Explorer nor a
    Word hyperlink can open it: confirmed live, a 341-char path to a real screenshot). A stable hash suffix
    (of the FULL, untruncated nodeid) keeps a truncated name unique and reproducible across write and read,
    since report.py's own _slug must derive the identical folder name to find this evidence again."""
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", nodeid).strip("-") or "test"
    if len(raw) <= 100:
        return raw
    return f"{raw[:91]}-{hashlib.sha1(nodeid.encode('utf-8')).hexdigest()[:8]}"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Start every test signed in when the pipeline has a saved session for the site (see authflow.py)."""
    state = os.environ.get("WTP_STORAGE_STATE")
    if state and Path(state).is_file():
        return {**browser_context_args, "storage_state": state}
    return browser_context_args


@pytest.fixture
def logged_out_page(browser, browser_context_args):
    """A page with NO saved session, for flows that sign in (a signed-in visitor is redirected away from /login)."""
    args = {k: v for k, v in browser_context_args.items() if k != "storage_state"}
    context = browser.new_context(**args)
    yield context.new_page()
    context.close()


@pytest.fixture
def evidence_dir(request) -> Path:
    directory = EVIDENCE_ROOT / _slug(request.node.nodeid)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def pytest_configure(config) -> None:
    # Give auto-retrying expect() calls longer on JS-heavy pages that render the
    # asserted element a few seconds after load. Only for pipeline runs.
    if ACTIVE:
        try:
            from playwright.sync_api import expect
            expect.set_options(timeout=12_000)
        except Exception:
            pass


_controller_merged_results: list[dict] = []   # xdist only - see pytest_testnodedown / pytest_sessionfinish


_SESSION_STARTED_AT: str | None = None   # set once per process - see pytest_sessionfinish for why this, not run.json


def pytest_sessionstart(session) -> None:
    global _SESSION_STARTED_AT
    session._collected_results = []
    if ACTIVE:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        if _SESSION_STARTED_AT is None:
            _SESSION_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if not ACTIVE:
        return
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


@pytest.hookimpl(optionalhook=True)   # xdist-only hook: without optionalhook, a run where xdist is not loaded fails plugin validation
def pytest_testnodedown(node, error) -> None:
    """xdist controller only: one worker process just finished. Fold the results it collected (handed over
    via workeroutput, since that worker's own pytest_sessionfinish does not write the shared file - see
    below) into the controller's merged list, before the controller's own pytest_sessionfinish runs."""
    if not ACTIVE:
        return
    results = (getattr(node, "workeroutput", None) or {}).get("results")
    if results:
        _controller_merged_results.extend(results)


def pytest_sessionfinish(session, exitstatus) -> None:
    if not ACTIVE:
        return
    if hasattr(session.config, "workerinput"):
        # An xdist worker: hand results to the controller instead of writing the shared file directly -
        # every worker process writing test_results.json would race, and whichever finished last would
        # silently discard every other worker's results. See pytest_testnodedown.
        session.config.workeroutput["results"] = getattr(session, "_collected_results", [])
        return
    # A plain (non-xdist) run, or the xdist controller after every worker has reported in above.
    results = getattr(session, "_collected_results", []) + _controller_merged_results
    payload = {
        "started_at": _SESSION_STARTED_AT or datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
