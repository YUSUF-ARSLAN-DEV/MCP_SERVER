from datetime import date

import pytest

from website_test_pipeline import report_policy as policy
from website_test_pipeline.report_model import canonical_counts, validate_counts
from website_test_pipeline.report_policy import ReportConsistencyError


def _rec(id_, category, severity="Medium", status="Open", **extra):
    rec = {"id": id_, "category": category, "severity": severity, "status": status, "title": f"title {id_}",
           "priority": policy.priority_for(category, severity), "url": f"https://x.test/{id_}", "failure_class": category}
    rec.update(extra)
    return rec


# ------------------------------------------------------------------ taxonomy and the release rule

def test_kinds_map_to_the_three_decision_tables_plus_tooling():
    assert policy.category_for("test_defect") == "test_defect"
    assert policy.category_for("human_input_required") == "limitation"
    assert policy.category_for("authentication_required") == "limitation"
    assert policy.category_for("inconclusive") == "unverified"
    assert policy.category_for("blocked_flow") == "unverified"
    assert policy.category_for("failed_last_check") == "defect"
    assert policy.category_for("unclear") == "defect"


def test_an_open_high_or_critical_defect_is_no_go():
    assert policy.release_decision([_rec("DEF-001", "defect", "High")]) == "NO-GO"
    assert policy.release_decision([_rec("DEF-001", "defect", "Critical"), _rec("UNV-001", "unverified")]) == "NO-GO"


def test_no_defects_but_unverified_journeys_is_conditional_go():
    assert policy.release_decision([_rec("UNV-001", "unverified", "High")]) == "CONDITIONAL GO"
    assert policy.release_decision([_rec("DEF-001", "defect", "Medium")]) == "CONDITIONAL GO"


def test_limitations_tooling_and_test_defects_never_change_the_verdict():
    records = [_rec("LIM-001", "limitation", "Low"), _rec("TST-001", "test_defect", "Low"), _rec("TOOL-001", "tooling", "Low")]
    assert policy.release_decision(records) == "GO"
    assert policy.release_decision([]) == "GO"


def test_a_closed_defect_does_not_block():
    assert policy.release_decision([_rec("DEF-001", "defect", "High", status="Closed")]) == "GO"


def test_the_headline_states_the_defect_count_in_plain_words_even_when_it_is_zero():
    zero = policy.taxonomy_counts([])
    assert policy.defect_headline(zero).startswith("0 application defects found this run.")
    one = policy.taxonomy_counts([_rec("DEF-001", "defect")])
    assert policy.defect_headline(one) == "1 application defect found this run."


# ------------------------------------------------------------------ severity from user impact, and priority

def test_a_core_element_that_does_not_render_is_critical():
    assert policy.impact_severity("defect", scope="page", title="main heading", failure_text="The expected element never appeared on the page.",
                                  assertion_text="expect(page.get_by_role('heading', name='News')).to_be_visible()") == "Critical"


def test_landing_on_the_wrong_address_is_high_and_a_secondary_control_is_medium():
    assert policy.impact_severity("defect", scope="flow", title="Open the country list",
                                  failure_text="E   AssertionError: Page URL expected to be 'x'") == "High"
    assert policy.impact_severity("defect", scope="page", title="Footer social link",
                                  failure_text="The expected element never appeared on the page.",
                                  assertion_text="expect(page.get_by_role('link', name='Share')).to_be_visible()") == "Medium"


def test_an_unverified_journey_takes_the_business_value_of_the_journey_default_medium():
    assert policy.impact_severity("unverified", scope="flow", title="Subscribe to the newsletter plan") == "High"
    assert policy.impact_severity("unverified", scope="flow", title="Change the site language") == "Low"
    assert policy.impact_severity("unverified", scope="flow", title="Open the gallery") == "Medium"


def test_a_limitation_is_always_low_and_never_p1():
    assert policy.impact_severity("limitation", scope="page", title="Checkout needs a CAPTCHA") == "Low"
    assert policy.priority_for("limitation", "High") == "P3 - when convenient"


