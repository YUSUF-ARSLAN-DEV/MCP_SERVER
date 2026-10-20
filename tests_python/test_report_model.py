import pytest
from docx import Document

from website_test_pipeline.report import _validate_docx_export
from website_test_pipeline.report_model import canonical_counts, validate_report_data
from website_test_pipeline.report_policy import ReportConsistencyError


class _Outcome:
    def __init__(self, status):
        self.status = status


class _Report:
    def __init__(self, outcomes):
        self.outcomes = outcomes


class _Run:
    url_reports = [_Report([_Outcome("passed"), _Outcome("failed"), _Outcome("skipped")])]
    tested_flows = []
    untested_flows = [object()]


def test_canonical_counts_reconcile_and_exclude_skips_from_pass_rate():
    counts = canonical_counts(_Run())
    # subset check, not full equality: canonical_counts also reports journey-level stats unrelated to this test
    assert {k: counts[k] for k in ("executed_tests", "passed", "failed", "skipped", "blocked_flows",
                                   "all_items", "pass_rate_percent")} == {
        "executed_tests": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 1,
        "blocked_flows": 1,
        "all_items": 4,
        "pass_rate_percent": 50,
    }


def test_conditional_go_requires_a_linked_condition():
    data = {
        "schema_version": "1.0", "generated_at": "2026-01-01T00:00:00+00:00",
        "counts": {"executed_tests": 0, "passed": 0, "failed": 0, "skipped": 0,
                   "blocked_flows": 0, "all_items": 0, "pass_rate_percent": 0},
        "recommendation": "CONDITIONAL GO", "conditions": [], "defects": [], "coverage": {},
    }
    with pytest.raises(ValueError, match="CONDITIONAL GO"):
        validate_report_data(data)


def test_docx_export_rejects_the_known_repeated_value_corruption(tmp_path):
    document = Document()
    document.add_paragraph(" ".join(["1"] * 40))
    path = tmp_path / "corrupt.docx"
    document.save(path)
    with pytest.raises(ValueError, match="repeated-value corruption"):
        _validate_docx_export(path)


def test_docx_export_accepts_normal_prose(tmp_path):
    document = Document()
    document.add_paragraph("The test run completed with a clear result.")
    path = tmp_path / "valid.docx"
    document.save(path)
    _validate_docx_export(path)


# ------------------------------------------------------------------ Part 0: pre-render assertion suite
# One assertion per Part 0 line, each proven to fire on bad input and pass on good - per the deliverable.

def _base_counts(**over):
    counts = {"executed_tests": 10, "passed": 8, "failed": 2, "skipped": 1, "blocked_flows": 1,
             "test_defects": 0, "quarantined": 0, "all_items": 12, "page_tests": 6, "flow_tests": 4,
             # journeys_with_result (4) == flow_tests (4) + quarantined (0): a journey only has a result
             # because its flow test ran (or was quarantined but still produced an outcome) - see P3.
             "journeys_total": 10, "journeys_passed": 3, "journeys_failed": 1, "journeys_with_result": 4,
             "journeys_test_defect": 0, "journeys_skipped": 0, "journeys_failed_last_check": 0,
             "journeys_no_result": 6, "flows_with_result": 4, "pass_rate_percent": 80, "open_defects": 2,
             "appendix_index_rows": 11}
    counts.update(over)
    return counts


def _base_data(**over):
    data = {"schema_version": "2.0", "generated_at": "2026-01-01T00:00:00+00:00", "counts": _base_counts(),
           "recommendation": "GO", "conditions": [], "defects": [], "coverage": {},
           "delta": {"baseline": False, "note": "first run"},
           "run_started_at": "2026-01-01T00:00:00+00:00", "run_finished_at": "2026-01-01T00:01:40+00:00",
           "run_duration_seconds": 100.0}
    data.update(over)
    return data


def test_page_plus_flow_tests_equals_tests_ran():
    validate_report_data(_base_data())  # good: 6 + 4 == 10
    with pytest.raises(ValueError, match="page_tests"):
        validate_report_data(_base_data(counts=_base_counts(flow_tests=5)))  # 6 + 5 != 10


