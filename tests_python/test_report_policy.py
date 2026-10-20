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


# Real errors from a production site that the report called application defects (sat.aljazeera.net, 2026-10-20).
SKIP_LINK = ("Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for get_by_role(\"link\", name=\"Skip to main content\")\n"
             "  - <div class=\"header\">...</div> intercepts pointer events\n  - retrying click action")
DROPDOWN = ("Locator.click: Timeout 30000ms exceeded.\nCall log:\n"
            "  - waiting for locator(\"label[for='ui-multiselect-channellist-option-1']\")\n  - element is not visible")
WRONG_PAGE = "AssertionError: Page URL expected to be 'https://x.test/en/find'\nActual value: https://x.test/en/login"
MISSING = ("AssertionError: Locator expected to be visible\nActual value: <element(s) not found>\nCall log:\n"
           "  - Expect \"to_be_visible\" with timeout 5000ms")
NO_LOAD = "Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to \"https://x.test/\", waiting until \"load\""


def test_a_check_that_could_not_act_on_the_page_needs_triage_not_a_defect():
    assert policy.triage_category("unclear", SKIP_LINK) == "triage"          # a hidden skip link, covered by the header
    assert policy.triage_category("unclear", DROPDOWN) == "triage"           # clicked an option before the menu opened


def test_a_check_that_saw_the_site_do_the_wrong_thing_is_still_a_defect():
    assert policy.triage_category("unclear", WRONG_PAGE) == "defect"          # it observed the wrong address
    assert policy.triage_category("unclear", MISSING) == "defect"             # it observed the element missing
    assert policy.triage_category("unclear", NO_LOAD) == "defect"             # the page itself did not load
    assert policy.triage_category("test_defect", SKIP_LINK) == "test_defect"  # a known cause keeps its own category
    assert policy.triage_category("inconclusive", SKIP_LINK) == "unverified"


def test_a_run_with_no_executed_test_has_no_verdict_whatever_else_it_recorded():
    records = [_rec("UNV-001", "unverified"), _rec("TOOL-001", "tooling", "Low")]
    assert policy.release_decision(records, executed_tests=0) == "NO VERDICT"
    assert policy.release_decision(records, requirements_backed_percent=10, executed_tests=0) == "NO VERDICT"
    assert policy.release_decision(records, executed_tests=3) == "GO"
    assert policy.verdict_trigger(records, executed_tests=0) == "no test ran, so this run has no evidence either way"
    criteria = policy.exit_criteria(records, [], "NO VERDICT")
    assert len(criteria) == 1 and "TOOL-001" in criteria[0] and "run again" in criteria[0]


def test_triage_is_never_p1_and_never_blocks():
    assert policy.priority_for("triage", "High") == "P2 - next fix cycle"
    assert policy.release_decision([_rec("TRI-001", "triage", "High")]) != "NO-GO"


def test_an_open_high_or_critical_defect_is_no_go():
    assert policy.release_decision([_rec("DEF-001", "defect", "High")]) == "NO-GO"
    assert policy.release_decision([_rec("DEF-001", "defect", "Critical"), _rec("UNV-001", "unverified")]) == "NO-GO"


def test_a_medium_defect_is_conditional_go_but_what_the_run_could_not_observe_is_not_held_against_the_site():
    assert policy.release_decision([_rec("DEF-001", "defect", "Medium")]) == "CONDITIONAL GO"
    # a production site whose map page and CAPTCHA form always leave journeys unverified could never reach GO
    assert policy.release_decision([_rec("UNV-001", "unverified", "High"), _rec("UNV-002", "unverified")]) == "GO"
    assert policy.verdict_trigger([_rec("UNV-001", "unverified", "High")]) == ""


def test_only_requirements_people_wrote_can_cap_the_verdict_not_ones_the_model_guessed():
    guessed_only = {"total": 6, "not_backed": 2, "percent": 33, "written_total": 0, "written_not_backed": 0}
    assert policy.requirements_backed_percent(guessed_only) is None
    mixed = {"total": 6, "not_backed": 4, "percent": 67, "written_total": 2, "written_not_backed": 1}
    assert policy.requirements_backed_percent(mixed) == 50
    assert policy.release_decision([], requirements_backed_percent=policy.requirements_backed_percent(mixed)) == "CONDITIONAL GO"
    older = {"total": 4, "not_backed": 1, "percent": 25}                     # data from before the split counts everything
    assert policy.requirements_backed_percent(older) == 75


