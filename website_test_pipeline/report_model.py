"""Canonical, validated data contract for stakeholder test reports.

The report renderer consumes this object rather than recomputing totals while
writing individual sections.  This keeps the DOCX, JSON export, and release
recommendation on one source of truth.
"""
from __future__ import annotations

import json
from datetime import date
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
    page_outcomes = [o for report in getattr(run, "url_reports", []) for o in report.outcomes]
    flow_outcomes = [f.outcome for f in getattr(run, "tested_flows", []) if f.outcome is not None]

    def invalid(o: Any) -> bool:
        # a failure caused by a fault in the test itself (report.py triage) is not an application result
        return o.status in {"failed", "error"} and bool(getattr(o, "invalid_reason", None))

    def ran(items: list, status: set[str]) -> int:
        return sum(o.status in status and not invalid(o) for o in items)

    page_tests = ran(page_outcomes, {"passed", "failed", "error"})
    flow_tests = ran(flow_outcomes, {"passed", "failed", "error"})
    outcomes = page_outcomes + flow_outcomes
    passed = ran(outcomes, {"passed"})
    failed = ran(outcomes, {"failed", "error"})
    skipped = sum(o.status == "skipped" for o in outcomes)
    test_defects = sum(invalid(o) for o in outcomes)
    blocked = len(getattr(run, "untested_flows", []))
    executed = passed + failed
    all_items = executed + skipped + blocked + test_defects
    pass_rate = round(100 * passed / executed) if executed else 0
    flows = list(getattr(run, "flow_reports", []))
    journeys_passed = sum(f.passed for f in flows)
    journeys_test_defect = sum(f.failed and invalid(f.outcome) for f in flows)
    journeys_failed = sum(f.failed for f in flows) - journeys_test_defect
    journeys_skipped = sum(f.tested and not f.passed and not f.failed for f in flows)
    journeys_check_failed = sum(f.verify_failed for f in flows if not f.tested)
    journeys_no_result = sum(not f.tested and not f.verify_failed for f in flows)
    journeys_total = len(flows)
    counts: dict[str, int | float] = {
        # Journeys are counted over ALL flows, so a blocked critical path lowers this number; the pass rate below
        # only covers tests that ran and can look high while journeys are untested.
        "journeys_total": journeys_total,
        "journeys_passed": journeys_passed,
        "journeys_failed": journeys_failed,
        "journeys_test_defect": journeys_test_defect,
        "journeys_skipped": journeys_skipped,
        "journeys_failed_last_check": journeys_check_failed,
        "journeys_no_result": journeys_no_result,
        "journeys_not_run": journeys_no_result,
        "journeys_passed_percent": round(100 * journeys_passed / journeys_total) if journeys_total else 0,
        "page_tests": page_tests,
        "flow_tests": flow_tests,
        "flows_with_result": len(flow_outcomes),
        "executed_tests": executed,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "test_defects": test_defects,
        "blocked_flows": blocked,
        "all_items": all_items,
        "pass_rate_percent": pass_rate,
    }
    validate_counts(counts)
    return counts