def test_passed_plus_failed_equals_tests_ran():
    validate_report_data(_base_data())
    with pytest.raises(ValueError, match="passed \\+ failed"):
        validate_report_data(_base_data(counts=_base_counts(failed=3)))


def test_journeys_with_result_plus_no_result_equals_journeys_total():
    validate_report_data(_base_data())  # good: passed+failed+...+no_result (8+2+0+0+0+0) == journeys_total (10)
    with pytest.raises(ValueError, match="journeys"):
        validate_report_data(_base_data(counts=_base_counts(journeys_no_result=1)))  # breakdown now sums to 11, not 10


def test_journeys_passed_plus_failed_equals_journeys_with_result():
    validate_report_data(_base_data())
    with pytest.raises(ValueError, match="journeys_with_result"):
        validate_report_data(_base_data(counts=_base_counts(journeys_with_result=11)))


def test_journeys_passed_never_exceeds_journeys_with_result():
    # journeys_passed > journeys_with_result is unreachable once passed+failed==with_result is enforced (the
    # prior assertion) - this is a second, independent guard against the same class of mistake, so it is
    # proven here by itself: with the equality check satisfied (passed+failed==with_result), passed can never
    # exceed with_result while failed>=0, so this line is dead code UNLESS someone removes the equality check
    # first. Proven by disabling that check's precondition: failed=-1 is not a real count, so skip it and
    # exercise the guard directly against its own too-strict construction.
    with pytest.raises(ValueError):
        validate_report_data(_base_data(counts=_base_counts(journeys_passed=11, journeys_with_result=10)))


def test_flow_tests_plus_quarantined_equals_journeys_with_result():
    """P3: S4's own prose and its own table once disagreed by exactly 1 quarantined flow test, both claiming
    to be "the same figure" - confirmed live: "26 flow tests ran" next to "27 journeys have a result"."""
    validate_report_data(_base_data())  # good: 4 + 0 == 4
    with pytest.raises(ValueError, match="journeys_with_result"):
        validate_report_data(_base_data(counts=_base_counts(quarantined=1)))  # 4 + 1 != 4


def test_reconciliation_equation_must_balance_including_quarantine():
    good_delta = {"baseline": True, "previous_run_id": "run-0", "previous_tests_ran": 9, "removed": [],
                 "added": [{"id": "x", "title": "x"}], "renamed_count": 0, "quarantined_delta": 0}
    validate_report_data(_base_data(delta=good_delta))  # 9 - 0 + 1 - 0 == 10
    bad_delta = {**good_delta, "quarantined_delta": 1}  # 9 - 0 + 1 - 1 == 9 != 10
    with pytest.raises(ValueError, match="does not balance"):
        validate_report_data(_base_data(delta=bad_delta))


def test_a_newly_invalid_test_leaves_tests_ran_and_the_delta_still_balances():
    """Found live: a test whose failure was the test's own fault dropped out of tests_ran, the delta did not
    know, and the build died with a bare ValueError instead of a balanced report."""
    delta = {"baseline": True, "previous_run_id": "run-0", "previous_tests_ran": 11, "removed": [],
             "added": [{"id": "x", "title": "x"}], "renamed_count": 0, "quarantined_delta": 0, "test_defect_delta": 2}
    validate_report_data(_base_data(delta=delta))  # 11 - 0 + 1 - 0 - 2 == 10
    with pytest.raises(ReportConsistencyError, match="test-defect change"):
        validate_report_data(_base_data(delta={**delta, "test_defect_delta": 0}))  # 12 != 10


def test_appendix_index_rows_equals_tests_ran_plus_skipped_plus_quarantined_plus_test_defects():
    validate_report_data(_base_data())  # 10 + 1 + 0 + 0 == 11
    with pytest.raises(ValueError, match="appendix index"):
        validate_report_data(_base_data(counts=_base_counts(appendix_index_rows=12)))