def test_limitations_tooling_and_test_defects_never_change_the_verdict():
    records = [_rec("LIM-001", "limitation", "Low"), _rec("TST-001", "test_defect", "Low"), _rec("TOOL-001", "tooling", "Low")]
    assert policy.release_decision(records) == "GO"
    assert policy.release_decision([]) == "GO"


def test_low_requirements_coverage_caps_a_would_be_go_at_conditional_go():
    """C4: zero open defects does not mean most written requirements are confirmed working - a would-be GO
    is capped at CONDITIONAL GO below the threshold."""
    assert policy.release_decision([], requirements_backed_percent=40) == "CONDITIONAL GO"
    assert policy.release_decision([], requirements_backed_percent=80) == "GO"
    assert policy.release_decision([], requirements_backed_percent=None) == "GO"   # no written requirements at all


def test_requirements_coverage_never_overrides_a_verdict_a_defect_already_made_worse():
    assert policy.release_decision([_rec("DEF-001", "defect", "High")], requirements_backed_percent=90) == "NO-GO"
    assert policy.release_decision([_rec("DEF-001", "defect", "Medium")], requirements_backed_percent=10) == "CONDITIONAL GO"


def test_verdict_trigger_names_low_requirements_coverage():
    assert policy.verdict_trigger([], requirements_backed_percent=40) == \
        "only 40% of requirements backed by a passing result, below the 60% threshold"
    assert policy.verdict_trigger([]) == ""


def test_a_closed_defect_does_not_block():
    assert policy.release_decision([_rec("DEF-001", "defect", "High", status="Closed")]) == "GO"


def test_the_headline_states_the_defect_count_in_plain_words_even_when_it_is_zero():
    assert policy.defect_headline(0, 0).startswith("0 application defects found this run.")
    assert policy.defect_headline(1, 0) == "1 application defect found this run."


def test_the_headline_distinguishes_found_this_run_from_carried_from_a_previous_run():
    """C3: a not-reproduced ledger item was not FOUND this run - it failed to reproduce and remains open."""
    assert policy.defect_headline(0, 1) == "0 application defects found this run; 1 open defect carried from previous runs (not reproduced this run)."
    assert policy.defect_headline(2, 3) == "2 application defects found this run; 3 open defects carried from previous runs (not reproduced this run)."


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


def test_a_run_where_every_scored_item_has_one_priority_is_flagged_for_a_person_not_refused():
    same = [_rec("UNV-001", "unverified", "Medium"), _rec("UNV-002", "unverified", "Medium")]
    note = policy.priority_uniformity_note(same)
    assert note and "All 2 scored items" in note and "P2" in note and "judged from its own user impact" in note
    assert policy.priority_uniformity_note(same + [_rec("DEF-001", "defect", "High")]) is None      # they vary: nothing to say
    assert policy.priority_uniformity_note([_rec("UNV-001", "unverified")]) is None                 # one item cannot be uniform
    assert policy.priority_uniformity_note([_rec("LIM-001", "limitation"), _rec("LIM-002", "limitation")]) is None   # fixed by rule


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
    assert bullets == ["A person should look at 13 journey(s) the run could not confirm either way (UNV-001 to UNV-013), section 5."]


def test_failed_checks_needing_triage_share_the_look_bullet_with_unverified_journeys():
    records = [_rec("TRI-001", "triage"), _rec("TRI-002", "triage"), _rec("UNV-001", "unverified")]
    assert policy.build_conditions(records, [], "GO") == [
        "A person should look at 2 failed check(s) whose cause is not known yet (TRI-001, TRI-002) and "
        "1 journey(s) the run could not confirm either way (UNV-001), section 5."]


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
    records = [_rec("DEF-001", "defect"), _rec("UNV-001", "unverified"), _rec("TRI-001", "triage"),
               _rec("LIM-001", "limitation"), _rec("TOOL-001", "tooling")]
    criteria = policy.exit_criteria(records, ["COV-001"], "CONDITIONAL GO")
    # only what can hold the verdict back is a hurdle; everything else is "a person should look" in the conditions
    assert criteria == ["Every application defect (DEF-001) is fixed, retested and closed."]
    assert policy.exit_criteria(records[1:], ["COV-001"], "GO") == ["No further condition applies; rerun on the next build to keep this verdict."]


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