def test_a_run_where_every_scored_item_has_one_priority_is_rejected():
    same = [_rec("UNV-001", "unverified", "Medium"), _rec("UNV-002", "unverified", "Medium")]
    with pytest.raises(ReportConsistencyError, match="same priority"):
        policy.assert_priorities_vary(same)
    policy.assert_priorities_vary(same + [_rec("DEF-001", "defect", "High")])
    policy.assert_priorities_vary([_rec("UNV-001", "unverified")])            # one item cannot be uniform


# ------------------------------------------------------------------ product vs test

def test_expected_and_observed_resolving_to_the_same_url_is_a_test_defect():
    error = "E   AssertionError: Page URL expected to be 'https://x.test/en/find'\nE   Actual value: https://x.test/en/find/"
    assert "same URL" in policy.triage_test_defect(error)


def test_a_url_regex_that_matches_the_observed_page_is_also_a_test_defect():
    error = "E   AssertionError: Page URL expected to match '/en/find'\nE   Actual value: https://x.test/en/find?x=1"
    assert policy.triage_test_defect(error) is not None


def test_a_real_url_mismatch_is_not_triaged_away():
    error = "E   AssertionError: Page URL expected to be 'https://x.test/en/find'\nE   Actual value: https://x.test/en/login"
    assert policy.triage_test_defect(error) is None


def test_a_stated_expectation_that_differs_from_the_assertion_that_ran_is_a_test_defect():
    reason = policy.triage_test_defect("E   boom", "navigates -> /en/find", ["expect(page).to_have_url(re.compile('/en/subscribe'))"])
    assert reason and "does not match the assertion that ran" in reason
    assert policy.triage_test_defect("E   boom", "navigates -> /en/find", ["expect(page).to_have_url(re.compile('/en/find'))"]) is None


# ------------------------------------------------------------------ conditions: five bullets, ranges, own ID and URL

def test_thirteen_unverified_journeys_collapse_into_one_bullet_with_an_id_range():
    records = [_rec(f"UNV-{n:03d}", "unverified") for n in range(1, 14)]
    bullets = policy.build_conditions(records, [], "CONDITIONAL GO")
    assert bullets == ["13 journeys need a manual pass before sign-off (UNV-001 to UNV-013, section 5)."]


def test_each_limitation_bullet_keeps_its_own_id_and_url_when_it_fits():
    records = [_rec("LIM-001", "limitation", failure_class="human_input_required", mitigation="Ask for a bypass."),
               _rec("LIM-002", "limitation", failure_class="human_input_required", mitigation="Ask for a bypass.")]
    bullets = policy.build_conditions(records, [], "GO")
    assert len(bullets) == 2 and "LIM-001" in bullets[0] and "https://x.test/LIM-001" in bullets[0]
    assert bullets[0] != bullets[1]                                  # identical text can still be told apart


def test_the_conditions_never_exceed_five_bullets():
    records = ([_rec("DEF-001", "defect", "High"), _rec("TST-001", "test_defect")] + [_rec(f"UNV-{n:03d}", "unverified") for n in range(1, 4)]
               + [_rec(f"LIM-{n:03d}", "limitation", failure_class="human_input_required") for n in range(1, 8)])
    assert len(policy.build_conditions(records, ["COV-001", "COV-002"], "NO-GO")) <= 5


def test_exit_criteria_state_what_flips_the_verdict_not_a_copy_of_the_summary():
    records = [_rec("UNV-001", "unverified"), _rec("UNV-002", "unverified"), _rec("UNV-003", "unverified")]
    criteria = policy.exit_criteria(records, [], "CONDITIONAL GO")
    assert criteria == ["Every unverified journey (UNV-001 to UNV-003) is manually signed off, with an owner named on each."]


# ------------------------------------------------------------------ owners and due dates

def test_a_missing_owner_fails_the_report_naming_the_items(monkeypatch):
    monkeypatch.delenv("REPORT_OWNER", raising=False)
    records = [_rec("DEF-001", "defect"), _rec("DEF-002", "defect")]
    with pytest.raises(ReportConsistencyError, match=r"DEF-001, DEF-002"):
        policy.assign_owners(records, {}, date(2026, 10, 4))