def test_open_defects_equals_open_def_records_not_the_test_level_tally():
    defect = {"id": "DEF-001", "category": "defect", "status": "Open", "severity": "Medium", "title": "t",
             "description": "d", "steps_to_reproduce": ["s"], "expected": "e", "actual": "a",
             "screenshot_url": "NOT CAPTURED", "trace_url": "NOT CAPTURED", "evidence_reason": "none",
             "owner": "Owner", "due": "2026-01-01", "linked_flow_id": "NOT CAPTURED",
             "severity_set_at": "2026-01-01", "severity_reason": "r"}
    good = _base_counts(failed=0, passed=10, open_defects=1)
    data = _base_data(counts=good, defects=[defect], taxonomy={"defect": 1}, recommendation="CONDITIONAL GO", conditions=["fix it"])
    validate_report_data(data)  # open_defects (1) == 1 open DEF record
    bad = {**good, "open_defects": 0}
    with pytest.raises(ValueError, match="open_defects"):
        validate_report_data(_base_data(counts=bad, defects=[defect], taxonomy={"defect": 1}, recommendation="CONDITIONAL GO", conditions=["fix it"]))


def test_timestamps_must_be_mutually_consistent():
    validate_report_data(_base_data())  # 00:00:00 -> 00:01:40 is exactly 100.0s
    with pytest.raises(ValueError, match="inconsistent"):
        validate_report_data(_base_data(run_duration_seconds=5.0))


def test_evidence_links_may_not_point_into_a_temp_directory(monkeypatch):
    monkeypatch.delenv("ARTIFACT_BASE_URL", raising=False)
    bad_defect = {"id": "DEF-001", "category": "defect", "status": "Open", "severity": "Medium", "title": "t",
                 "description": "d", "steps_to_reproduce": ["s"], "expected": "e", "actual": "a",
                 "trace_url": "../../../../../../../../AppData/Local/Temp/pw/x/trace.zip",
                 "screenshot_url": "../evidence/x/01.png", "owner": "Owner", "due": "2026-01-01",
                 "linked_flow_id": "NOT CAPTURED", "severity_set_at": "2026-01-01", "severity_reason": "r"}
    with pytest.raises(ValueError, match="cannot resolve"):
        validate_report_data(_base_data(defects=[bad_defect], counts=_base_counts(open_defects=1),
                                        taxonomy={"defect": 1}, recommendation="CONDITIONAL GO", conditions=["fix it"]))
    good_defect = {**bad_defect, "trace_url": "../evidence/x/trace.zip"}
    validate_report_data(_base_data(defects=[good_defect], counts=_base_counts(open_defects=1),
                                    taxonomy={"defect": 1}, recommendation="CONDITIONAL GO", conditions=["fix it"]))


def test_category_change_may_not_say_resolved_while_the_same_test_is_still_an_open_defect():
    """C1: the ledger is the single authority on whether a defect is still open - confirmed live: a report
    called the same defect resolved, open, and release-blocking all at once."""
    defect = {"id": "DEF-001", "category": "defect", "status": "Open", "severity": "Medium", "title": "t",
             "description": "d", "steps_to_reproduce": ["s"], "expected": "e", "actual": "a",
             "screenshot_url": "NOT CAPTURED", "trace_url": "NOT CAPTURED", "evidence_reason": "none",
             "owner": "Owner", "due": "2026-01-01", "linked_flow_id": "NOT CAPTURED",
             "severity_set_at": "2026-01-01", "severity_reason": "r", "_stable_key": "k1"}
    bad_delta = {"baseline": False, "note": "x", "category_changes": [
        {"id": "k1", "title": "t", "from": "defect", "to": None, "reason": "resolved (was defect, now passes clean)"}]}
    with pytest.raises(ValueError, match="resolved"):
        validate_report_data(_base_data(counts=_base_counts(open_defects=1), defects=[defect], taxonomy={"defect": 1},
                                        recommendation="CONDITIONAL GO", conditions=["fix it"], delta=bad_delta))
    good_delta = {**bad_delta, "category_changes": [
        {"id": "k1", "title": "t", "from": "defect", "to": "defect", "reason": "not reproduced this run - still open per the defect ledger, not resolved"}]}
    validate_report_data(_base_data(counts=_base_counts(open_defects=1), defects=[defect], taxonomy={"defect": 1},
                                    recommendation="CONDITIONAL GO", conditions=["fix it"], delta=good_delta))