# ------------------------------------------------------------------ defect ledger (a defect persists across runs)

def _rec_for(title="t", url="https://x.test/a", scope="page"):
    return {"title": title, "url": url, "scope": scope}


def test_a_fresh_defect_is_added_open_with_one_failure():
    ledger = {"version": 1, "defects": {}}
    ghosts, closed = policy.update_ledger(ledger, {"k1": _rec_for()}, "run-1", "t1")
    assert ghosts == [] and closed == {}   # reproduced this run, so no ghost - already in this run's own defect list
    entry = ledger["defects"]["k1"]
    assert entry["status"] == "open" and entry["total_failures"] == 1 and entry["consecutive_not_reproduced"] == 0
    assert entry["first_seen_run"] == "run-1" and entry["last_failed_run"] == "run-1"


def test_a_defect_not_reproduced_becomes_a_ghost_instead_of_disappearing():
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-1", "t1")
    ghosts, closed = policy.update_ledger(ledger, {}, "run-2", "t2")   # k1 did not fail this run
    assert len(ghosts) == 1 and ghosts[0]["key"] == "k1" and closed == {}
    assert ghosts[0]["consecutive_not_reproduced"] == 1 and ghosts[0]["status"] == "open"
    assert ledger["defects"]["k1"]["total_failures"] == 1   # only incremented on an actual reproduction


def test_a_defect_that_reproduces_again_resets_not_reproduced_and_adds_a_failure():
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-1", "t1")
    policy.update_ledger(ledger, {}, "run-2", "t2")
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-3", "t3")
    entry = ledger["defects"]["k1"]
    assert entry["consecutive_not_reproduced"] == 0 and entry["total_failures"] == 2 and entry["last_failed_run"] == "run-3"


def test_closing_a_defect_removes_it_from_future_ghosts():
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-1", "t1")
    assert policy.close_defect(ledger, "k1", "sam", "fixed in prod", "t2") is True
    assert ledger["defects"]["k1"]["status"] == "closed"
    ghosts, closed = policy.update_ledger(ledger, {}, "run-2", "t2")
    assert ghosts == [] and closed == {}   # closed, not open - never reappears as a ghost
    assert policy.close_defect(ledger, "k1", "sam", "again", "t3") is False   # already closed
    assert policy.close_defect(ledger, "nonexistent", "sam", "x", "t3") is False


def test_a_closed_defect_that_reproduces_again_reopens_with_its_closure_kept_in_history():
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-1", "t1")
    policy.close_defect(ledger, "k1", "sam", "fixed in prod", "t2")
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-3", "t3")   # the same defect fails again
    entry = ledger["defects"]["k1"]
    assert entry["status"] == "open" and entry["closed_by"] is None
    assert entry["closure_history"] == [{"closed_by": "sam", "closed_at": "t2", "closed_reason": "fixed in prod",
                                         "reopened_at": "t3", "reopened_run": "run-3"}]


def test_severity_is_set_once_and_a_later_reproduction_never_recomputes_it():
    """C2: severity reflects user impact and is fixed when the defect is first confirmed - reproducing it
    again is never a reason to change it, even if a fresh computation would come out differently."""
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": {**_rec_for(), "severity": "Medium", "severity_set_at": "t1", "severity_reason": "r1"}}, "run-1", "t1")
    entry = ledger["defects"]["k1"]
    assert entry["severity"] == "Medium" and entry["severity_set_at"] == "t1" and entry["severity_reason"] == "r1"
    # Reproduces again, and this time a (hypothetical) fresh computation would say High - frozen value wins.
    policy.update_ledger(ledger, {"k1": {**_rec_for(), "severity": "High", "severity_set_at": "t3", "severity_reason": "r3"}}, "run-3", "t3")
    entry = ledger["defects"]["k1"]
    assert entry["severity"] == "Medium" and entry["severity_set_at"] == "t1" and entry["severity_reason"] == "r1"