def test_a_placeholder_owner_is_not_a_name():
    with pytest.raises(ReportConsistencyError):
        policy.assign_owners([_rec("DEF-001", "defect")], {"default": "Unassigned"}, date(2026, 10, 4))


def test_owner_lookup_prefers_url_then_category_then_default_and_sets_an_iso_due_date():
    records = [_rec("DEF-001", "defect", "High", url="https://x.test/a"), _rec("UNV-001", "unverified"), _rec("LIM-001", "limitation", "Low")]
    cfg = {"default": "Ana", "by_category": {"unverified": "Ben"}, "by_url": {"https://x.test/a": "Cy"}}
    policy.assign_owners(records, cfg, date(2026, 10, 4))
    assert [r["owner"] for r in records] == ["Cy", "Ben", "Ana"]
    assert [r["due"] for r in records] == ["2026-10-07", "2026-10-11", "2026-10-18"]


def test_a_release_date_caps_every_due_date():
    assert policy.due_date("P3 - when convenient", date(2026, 10, 4), {"release_date": "2026-10-09"}) == "2026-10-09"
    with pytest.raises(ReportConsistencyError):
        policy.due_date("P1 - before release", date(2026, 10, 4), {"release_date": "soon"})


# ------------------------------------------------------------------ run-over-run delta

def _snap(run_id, tests, **over):
    snap = policy.snapshot(run_id=run_id, finished_at="t", counts={"pass_rate_percent": 50, "executed_tests": len(tests)}, tests=tests,
                           pages=["https://x.test/a", "https://x.test/b"], tooling_urls=[], untested_files=[],
                           env={"Browser version": "120.0.1", "Playwright": "1.40.0"})
    snap.update(over)
    return snap


def _t(status, url="https://x.test/a", file="a_test.py"):
    return {"title": "t", "url": url, "status": status, "scope": "page", "file": file}


def test_the_delta_lists_added_removed_fixed_still_open_and_new_failures():
    prev = _snap("r1", {"a::t_fixed": _t("failed"), "a::t_still": _t("failed"), "z_test.py::dropped": _t("passed", "https://x.test/zzz", "z_test.py"),
                        "a::t_ok": _t("passed")})
    cur = _snap("r2", {"a::t_fixed": _t("passed"), "a::t_still": _t("failed"), "a::t_ok": _t("failed"), "a::t_new": _t("passed")})
    delta = policy.compute_delta(prev, cur)
    assert [d["id"] for d in delta["fixed"]] == ["a::t_fixed"]
    assert [d["id"] for d in delta["still_failing"]] == ["a::t_still"]
    assert [d["id"] for d in delta["new_failures"]] == ["a::t_ok"]
    assert [d["id"] for d in delta["added"]] == ["a::t_new"]
    assert delta["removed"][0]["id"] == "z_test.py::dropped" and delta["removed"][0]["reason"] == "page no longer discovered"


def test_a_removed_test_is_explained_as_renamed_validation_failure_or_deleted():
    prev = _snap("r1", {"a_test.py::test_open_the_country_menu": _t("passed"), "b_test.py::test_x": _t("passed", "https://x.test/b", "b_test.py"),
                        "c_test.py::test_y": _t("passed", "https://x.test/a", "c_test.py")})
    cur = _snap("r2", {"a_test.py::test_open_the_country_menu_list": _t("passed")}, tooling_urls=["https://x.test/b"])
    delta = policy.compute_delta(prev, cur)
    reasons = {r["id"]: r["reason"] for r in delta["removed"]}
    assert reasons["a_test.py::test_open_the_country_menu"].startswith("renamed to")
    assert "spec failed validation" in reasons["b_test.py::test_x"]
    assert reasons["c_test.py::test_y"].startswith("deleted")
    assert delta["added"] == [] and delta["renamed_count"] == 1       # the rename is not also counted as an addition


def test_with_no_previous_run_the_delta_says_it_is_the_baseline():
    delta = policy.compute_delta(None, _snap("r1", {}))
    assert delta["baseline"] is False and "baseline" in delta["note"]