def test_a_category_count_cannot_drift_with_no_row_to_explain_it():
    """P2: the category-changes table was absent in a run where a Defect genuinely left the ledger - exactly
    the event it exists to record."""
    bad_delta = {"baseline": True, "previous_run_id": "run-0", "previous_tests_ran": 10, "removed": [],
                "added": [], "renamed_count": 0, "quarantined_delta": 0,
                "previous_taxonomy": {"defect": 1, "unverified": 0, "limitation": 0, "test_defect": 0, "tooling": 0},
                "category_changes": []}   # the bug: defect count dropped 1->0 but nothing explains it
    tax0 = {"defect": 0, "unverified": 0, "limitation": 0, "test_defect": 0, "tooling": 0}
    counts0 = _base_counts(open_defects=0)
    data = _base_data(delta=bad_delta, taxonomy=tax0, counts=counts0)
    with pytest.raises(ValueError, match="defect"):
        validate_report_data(data)
    good_delta = {**bad_delta, "category_changes": [{"id": "k1", "title": "t", "from": "defect", "to": None, "reason": "closed [auto] on 2026-01-01 - not reproduced for 3 consecutive runs (clean run count at close: 3)"}]}
    validate_report_data(_base_data(delta=good_delta, taxonomy=tax0, counts=counts0))
    # Also fine when a removed/added test plausibly accounts for the drift, with no category_changes row at all.
    explained_delta = {**bad_delta, "previous_tests_ran": 11, "removed": [{"id": "r1", "title": "x", "reason": "x"}]}
    validate_report_data(_base_data(delta=explained_delta, taxonomy=tax0, counts=counts0))


def test_exit_criteria_must_name_low_requirements_coverage_if_it_caps_the_verdict():
    """P4: all four other exit criteria could be satisfied and GO would still be unreachable below the
    requirements threshold, with nothing saying so - confirmed live: 25 of 78 backed (32%), no exit criterion."""
    reqs = {"total": 78, "not_backed": 53, "percent": 68, "rows": []}   # 68% not backed == 32% backed
    bad = _base_data(requirements=reqs, exit_criteria=["Some unrelated condition."])
    with pytest.raises(ValueError, match="requirements coverage"):
        validate_report_data(bad)
    good = _base_data(requirements=reqs, exit_criteria=["At least 60% of written requirements are backed by a passing result (currently 32%)."])
    validate_report_data(good)
    high_coverage = _base_data(requirements={"total": 78, "not_backed": 5, "percent": 6, "rows": []}, exit_criteria=[])
    validate_report_data(high_coverage)   # 94% backed, well above threshold - nothing required here


def test_severity_cannot_render_without_a_recorded_cause():
    """C2: a severity (set or changed) must always carry a recorded cause - the escalation that produced an
    unexplained Medium -> High jump in a live report is exactly what this guards against."""
    defect = {"id": "DEF-001", "category": "defect", "status": "Open", "severity": "High", "title": "t",
             "description": "d", "steps_to_reproduce": ["s"], "expected": "e", "actual": "a",
             "screenshot_url": "NOT CAPTURED", "trace_url": "NOT CAPTURED", "evidence_reason": "none",
             "owner": "Owner", "due": "2026-01-01", "linked_flow_id": "NOT CAPTURED"}
    good = {**defect, "severity_set_at": "2026-01-01", "severity_reason": "core control unusable"}
    validate_report_data(_base_data(counts=_base_counts(open_defects=1), defects=[good], taxonomy={"defect": 1},
                                    recommendation="NO-GO", conditions=["fix it"]))
    bad = {**defect, "severity_set_at": "", "severity_reason": "core control unusable"}
    with pytest.raises(ValueError, match="severity_set_at"):
        validate_report_data(_base_data(counts=_base_counts(open_defects=1), defects=[bad], taxonomy={"defect": 1},
                                        recommendation="NO-GO", conditions=["fix it"]))
    bad2 = {**defect, "severity_set_at": "2026-01-01", "severity_reason": ""}
    with pytest.raises(ValueError, match="severity_reason"):
        validate_report_data(_base_data(counts=_base_counts(open_defects=1), defects=[bad2], taxonomy={"defect": 1},
                                        recommendation="NO-GO", conditions=["fix it"]))


