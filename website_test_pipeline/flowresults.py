"""Feed the results of the generated flow tests back into flow_ratings.json.

`execute` / `report` run pytest, and conftest.py records every outcome in
test_results.json. Here each result of a flow_<id>_test.py file is matched to its flow
and appended to that flow's rating history as source=pytest, then the flow's status is
re-derived from the whole history (ratings.derive_status). Nothing is ever deleted, and
feeding the same results twice adds nothing.
"""
from __future__ import annotations
import json
import re

from .flowgen import file_name
from .flows import FlowsFileError, load_flows, save_flows
from .ratings import RatingsFileError, append_rating, derive_status, load_ratings, save_ratings

_RAN = {"passed", "failed", "error"}


def match_results(results: dict, flows: list[dict]) -> dict[str, dict]:
    """flow id -> the test row for its generated file. A failed row wins over a passed one."""
    by_file = {file_name(f): f["id"] for f in flows}
    matched: dict[str, dict] = {}
    for row in results.get("tests", []):
        if row.get("status") not in _RAN:
            continue
        base = re.split(r"[\\/]", str(row.get("nodeid", "")).split("::")[0])[-1]
        flow_id = by_file.get(base)
        if flow_id is None:
            continue
        if flow_id not in matched or row["status"] != "passed":
            matched[flow_id] = row
    return matched


def _reason(error: str | None) -> str:
    text = error or ""
    found = re.search(r"^E\s+(.+)$", text, re.M)
    line = found.group(1) if found else next((ln for ln in text.splitlines() if ln.strip()), "")
    return line.strip()[:200]


def apply_pytest_results(doc: dict, ratings: dict, results: dict, now: str) -> list[tuple[str, str, str]]:
    """Record the results; returns [(flow id, old status, new status)] for flows whose status changed."""
    at = results.get("finished_at") or now
    by_id = {f["id"]: f for f in doc["flows"]}
    changes = []
    for flow_id, row in match_results(results, doc["flows"]).items():
        entries = ratings["ratings"].setdefault(flow_id, [])
        last = next((e for e in reversed(entries) if e.get("source") == "pytest"), None)
        if last and last.get("at") == at:
            continue  # this run was already recorded
        entry = {"source": "pytest", "at": at, "passed": row["status"] == "passed", "test": row.get("nodeid", "")}
        if not entry["passed"]:
            entry["error"] = _reason(row.get("error"))
        append_rating(ratings, flow_id, entry)
        flow = by_id[flow_id]
        old = flow.get("status")
        flow["status"] = derive_status(old, entries)
        if flow["status"] != old:
            changes.append((flow_id, old, flow["status"]))
    return changes


def feed_results(settings, log) -> None:
    """Best effort: a problem here must never fail execute/report."""
    from datetime import datetime, timezone
    results_file = settings.artifacts_dir / "test_results.json"
    if not results_file.exists() or not settings.flows_file.exists():
        return
    try:
        results = json.loads(results_file.read_text(encoding="utf-8"))
        doc = load_flows(settings.flows_file)
        ratings = load_ratings(settings.ratings_file)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        before = sum(len(v) for v in ratings["ratings"].values())
        changes = apply_pytest_results(doc, ratings, results, now)
        added = sum(len(v) for v in ratings["ratings"].values()) - before
        if added:
            save_ratings(settings.ratings_file, ratings)
            save_flows(settings.flows_file, doc)
        for flow_id, old, new in changes:
            log.info("flows: %s %s -> %s", flow_id, old, new)
        log.info("FLOW RESULTS recorded=%d status_changes=%d", added, len(changes))
    except (OSError, ValueError, FlowsFileError, RatingsFileError) as exc:
        log.warning("flows: could not record flow test results (%s)", exc)