def test_a_ghost_never_recomputes_severity_either():
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": {**_rec_for(), "severity": "High", "severity_set_at": "t1", "severity_reason": "r1"}}, "run-1", "t1")
    ghosts, _ = policy.update_ledger(ledger, {}, "run-2", "t2")   # not reproduced
    assert ghosts[0]["severity"] == "High" and ghosts[0]["severity_set_at"] == "t1" and ghosts[0]["severity_reason"] == "r1"


def test_a_legacy_ledger_entry_with_no_severity_gets_one_recorded_on_migration():
    ledger = {"version": 1, "defects": {"k1": {"title": "t", "url": "https://x.test/a", "scope": "page",
             "first_seen_run": "run-0", "first_seen_at": "t0", "total_failures": 1, "closure_history": [],
             "status": "open", "last_failed_run": "run-0", "last_failed_at": "t0", "consecutive_not_reproduced": 0,
             "closed_by": None, "closed_at": None, "closed_reason": None}}}
    ghosts, _ = policy.update_ledger(ledger, {}, "run-1", "t1")
    assert ghosts[0]["severity"] in {"Critical", "High", "Medium", "Low"}
    assert ghosts[0]["severity_set_at"] == "t1" and "migration" in ghosts[0]["severity_reason"]


def test_a_defect_auto_closes_after_three_consecutive_clean_runs():
    """The other half of 'keeps its blocking status until a person closes it, which requires 3 consecutive
    clean runs or a deliberate close' - without this, that sentence would describe a mechanism that doesn't exist."""
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-1", "t1")
    g1, c1 = policy.update_ledger(ledger, {}, "run-2", "t2")
    assert len(g1) == 1 and c1 == {} and ledger["defects"]["k1"]["status"] == "open"
    g2, c2 = policy.update_ledger(ledger, {}, "run-3", "t3")
    assert len(g2) == 1 and c2 == {} and ledger["defects"]["k1"]["status"] == "open"
    g3, c3 = policy.update_ledger(ledger, {}, "run-4", "t4")
    assert g3 == []   # the 3rd consecutive clean run closed it - no longer rendered as an open ghost
    entry = ledger["defects"]["k1"]
    assert entry["status"] == "closed" and entry["closed_by"] == "system (auto)"
    # P1.2: the audit trail for THIS close, returned so the caller can render it in the same run it happened.
    assert c3["k1"]["actor"] == "[auto]" and c3["k1"]["clean_run_count"] == 3 and c3["k1"]["event"] == "closed"
    assert entry["audit_log"][-1] == c3["k1"]
    assert policy.update_ledger(ledger, {}, "run-5", "t5") == ([], {})   # stays closed, never reappears


def test_a_not_reproduced_defect_gets_an_honest_evidence_note_not_the_generic_fallback():
    """D4: a ghost's check PASSED this run - "the test ended before its first evidence step" is false for it."""
    note = policy.evidence_reason("defect", "not_reproduced")
    assert "the check passed" in note
    assert "ended before its first evidence step" not in note


# ------------------------------------------------------------------ owner routing by category

def test_by_category_routes_a_defect_tooling_and_limitation_to_different_people():
    cfg = {"default": "Default Person", "by_category": {
        "application_defect": "Dev Owner", "test_harness": "QA Owner", "site_owner_agreement": "Site Owner"}}
    assert policy.resolve_owner(cfg, _rec("DEF-001", "defect")) == "Dev Owner"
    assert policy.resolve_owner(cfg, _rec("TOOL-001", "tooling")) == "QA Owner"
    assert policy.resolve_owner(cfg, _rec("LIM-001", "limitation")) == "Site Owner"


def test_by_category_also_accepts_the_internal_category_name_directly():
    cfg = {"by_category": {"tooling": "Internal Name Owner"}}
    assert policy.resolve_owner(cfg, _rec("TOOL-001", "tooling")) == "Internal Name Owner"