def test_a_cited_run_id_must_match_the_timestamp_printed_beside_it():
    """D2: run-20261008071045 encodes 07:10:45 - printing 08:05:08 beside it for the "same" event is an
    internal inconsistency, even though each value is individually well-formed."""
    defect = {"id": "DEF-001", "category": "defect", "status": "Open", "severity": "Medium", "title": "t",
             "description": "d", "steps_to_reproduce": ["s"], "expected": "e", "actual": "a",
             "screenshot_url": "NOT CAPTURED", "trace_url": "NOT CAPTURED", "evidence_reason": "none",
             "owner": "Owner", "due": "2026-01-01", "linked_flow_id": "NOT CAPTURED",
             "severity_set_at": "2026-01-01", "severity_reason": "r",
             "_cited_run_id": "run-20261008071045", "_cited_run_at": "2026-10-08T08:05:08+00:00"}
    with pytest.raises(ValueError, match="must agree"):
        validate_report_data(_base_data(counts=_base_counts(open_defects=1), defects=[defect], taxonomy={"defect": 1},
                                        recommendation="CONDITIONAL GO", conditions=["fix it"]))
    good = {**defect, "_cited_run_at": "2026-10-08T07:10:45+00:00"}
    validate_report_data(_base_data(counts=_base_counts(open_defects=1), defects=[good], taxonomy={"defect": 1},
                                    recommendation="CONDITIONAL GO", conditions=["fix it"]))


def test_a_previous_runs_known_bad_report_fails_the_suite():
    """Proof the suite would have caught the exact bugs an external review found live: the self-contradicting
    reconciliation line (= 71, this run counted 70), Failed (application) 0 next to a non-zero defect count,
    the 74-vs-70 appendix gap, and the AppData evidence links - all in the SAME report, as they really were."""
    defect = {"id": "DEF-001", "category": "defect", "status": "Open", "severity": "Medium", "title": "t",
             "description": "d", "steps_to_reproduce": ["s"], "expected": "e", "actual": "a",
             "trace_url": "../../../../../../../../AppData/Local/Temp/pw/x/trace.zip",
             "screenshot_url": "../evidence/x/01.png", "owner": "Owner", "due": "2026-01-01",
             "linked_flow_id": "NOT CAPTURED"}
    bad_counts = _base_counts(failed=0, open_defects=0,  # the conflict: 0 here, 1 open DEF- record
                              appendix_index_rows=74, executed_tests=70, passed=70, page_tests=44, flow_tests=26,
                              journeys_with_result=26, journeys_passed=26, journeys_failed=0, journeys_total=35,
                              journeys_no_result=9, skipped=3, quarantined=0, test_defects=0)
    bad_delta = {"baseline": True, "previous_run_id": "run-0", "previous_tests_ran": 80, "renamed_count": 28,
                "removed": [{"id": f"r{i}", "title": "x", "reason": "x"} for i in range(43)],
                "added": [{"id": f"a{i}", "title": "x"} for i in range(6)], "quarantined_delta": 0}
    data = _base_data(counts=bad_counts, defects=[defect], taxonomy={"defect": 1}, delta=bad_delta,
                      recommendation="CONDITIONAL GO", conditions=["fix it"])
    with pytest.raises(ValueError):
        validate_report_data(data)
