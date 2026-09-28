"""Canonical, validated data contract for stakeholder test reports.

The report renderer consumes this object rather than recomputing totals while
writing individual sections.  This keeps the DOCX, JSON export, and release
recommendation on one source of truth.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_DEFECT_FIELDS = (
    "id", "severity", "title", "description", "steps_to_reproduce",
    "expected", "actual", "screenshot_url", "status", "owner",
    "linked_flow_id",
)
VALID_SEVERITIES = {"Critical", "High", "Medium", "Low"}
VALID_STATUSES = {"New", "Open", "Fixed", "Retest", "Closed"}
VALID_RECOMMENDATIONS = {"GO", "NO-GO", "CONDITIONAL GO"}


def canonical_counts(run: Any) -> dict[str, int | float]:
    """Return mutually explicit counts for every test item in a run.

    ``executed_tests`` contains only tests that actually ran and therefore is
    the denominator for pass rate.  Skipped tests and blocked flows are kept
    separate; ``all_items`` is the auditable total across all four statuses.
    """
    outcomes = []
    for report in getattr(run, "url_reports", []):
        outcomes.extend(report.outcomes)
    for flow in getattr(run, "tested_flows", []):
        if flow.outcome is not None:
            outcomes.append(flow.outcome)

    passed = sum(o.status == "passed" for o in outcomes)
    failed = sum(o.status in {"failed", "error"} for o in outcomes)
    skipped = sum(o.status == "skipped" for o in outcomes)
    blocked = len(getattr(run, "untested_flows", []))
    executed = passed + failed
    all_items = executed + skipped + blocked
    pass_rate = round(100 * passed / executed) if executed else 0
    counts: dict[str, int | float] = {
        "executed_tests": executed,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "blocked_flows": blocked,
        "all_items": all_items,
        "pass_rate_percent": pass_rate,
    }
    validate_counts(counts)
    return counts


def validate_counts(counts: dict[str, Any]) -> None:
    required = {"executed_tests", "passed", "failed", "skipped", "blocked_flows", "all_items"}
    missing = required - set(counts)
    if missing:
        raise ValueError(f"report counts missing fields: {', '.join(sorted(missing))}")
    if counts["passed"] + counts["failed"] != counts["executed_tests"]:
        raise ValueError("report count invariant failed: passed + failed != executed_tests")
    if counts["executed_tests"] + counts["skipped"] + counts["blocked_flows"] != counts["all_items"]:
        raise ValueError("report count invariant failed: executed_tests + skipped + blocked_flows != all_items")
    if counts["passed"] + counts["failed"] + counts["blocked_flows"] + counts["skipped"] != counts["all_items"]:
        raise ValueError("report count invariant failed: passed + failed + blocked + skipped != all_items")


def validate_report_data(data: dict[str, Any]) -> None:
    """Fail before DOCX generation when the structured report is incomplete."""
    for key in ("schema_version", "generated_at", "counts", "recommendation", "conditions", "defects", "coverage"):
        if key not in data:
            raise ValueError(f"report data missing required field: {key}")
    validate_counts(data["counts"])
    recommendation = data["recommendation"]
    if recommendation not in VALID_RECOMMENDATIONS:
        raise ValueError(f"invalid release recommendation: {recommendation}")
    conditions = data["conditions"]
    if recommendation == "CONDITIONAL GO" and not conditions:
        raise ValueError("CONDITIONAL GO requires at least one release condition")
    if not isinstance(conditions, list):
        raise ValueError("report conditions must be a list")
    ids = set()
    for defect in data["defects"]:
        missing = [key for key in REQUIRED_DEFECT_FIELDS if key not in defect]
        if missing:
            raise ValueError(f"defect {defect.get('id', '?')} missing fields: {', '.join(missing)}")
        if defect["id"] in ids:
            raise ValueError(f"duplicate defect id: {defect['id']}")
        ids.add(defect["id"])
        if defect["severity"] not in VALID_SEVERITIES:
            raise ValueError(f"invalid defect severity: {defect['severity']}")
        if defect["status"] not in VALID_STATUSES:
            raise ValueError(f"invalid defect status: {defect['status']}")
        if not isinstance(defect["steps_to_reproduce"], list) or not defect["steps_to_reproduce"]:
            raise ValueError(f"defect {defect['id']} must have reproduction steps")
        for field in REQUIRED_DEFECT_FIELDS:
            if defect[field] is None or defect[field] == "":
                raise ValueError(f"defect {defect['id']} has an empty {field}; use NOT CAPTURED")


def write_report_data(path: Path, data: dict[str, Any]) -> None:
    validate_report_data(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