def test_an_unset_category_falls_back_to_default_not_unassigned():
    cfg = {"default": "Default Person", "by_category": {"application_defect": "Dev Owner"}}
    assert policy.resolve_owner(cfg, _rec("UNV-001", "unverified")) == "Default Person"


def test_owner_source_distinguishes_by_category_from_a_true_default_fallback():
    """P6: "same owner for every category" and "fell back to the default owner" are different claims -
    by_category can be fully configured and still resolve every category to one person, which is a real
    organizational fact, not a missing setting. The disclosure must tell these apart."""
    fully_configured = {"default": "D", "by_category": {"application_defect": "D", "test_harness": "D", "site_owner_agreement": "D"}}
    name, source = policy.resolve_owner_with_source(fully_configured, _rec("DEF-001", "defect"))
    assert name == "D" and source == "by_category"   # NOT "default", even though the value is the same
    partially_configured = {"default": "D", "by_category": {"application_defect": "D"}}
    name2, source2 = policy.resolve_owner_with_source(partially_configured, _rec("UNV-001", "unverified"))
    assert name2 == "D" and source2 == "default"   # genuinely fell back - no by_category entry for "unverified"


def test_category_changes_names_the_test_and_the_reason_for_each_move():
    prev = _snap("r1", {"a::t1": {**_t("passed"), "category": "unverified"},
                        "a::t2": {**_t("failed"), "category": "defect"},
                        "a::t3": {**_t("passed"), "category": None}})
    cur = _snap("r2", {"a::t1": {**_t("failed"), "category": "defect"},     # reclassified unverified -> defect
                       "a::t2": {**_t("passed"), "category": None},        # resolved
                       "a::t3": {**_t("failed"), "category": "tooling"}})  # newly classified
    delta = policy.compute_delta(prev, cur)
    changes = {c["id"]: c for c in delta["category_changes"]}
    assert changes["a::t1"]["reason"] == "reclassified from unverified to defect"
    assert changes["a::t2"]["reason"] == "resolved (was defect, now passes clean)"
    assert changes["a::t3"]["reason"] == "newly classified as tooling"


def test_a_defect_closing_this_run_always_produces_a_category_change_row():
    """P2: the category-changes table was absent in a run where DEF-001 left the ledger - exactly the event
    it exists to record. A defect -> closed transition must always produce a row, carrying the audit trail
    (actor, reason, clean-run count), not a generic "resolved" that erases how it actually happened."""
    prev = _snap("r1", {"a::t1": {**_t("failed"), "category": "defect"}})
    cur = _snap("r2", {"a::t1": {**_t("passed"), "category": None}})
    closed_this_run = {"a::t1": {"at": "2026-10-08T12:53:45+00:00", "event": "closed", "actor": "[auto]",
                                 "reason": "not reproduced for 3 consecutive runs", "clean_run_count": 3}}
    delta = policy.compute_delta(prev, cur, closed_this_run=closed_this_run)
    assert len(delta["category_changes"]) == 1
    change = delta["category_changes"][0]
    assert change["id"] == "a::t1"
    assert "[auto]" in change["reason"] and "not reproduced for 3 consecutive runs" in change["reason"]
    assert "clean run count at close: 3" in change["reason"]
    assert not change["reason"].startswith("resolved")


def test_no_category_change_when_category_is_unchanged():
    prev = _snap("r1", {"a::t1": {**_t("passed"), "category": "defect"}})
    cur = _snap("r2", {"a::t1": {**_t("passed"), "category": "defect"}})
    assert policy.compute_delta(prev, cur)["category_changes"] == []


def test_ledger_round_trips_through_disk(tmp_path):
    path = tmp_path / "defect_ledger.json"
    ledger = {"version": 1, "defects": {}}
    policy.update_ledger(ledger, {"k1": _rec_for()}, "run-1", "t1")
    policy.save_ledger(path, ledger)
    reloaded = policy.load_ledger(path)
    assert reloaded["defects"]["k1"]["total_failures"] == 1
    assert policy.load_ledger(tmp_path / "missing.json") == {"version": 1, "defects": {}}
