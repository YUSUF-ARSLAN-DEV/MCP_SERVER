"""Synthetic reconstructions of the two previous (known-bad) runs an external review found live, built from
the review's own quoted evidence - not real files, since those runs predate this ledger/schema. Used to prove
the Part A golden-file suite fails on exactly the regressions it was written to catch."""
import json
from pathlib import Path

HERE = Path(__file__).parent


def _defect(id_="DEF-001", severity="Medium", **over):
    d = {"id": id_, "category": "defect", "status": "Open", "severity": severity, "title": "t",
        "description": "d", "steps_to_reproduce": ["s"], "expected": "e", "actual": "a",
        "screenshot_url": "NOT CAPTURED", "trace_url": "NOT CAPTURED", "evidence_reason": "none",
        "owner": "Owner", "due": "2026-01-01", "linked_flow_id": "NOT CAPTURED",
        "severity_set_at": "2026-01-01", "severity_reason": "r", "blocking": severity in {"High", "Critical"}}
    d.update(over)
    return d


def _base(**over):
    d = {"schema_version": "2.0", "generated_at": "2026-10-08T07:10:45+00:00", "run_id": "run-20261008071045",
        "run_started_at": "2026-10-08T07:05:18+00:00", "run_finished_at": "2026-10-08T07:10:45+00:00",
        "run_duration_seconds": 327.0, "recommendation": "CONDITIONAL GO", "conditions": ["fix it"],
        "defects": [], "coverage": {}, "taxonomy": {"defect": 0, "unverified": 0, "limitation": 0,
                                                     "test_defect": 0, "tooling": 0},
        "delta": {"baseline": False, "note": "first run"}}
    d.update(over)
    return d


# run-20261008071045: the reconciliation line self-contradicts (=71, this run counted 70); Failed
# (application)=0 next to a real open DEF-001; the appendix index holds 74 rows against a headline of 70;
# DEF-001's trace/video hrefs point into a Windows AppData temp directory.
bad_071045 = _base(
    counts={"executed_tests": 70, "passed": 70, "failed": 0, "skipped": 3, "blocked_flows": 9,
           "test_defects": 0, "quarantined": 0, "quarantined_passed": 0, "quarantined_failed": 0,
           "all_items": 82, "page_tests": 44, "flow_tests": 26, "journeys_total": 35,
           "journeys_passed": 26, "journeys_failed": 0, "journeys_with_result": 26,
           "journeys_test_defect": 0, "journeys_skipped": 0, "journeys_failed_last_check": 0,
           "journeys_no_result": 9, "journeys_not_run": 9, "journeys_passed_percent": 74,
           "flows_with_result": 26, "pass_rate_percent": 100,
           "defects_found_this_run": 0, "defects_carried": 0, "open_defects": 0,  # the conflict: 0 here, 1 open DEF- record
           "appendix_index_rows": 74},
    defects=[_defect(trace_url="../../../../../../../../AppData/Local/Temp/pw/x/trace.zip",
                     video_url="../../../../../../../../AppData/Local/Temp/pw/x/video.webm",
                     screenshot_url="../evidence/x/01.png")],
    taxonomy={"defect": 1, "unverified": 0, "limitation": 0, "test_defect": 0, "tooling": 0},
    delta={"baseline": True, "previous_run_id": "run-0", "previous_finished_at": "2026-10-07T07:10:45+00:00",
          "previous_pass_rate_percent": 97, "previous_tests_ran": 80, "renamed_count": 0,
          "removed": [{"id": f"r{i}", "title": "x", "reason": "x"} for i in range(15)],
          "added": [{"id": f"a{i}", "title": "x"} for i in range(6)], "quarantined_delta": 0,
          "quarantined_now": 0, "category_changes": [], "fixed": [], "still_failing": [], "new_failures": []},
    requirements={"total": 10, "not_backed": 2, "percent": 20, "rows": []},
)

# run-20261008120305: the SAME defect is simultaneously called resolved (category-changes), open and High
# severity (Defects table), and release-blocking (NO-GO) - with no recorded reason for a Medium->High jump,
# and a run ID that encodes 07:10:45 cited next to a printed 08:05:08.
bad_120305 = _base(
    generated_at="2026-10-08T12:03:05+00:00", run_id="run-20261008120305", recommendation="NO-GO",
    run_started_at="2026-10-08T11:57:38+00:00", run_finished_at="2026-10-08T12:03:05+00:00",
    counts={"executed_tests": 70, "passed": 70, "failed": 0, "skipped": 3, "blocked_flows": 8,
           "test_defects": 0, "quarantined": 1, "quarantined_passed": 1, "quarantined_failed": 0,
           "page_tests": 44, "flow_tests": 26, "journeys_total": 35,
           "journeys_passed": 26, "journeys_failed": 1, "journeys_with_result": 27,
           "journeys_test_defect": 0, "journeys_skipped": 0, "journeys_failed_last_check": 0,
           "journeys_no_result": 8, "journeys_not_run": 8, "journeys_passed_percent": 74,
           "flows_with_result": 26, "pass_rate_percent": 100, "all_items": 81,
           "defects_found_this_run": 1, "defects_carried": 0, "open_defects": 1,  # also wrong: it did not reproduce
           "appendix_index_rows": 74},
    defects=[_defect(severity="High", severity_set_at="", severity_reason="",   # escalated with no recorded cause
                     _cited_run_id="run-20261008071045", _cited_run_at="2026-10-08T08:05:08+00:00")],
    taxonomy={"defect": 1, "unverified": 0, "limitation": 0, "test_defect": 0, "tooling": 0},
    delta={"baseline": True, "previous_run_id": "run-20261008071045", "previous_finished_at": "2026-10-08T07:10:45+00:00",
          "previous_pass_rate_percent": 100, "previous_tests_ran": 70, "renamed_count": 0,
          "removed": [], "added": [], "quarantined_delta": 0, "quarantined_now": 1,
          "category_changes": [{"id": "k1", "title": "t", "from": "defect", "to": None,
                                "reason": "resolved (was defect, now passes clean)"}],  # contradicts the open DEF-001 above
          "fixed": [{"id": "k1", "title": "t"}], "still_failing": [], "new_failures": []},
    requirements={"total": 10, "not_backed": 2, "percent": 20, "rows": []},
)

(HERE / "golden_run_20261008071045_bad.json").write_text(json.dumps(bad_071045, indent=2), encoding="utf-8")
(HERE / "golden_run_20261008120305_bad.json").write_text(json.dumps(bad_120305, indent=2), encoding="utf-8")
print("wrote fixtures")
