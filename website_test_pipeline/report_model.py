"""Canonical, validated data contract for stakeholder test reports.

The report renderer consumes this object rather than recomputing totals while
writing individual sections.  This keeps the DOCX, JSON export, and release
recommendation on one source of truth.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_DEFECT_FIELDS = (
    "id", "severity", "title", "description", "steps_to_reproduce",
    "expected", "actual", "screenshot_url", "status", "owner",
    "linked_flow_id",
)
VALID_SEVERITIES = {"Critical", "High", "Medium", "Low"}
VALID_STATUSES = {"New", "Open", "Fixed", "Retest", "Closed"}
VALID_RECOMMENDATIONS = {"GO", "NO-GO", "CONDITIONAL GO", "NO VERDICT"}


def canonical_counts(run: Any) -> dict[str, int | float]:
    """Return mutually explicit counts for every test item in a run.

    ``executed_tests`` contains only tests that actually ran and therefore is
    the denominator for pass rate.  Skipped tests and blocked flows are kept
    separate; ``all_items`` is the auditable total across all four statuses.
    """
    page_outcomes = [o for report in getattr(run, "url_reports", []) for o in report.outcomes]
    # A diagnosed-unstable flow (flow.quarantined, see flowreport.FlowReport) is excluded from the test-level
    # pass rate until it is fixed or cleared - this is the ONLY place that decision is made; every other count
    # (journeys_failed, the Defects table, the appendix index) reads quarantined_tests from here rather than
    # re-deriving its own notion of "does this count", which is how four separate instances of the same
    # quarantine-propagation bug shipped across four revisions before this one.
    quarantined_flows = [f for f in getattr(run, "tested_flows", []) if getattr(f, "quarantined", False)]
    flow_outcomes = [f.outcome for f in getattr(run, "tested_flows", [])
                     if f.outcome is not None and not getattr(f, "quarantined", False)]

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
    # Journeys are counted over ALL flows (quarantine never hides a journey's own pass/fail fact - only the
    # test-level pass rate above excludes it); a blocked critical path lowers journeys_total, and the pass
    # rate can look high while journeys are untested.
    journeys_passed = sum(f.passed for f in flows)
    journeys_test_defect = sum(f.failed and invalid(f.outcome) for f in flows)
    journeys_failed = sum(f.failed for f in flows) - journeys_test_defect
    journeys_skipped = sum(f.tested and not f.passed and not f.failed for f in flows)
    journeys_check_failed = sum(f.verify_failed for f in flows if not f.tested)
    journeys_no_result = sum(not f.tested and not f.verify_failed for f in flows)
    journeys_total = len(flows)
    journeys_with_result = journeys_passed + journeys_failed
    counts: dict[str, int | float] = {
        "journeys_total": journeys_total,
        "journeys_with_result": journeys_with_result,
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
        "quarantined": len(quarantined_flows),
        "quarantined_passed": sum(1 for f in quarantined_flows if f.passed),
        "quarantined_failed": sum(1 for f in quarantined_flows if f.failed),
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
    if "journeys_with_result" in counts and counts["journeys_with_result"] != counts["journeys_passed"] + counts["journeys_failed"]:
        raise ValueError(f"report count invariant failed: journeys_passed ({counts['journeys_passed']}) + journeys_failed "
                         f"({counts['journeys_failed']}) != journeys_with_result ({counts['journeys_with_result']})")
    if "journeys_with_result" in counts and counts["journeys_passed"] > counts["journeys_with_result"]:
        raise ValueError("report count invariant failed: journeys_passed exceeds journeys_with_result")
    # P3: "26 flow tests ran" and "27 journeys have a result" read as the same claim unless the quarantined
    # test is named as the difference - confirmed live: S4's own prose and its own table disagreed by exactly
    # the 1 quarantined flow test, both claiming to be "the same figure".
    if "journeys_with_result" in counts and "flow_tests" in counts:
        expected = counts["flow_tests"] + counts.get("quarantined", 0)
        if counts["journeys_with_result"] != expected:
            raise ValueError(f"report count invariant failed: flow_tests ({counts['flow_tests']}) + quarantined "
                             f"({counts.get('quarantined', 0)}) != journeys_with_result ({counts['journeys_with_result']})")
    if "appendix_index_rows" in counts:
        # _all_outcomes (report.py) includes a test_defect outcome too - it still ran and still gets a row,
        # it is just excluded from executed_tests because the test itself, not the application, was wrong.
        expected_rows = counts["executed_tests"] + counts["skipped"] + counts.get("quarantined", 0) + counts.get("test_defects", 0)
        if counts["appendix_index_rows"] != expected_rows:
            raise ValueError(f"appendix index would render {counts['appendix_index_rows']} rows, but "
                             f"executed_tests + skipped + quarantined + test_defects = {expected_rows}")


def validate_report_data(data: dict[str, Any]) -> None:
    """Fail before DOCX generation when the structured report is incomplete."""
    for key in ("schema_version", "generated_at", "counts", "recommendation", "conditions", "defects", "coverage"):
        if key not in data:
            raise ValueError(f"report data missing required field: {key}")
    validate_counts(data["counts"])
    _validate_delta_reconciles(data)
    _validate_timestamps(data)
    _validate_evidence_links(data)
    _validate_ledger_not_contradicted(data)
    _validate_severity_has_a_recorded_cause(data)
    _validate_cited_run_ids_match_their_timestamp(data)
    _validate_category_drift_has_a_row(data)
    _validate_exit_criteria_cover_every_capping_clause(data)
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


def _validate_delta_reconciles(data: dict[str, Any]) -> None:
    """The "change since the previous run" arithmetic (report.py's reconciliation sentence) must actually
    balance before it is ever rendered - a sentence that states its own mismatch is the exact failure mode
    this function exists to prevent (three prior revisions each shipped one)."""
    delta = data.get("delta") or {}
    if not delta.get("baseline"):
        return
    dropped = len(delta.get("removed", [])) - delta.get("renamed_count", 0)
    expected = (delta.get("previous_tests_ran", 0) - dropped + len(delta.get("added", []))
                - delta.get("quarantined_delta", 0) - delta.get("test_defect_delta", 0))
    actual = data["counts"]["executed_tests"]
    if expected != actual:
        from .report_policy import ReportConsistencyError   # imported here: report_policy must stay import-free of this module
        raise ReportConsistencyError(
            f"delta reconciliation does not balance: {delta.get('previous_tests_ran', 0)} (previous) - {dropped} "
            f"(removed, excluding renames) + {len(delta.get('added', []))} (added) - {delta.get('quarantined_delta', 0)} "
            f"(quarantine change) - {delta.get('test_defect_delta', 0)} (test-defect change) = {expected}, but this "
            f"run counted {actual}. The report is not emitted with a sentence that admits its own mismatch.")


def _validate_timestamps(data: dict[str, Any]) -> None:
    """started/finished/duration must describe the SAME run, not one carried over from an earlier manifest -
    confirmed live: three reruns once reported the identical generate-time timestamp."""
    started, finished, duration = data.get("run_started_at"), data.get("run_finished_at"), data.get("run_duration_seconds")
    if not started or not finished or duration is None:
        return
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return
    actual_duration = (f - s).total_seconds()
    if abs(actual_duration - duration) >= 1.0:
        raise ValueError(f"run timestamps are inconsistent: finished - started = {actual_duration:.2f}s, "
                         f"but duration is reported as {duration:.2f}s")


def _validate_severity_has_a_recorded_cause(data: dict[str, Any]) -> None:
    """C2: severity is set once, from user impact, when a defect is first confirmed - and that origin must
    always be rendered next to it (Severity: High (set 2026-10-08: reason)), never a bare value with no
    recorded cause, so a severity change (or a first setting) is never silent."""
    for defect in data.get("defects", []):
        if defect.get("category") != "defect":
            continue
        if not str(defect.get("severity_set_at") or "").strip():
            raise ValueError(f"defect {defect.get('id', '?')} has a severity ({defect.get('severity')}) with no "
                             "recorded severity_set_at - severity may not render without a cause")
        if not str(defect.get("severity_reason") or "").strip():
            raise ValueError(f"defect {defect.get('id', '?')} has a severity ({defect.get('severity')}) with no "
                             "recorded severity_reason - severity may not render without a cause")


def _validate_cited_run_ids_match_their_timestamp(data: dict[str, Any]) -> None:
    """D2: a run ID of the form run-YYYYMMDDHHMMSS encodes a timestamp (report_policy.run_id_for) - a report
    citing that run ID next to a DIFFERENT timestamp for the same event is internally inconsistent, even
    though each value was individually valid. Confirmed live: "last failed in run-20261008071045
    (2026-10-08T08:05:08)" - an encoded 07:10:45 next to a printed 08:05:08."""
    for defect in data.get("defects", []):
        run_id, run_at = defect.get("_cited_run_id"), defect.get("_cited_run_at")
        if not run_id or not run_at:
            continue
        match = re.fullmatch(r"run-(\d{14})", run_id)
        if not match:
            continue
        try:
            cited = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            printed = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if abs((cited - printed).total_seconds()) >= 60:
            raise ValueError(f"defect {defect.get('id', '?')} cites {run_id!r} (encodes {cited.isoformat()}) next "
                             f"to timestamp {run_at!r} - these describe the same event and must agree")


def _validate_exit_criteria_cover_every_capping_clause(data: dict[str, Any]) -> None:
    """P4: all four other exit criteria could be satisfied and GO would still be unreachable if requirements
    coverage stays below the threshold - the exit criteria list must say so, or a reader has no way to know
    that clause exists at all."""
    from .report_policy import REQUIREMENTS_BACKED_THRESHOLD, requirements_backed_percent
    backed_percent = requirements_backed_percent(data.get("requirements"))
    if backed_percent is None:
        return
    if backed_percent < REQUIREMENTS_BACKED_THRESHOLD and not any(
            "requirements" in c.lower() for c in data.get("exit_criteria", [])):
        raise ValueError(f"requirements coverage ({backed_percent:.0f}%) is below the "
                         f"{REQUIREMENTS_BACKED_THRESHOLD:.0f}% threshold that caps the verdict, but no exit "
                         "criterion says so")


def _validate_category_drift_has_a_row(data: dict[str, Any]) -> None:
    """P2: a category count (Defect/Unverified/Limitation/Tooling/Test defect) may not differ from the
    previous run without at least one category_changes row explaining a move - confirmed live: the table was
    entirely absent in a run where a Defect genuinely left the ledger."""
    delta = data.get("delta") or {}
    prev_tax = delta.get("previous_taxonomy")
    tax = data.get("taxonomy")
    if not delta.get("baseline") or prev_tax is None or tax is None:
        return
    changed = {cat for cat in tax if prev_tax.get(cat, 0) != tax.get(cat, 0)}
    # A category count can also move because a test was added or removed (each already disclosed in its own
    # table, with its own reason) - only flag a drift that NOTHING explains: no reclassification, and no
    # test-count change at all that could plausibly account for it.
    if changed and not delta.get("category_changes") and not delta.get("added") and not delta.get("removed"):
        raise ValueError(f"categor{'y' if len(changed) == 1 else 'ies'} {', '.join(sorted(changed))} changed "
                         "count since the previous run, but no category_changes row, added test or removed "
                         "test explains it")


def _validate_ledger_not_contradicted(data: dict[str, Any]) -> None:
    """C1: the ledger is the single authority on whether a defect is still open. No test may be described as
    "resolved"/"now passes clean" in the category-changes delta while an open DEF-* record for the same test
    says otherwise - confirmed live: a report called DEF-001 resolved, open, and release-blocking at once."""
    open_keys = {d["_stable_key"] for d in data.get("defects", [])
                if d.get("status") == "Open" and d.get("category") == "defect" and "_stable_key" in d}
    for change in (data.get("delta") or {}).get("category_changes", []):
        if change["id"] in open_keys and str(change.get("reason", "")).startswith("resolved"):
            raise ValueError(f"category change for {change['id']!r} says 'resolved' while an open DEF-* record "
                             "for the same test still exists - the ledger is the single authority on defect "
                             "state and must not be contradicted elsewhere in the same render")


def _validate_evidence_links(data: dict[str, Any]) -> None:
    """An evidence link that resolves for nobody is worse than no link: a reader clicks it, it does nothing,
    and now they distrust every other link in the report too. Confirmed live: trace/video hrefs pointing
    eight ../ segments into a Windows temp directory that is wiped on reboot.

    What makes a link unresolvable is its SHAPE: an absolute path (it only exists on the machine that wrote it) or one that
    climbs out of the run's folder. It is not the words in it: a server may keep every run under a temp folder, and an
    in-tree link such as ../evidence/AppData-Local-Temp-...-tests-x/01.png (a folder named after the test's path) is fine.
    The old check looked for "AppData" / "Temp/" anywhere in the link and so refused every report from such a server."""
    base = os.environ.get("ARTIFACT_BASE_URL", "").strip()
    for defect in data.get("defects", []):
        for key in ("screenshot_url", "trace_url", "video_url"):
            href = defect.get(key)
            if not href or href == "NOT CAPTURED" or base:
                continue
            link = str(href).replace("\\", "/")
            climbs = len(re.match(r"(?:\.\./)*", link).group(0)) // 3
            if re.match(r"^(?:[A-Za-z]:|/|file:|//)", link) or climbs > 3:
                raise ValueError(f"defect {defect.get('id', '?')} has an evidence link that cannot resolve for a "
                                 f"reader ({key}={href!r}); copy the artifact into the run's own evidence folder "
                                 "or set ARTIFACT_BASE_URL")


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
    # B2: open_defects is a LEDGER quantity, checked against the Defects table here, and must never be used
    # as a term in an arithmetic identity (passed + failed == tests_ran uses counts["failed"], never this).
    open_defects = sum(r.get("category") == "defect" and r.get("status") == "Open" for r in records)
    if "open_defects" in data.get("counts", {}) and data["counts"]["open_defects"] != open_defects:
        raise ValueError(f"counts.open_defects ({data['counts']['open_defects']}) != open DEF-* "
                         f"records this run ({open_defects}) - every 'application defects' figure must read "
                         "this count, never the test-level pass/fail tally")
    from .report_policy import release_decision, requirements_backed_percent
    backed_percent = requirements_backed_percent(data.get("requirements"))
    expected = release_decision(records, requirements_backed_percent=backed_percent,
                                executed_tests=(data.get("counts") or {}).get("executed_tests"))
    if data["recommendation"] != expected:
        raise ValueError(f"recommendation {data['recommendation']} does not follow from the Defects table and "
                         f"requirements coverage ({expected})")
    if len(data["conditions"]) > 5:
        raise ValueError("more than five release conditions; collapse repeated items into counts")


def write_report_data(path: Path, data: dict[str, Any]) -> None:
    validate_report_data(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