def validate_counts(counts: dict[str, Any]) -> None:
    """Every figure the report prints comes from one dict; these identities must hold before anything is rendered."""
    required = {"executed_tests", "passed", "failed", "skipped", "blocked_flows", "all_items"}
    missing = required - set(counts)
    if missing:
        raise ValueError(f"report counts missing fields: {', '.join(sorted(missing))}")
    invalid = counts.get("test_defects", 0)
    if counts["passed"] + counts["failed"] != counts["executed_tests"]:
        raise ValueError("report count invariant failed: passed + failed != executed_tests")
    if counts["executed_tests"] + counts["skipped"] + counts["blocked_flows"] + invalid != counts["all_items"]:
        raise ValueError("report count invariant failed: executed_tests + skipped + blocked_flows + test_defects != all_items")
    if counts["passed"] + counts["failed"] + counts["blocked_flows"] + counts["skipped"] + invalid != counts["all_items"]:
        raise ValueError("report count invariant failed: passed + failed + blocked + skipped + test_defects != all_items")
    if "page_tests" in counts and counts["page_tests"] + counts["flow_tests"] != counts["executed_tests"]:
        raise ValueError(f"report count invariant failed: page_tests ({counts['page_tests']}) + flow_tests "
                         f"({counts['flow_tests']}) != tests ran ({counts['executed_tests']})")
    if "journeys_total" in counts:
        parts = sum(counts.get(k, 0) for k in ("journeys_passed", "journeys_failed", "journeys_test_defect", "journeys_skipped",
                                               "journeys_failed_last_check", "journeys_no_result"))
        if parts != counts["journeys_total"]:
            raise ValueError(f"report count invariant failed: journeys passed/failed/skipped/no-result add up to {parts}, "
                             f"not the {counts['journeys_total']} journeys")
    if "flows_with_result" in counts and counts["flows_with_result"] != (
            counts["flow_tests"] + counts.get("journeys_skipped", 0) + counts.get("journeys_test_defect", 0)):
        raise ValueError("report count invariant failed: journeys with a result != flow tests + skipped + invalid flow tests")


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
    _validate_taxonomy(data)
    for defect in data["defects"]:
        missing = [key for key in REQUIRED_DEFECT_FIELDS if key not in defect]
        if missing:
            raise ValueError(f"defect {defect.get('id', '?')} missing fields: {', '.join(missing)}")
        if defect["id"] in ids:
            raise ValueError(f"duplicate defect id: {defect['id']}")
        ids.add(defect["id"])
        if defect["severity"] not in VALID_SEVERITIES:
            raise ValueError(f"invalid defect severity: {defect['severity']}")
        if str(defect["owner"]).strip().lower() in {"", "unassigned", "tbd", "n/a"}:
            raise ValueError(f"defect {defect['id']} has no real owner; name a person")
        if defect.get("due") is not None:
            try:
                date.fromisoformat(str(defect["due"]))
            except ValueError:
                raise ValueError(f"defect {defect['id']} due date {defect['due']!r} is not an ISO date")
        if (defect.get("screenshot_url") == "NOT CAPTURED" and defect.get("trace_url", "NOT CAPTURED") == "NOT CAPTURED"
                and not defect.get("evidence_reason")):
            raise ValueError(f"defect {defect['id']} has no evidence link and no reason for its absence")
        if defect["status"] not in VALID_STATUSES:
            raise ValueError(f"invalid defect status: {defect['status']}")
        if not isinstance(defect["steps_to_reproduce"], list) or not defect["steps_to_reproduce"]:
            raise ValueError(f"defect {defect['id']} must have reproduction steps")
        for field in REQUIRED_DEFECT_FIELDS:
            if defect[field] is None or defect[field] == "":
                raise ValueError(f"defect {defect['id']} has an empty {field}; use NOT CAPTURED")


def _validate_taxonomy(data: dict[str, Any]) -> None:
    """Verdict, per-type counts and the defect list must agree (only checked for data built by the report generator)."""
    tax = data.get("taxonomy")
    if tax is None:
        return
    records = data["defects"]
    for category, n in tax.items():
        if sum(r.get("category") == category for r in records) != n:
            raise ValueError(f"taxonomy count for {category} ({n}) does not match the records listed")
    if sum(tax.values()) != len(records):
        raise ValueError("taxonomy counts do not add up to the number of recorded items")
    from .report_policy import release_decision
    expected = release_decision(records)
    if data["recommendation"] != expected:
        raise ValueError(f"recommendation {data['recommendation']} does not follow from the Defects table ({expected})")
    if len(data["conditions"]) > 5:
        raise ValueError("more than five release conditions; collapse repeated items into counts")


def write_report_data(path: Path, data: dict[str, Any]) -> None:
    validate_report_data(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
