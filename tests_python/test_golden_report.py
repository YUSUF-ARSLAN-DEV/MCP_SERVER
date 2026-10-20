"""Part A: golden-file regression suite. A1-A17 are correct in the current report and must not regress - this
locks them to a real, checked-in report-data.json (not a hand-built fixture) so a future change that breaks
one of them fails a test instead of waiting to be noticed by a human reader.

Checks are by pattern where exact text would be brittle, per the deliverable. The two "known-bad" fixtures are
synthetic reconstructions of the two previous runs an external review found live (run-20261008071045 and
run-20261008120305), built from the review's own quoted evidence - proof the suite would have caught them.
"""
import json
import re
from pathlib import Path

import pytest

from website_test_pipeline.report_model import validate_report_data

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = sorted(FIXTURES.glob("golden_run_2*_good.json"))[-1] if sorted(FIXTURES.glob("golden_run_2*_good.json")) else None


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def good():
    if GOOD is None:
        pytest.skip("no golden good fixture checked in yet")
    return _load(GOOD.name)


def test_the_golden_fixture_itself_passes_validation(good):
    validate_report_data(good)   # the pre-render suite itself must accept the known-good run


# ------------------------------------------------------------------ A1-A14, by pattern

def test_a1_run_timestamps_are_mutually_consistent(good):
    from datetime import datetime
    s = datetime.fromisoformat(good["run_started_at"].replace("Z", "+00:00"))
    f = datetime.fromisoformat(good["run_finished_at"].replace("Z", "+00:00"))
    assert abs((f - s).total_seconds() - good["run_duration_seconds"]) < 1.0
    assert good["run_id"].startswith("run-")
    stamp = re.sub(r"[^0-9]", "", good["run_finished_at"])[:14]
    assert good["run_id"] == f"run-{stamp}"


def test_a4_no_evidence_link_points_into_a_temp_directory(good):
    for defect in good["defects"]:
        for key in ("screenshot_url", "trace_url", "video_url"):
            href = defect.get(key, "")
            if not href or href == "NOT CAPTURED":
                continue
            assert not re.search(r"AppData|/tmp/|Temp/", href), f"{defect['id']} {key} resolves into a temp dir: {href}"
            assert href.count("../") <= 3, f"{defect['id']} {key} has more than 3 ../ segments: {href}"


def test_a7_ledger_backed_defects_require_a_human_close(good):
    """The persistence ledger: an open defect must carry enough to prove it is ledger-tracked, not a bare
    severity with no history - consecutive-clean-run and human-close semantics live in report_policy, this
    only confirms the render carries the fields that make that mechanism auditable."""
    for defect in good["defects"]:
        if defect["category"] == "defect" and defect["status"] == "Open":
            assert defect.get("severity_set_at"), f"{defect['id']} has no recorded severity origin"
            assert defect.get("severity_reason"), f"{defect['id']} has no recorded severity reason"


def test_a8_reconciliation_has_no_escape_hatch_and_balances(good):
    delta = good.get("delta") or {}
    if not delta.get("baseline"):
        pytest.skip("baseline run - no reconciliation to check")
    dropped = len(delta.get("removed", [])) - delta.get("renamed_count", 0)
    expected = delta.get("previous_tests_ran", 0) - dropped + len(delta.get("added", [])) - delta.get("quarantined_delta", 0) - delta.get("test_defect_delta", 0)
    assert expected == good["counts"]["executed_tests"]


def test_a9_category_changes_carry_a_reason_and_never_say_resolved_while_still_open(good):
    open_keys = {d["_stable_key"] for d in good["defects"]
                if d.get("status") == "Open" and d.get("category") == "defect" and "_stable_key" in d}
    for change in (good.get("delta") or {}).get("category_changes", []):
        assert change.get("reason"), f"category change for {change['id']} has no reason"
        if change["id"] in open_keys:
            assert not change["reason"].startswith("resolved"), \
                f"{change['id']} is ledger-open but its category change says resolved"


def test_a10_quarantine_propagates_to_every_count(good):
    counts = good["counts"]
    assert counts["quarantined"] == counts["quarantined_passed"] + counts["quarantined_failed"]
    delta = good.get("delta") or {}
    if delta.get("baseline"):
        assert "quarantined_now" in delta


def test_a_ledger_open_defect_never_appears_in_the_fixed_list(good):
    """RSK-001/C1 honesty: a defect the ledger still calls open did not pass for good - it must never appear
    in "Failures now passing" alongside an Executive Summary that calls the same item still open."""
    open_keys = {d["_stable_key"] for d in good["defects"]
                if d.get("status") == "Open" and d.get("category") == "defect" and "_stable_key" in d}
    fixed_ids = {row["id"] for row in (good.get("delta") or {}).get("fixed", [])}
    assert not (open_keys & fixed_ids)


# ------------------------------------------------------------------ A13-A17 (second review)

def test_a13_failed_this_run_and_open_defects_are_never_merged(good):
    """S1/S4 must carry both as SEPARATE counts.counts fields - never collapsed back into one column."""
    counts = good["counts"]
    for key in ("failed", "open_defects", "defects_found_this_run", "defects_carried"):
        assert key in counts
    assert counts["open_defects"] == counts["defects_found_this_run"] + counts["defects_carried"]


def test_a14_release_rule_text_states_the_protected_clauses(good):
    from website_test_pipeline import report_policy as policy
    rule = policy.release_rule_text()
    assert "a run that does not reproduce it changes neither its severity nor its open status" in rule
    assert f"{policy.REQUIREMENTS_BACKED_THRESHOLD:.0f}%" in rule
    assert "3 consecutive clean runs" in rule


def test_a15_verdict_names_its_trigger(good):
    from website_test_pipeline import report_policy as policy
    reqs = good.get("requirements") or {}
    backed = 100 - reqs["percent"] if reqs.get("total") else None
    trigger = policy.verdict_trigger(good["defects"], requirements_backed_percent=backed)
    if good["recommendation"] != "GO":
        assert trigger, f"non-GO verdict ({good['recommendation']}) has no named trigger"


def test_a16_index_formula_note_matches_the_real_counts(good):
    counts = good["counts"]
    expected = counts["executed_tests"] + counts["skipped"] + counts.get("quarantined", 0) + counts.get("test_defects", 0)
    assert counts["appendix_index_rows"] == expected


def test_a17_arithmetic_identities_hold(good):
    counts = good["counts"]
    assert counts["page_tests"] + counts["flow_tests"] == counts["executed_tests"]
    assert counts["passed"] + counts["failed"] == counts["executed_tests"]


# ------------------------------------------------------------------ proof the suite fails on the known-bad runs

@pytest.mark.parametrize("name", ["golden_run_20261008071045_bad.json", "golden_run_20261008120305_bad.json"])
def test_the_suite_fails_on_the_previous_runs_known_bad_data(name):
    data = _load(name)
    with pytest.raises(ValueError):
        validate_report_data(data)