def test_a_browser_or_playwright_version_change_is_flagged_with_its_direction():
    changes = policy.environment_changes({"Browser version": "120.0.1", "Playwright": "1.40.0"},
                                         {"Browser version": "119.0.0", "Playwright": "1.41.2"})
    assert any("Browser version downgraded" in c for c in changes) and any("Playwright upgraded" in c for c in changes)
    assert policy.environment_changes({"Playwright": "1.40.0"}, {"Playwright": "1.40.0"}) == []


def test_history_keeps_one_entry_per_run_id_and_the_baseline_is_the_previous_run(tmp_path):
    path = tmp_path / "run-history.json"
    policy.save_history(path, [], _snap("r1", {}))
    policy.save_history(path, policy.load_history(path), _snap("r2", {}))
    policy.save_history(path, policy.load_history(path), _snap("r2", {"a::t": _t("passed")}))     # re-render of the same run
    history = policy.load_history(path)
    assert [r["run_id"] for r in history] == ["r1", "r2"]
    assert policy.previous_run(history, "r2")["run_id"] == "r1"


# ------------------------------------------------------------------ IDs and counts

def test_an_id_cited_without_a_row_in_section_5_or_6_is_rejected():
    policy.check_ids({"Executive Summary": {"UNV-001"}}, {"UNV‑001"})
    with pytest.raises(ReportConsistencyError, match="COV-001"):
        policy.check_ids({"Executive Summary": {"COV-001"}}, {"UNV-001"})


def test_id_range_collapses_a_contiguous_run_only():
    assert policy.id_range(["UNV-001", "UNV-002", "UNV-003"]) == "UNV-001 to UNV-003"
    assert policy.id_range(["UNV-001", "UNV-003"]) == "UNV-001, UNV-003"
    assert policy.id_range(["UNV-001"]) == "UNV-001"


def _counts(**over):
    counts = {"executed_tests": 74, "passed": 70, "failed": 4, "skipped": 2, "blocked_flows": 0, "all_items": 76,
              "page_tests": 50, "flow_tests": 24}
    counts.update(over)
    return counts


def test_page_plus_flow_tests_must_equal_the_tests_that_ran():
    validate_counts(_counts())
    with pytest.raises(ValueError, match="page_tests"):
        validate_counts(_counts(page_tests=52))                       # the 52 + 24 = 76 vs 74 mismatch from a real report


def test_passed_plus_failed_must_equal_tests_ran():
    with pytest.raises(ValueError, match="passed \\+ failed"):
        validate_counts(_counts(passed=71))


def test_journeys_passed_failed_and_no_result_must_add_up_to_the_total():
    validate_counts(_counts(journeys_total=3, journeys_passed=2, journeys_no_result=1, flows_with_result=24, journeys_skipped=0,
                            journeys_test_defect=0))
    with pytest.raises(ValueError, match="journeys"):
        validate_counts(_counts(journeys_total=4, journeys_passed=2, journeys_no_result=1, flows_with_result=24))


class _O:
    def __init__(self, status, invalid=None):
        self.status, self.invalid_reason = status, invalid


class _R:
    def __init__(self, outcomes):
        self.outcomes = outcomes


class _F:
    def __init__(self, outcome):
        self.outcome = outcome
        self.verify_failed = False

    tested = property(lambda s: s.outcome is not None)
    passed = property(lambda s: bool(s.outcome and s.outcome.status == "passed"))
    failed = property(lambda s: bool(s.outcome and s.outcome.status in {"failed", "error"}))


class _Run:
    url_reports = [_R([_O("passed"), _O("failed"), _O("failed", "wrong test"), _O("skipped")])]
    flow_reports = [_F(_O("passed")), _F(_O("failed")), _F(None)]
    tested_flows = flow_reports[:2]
    untested_flows = flow_reports[2:]


def test_a_failure_caused_by_the_test_itself_is_left_out_of_the_failure_count():
    counts = canonical_counts(_Run())
    assert counts["failed"] == 2 and counts["test_defects"] == 1 and counts["skipped"] == 1
    assert counts["page_tests"] == 2 and counts["flow_tests"] == 2 and counts["executed_tests"] == 4
    assert counts["passed"] + counts["failed"] == counts["executed_tests"]
