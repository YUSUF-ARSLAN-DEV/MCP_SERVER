from pathlib import Path
from types import SimpleNamespace

from website_test_pipeline.findings import (
    Finding, classify_failure, collect_findings, detect_flapping, direction_mismatch_finding,
    environment_block, mojibake_finding, recorded_roles, role_mismatch_finding, url_after_non_navigating_step,
)

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ heuristic 1: role mismatch

INV = [{"url": "https://x.test/en", "controls": [{"name": "Passcode (default)", "tag": "fieldset", "role": None}],
       "revealed": [{"trigger": "Next", "controls": [{"name": "Al Jazeera 2", "role": "checkbox"}]}]}]


def test_recorded_roles_looks_across_controls_and_revealed_and_flow_steps():
    assert recorded_roles("Passcode (default)", INV) == {None}
    assert recorded_roles("Al Jazeera 2", INV) == {"checkbox"}
    assert recorded_roles("never seen anywhere", INV) == set()
    flow = {"steps": [{"name": "Subscribe", "role": "link"}]}
    assert recorded_roles("Subscribe", [], flow) == {"link"}


def test_role_mismatch_finding_flags_a_role_never_confirmed_for_that_control():
    error = 'waiting for get_by_role("group", name="Passcode (default)", exact=True)'
    why = role_mismatch_finding(error, INV)
    assert why and "role=\"group\"" in why and "no role recorded" in why


def test_role_mismatch_finding_flags_a_name_never_seen_at_all():
    error = 'waiting for get_by_role("button", name="Totally Invented Button")'
    why = role_mismatch_finding(error, INV)
    assert why and "no explored page ever recorded a control named" in why


def test_role_mismatch_finding_is_silent_when_the_role_matches_what_was_observed():
    error = 'waiting for get_by_role("checkbox", name="Al Jazeera 2")'
    assert role_mismatch_finding(error, INV) is None


def test_role_mismatch_finding_is_none_for_an_unrelated_error():
    assert role_mismatch_finding("AssertionError: Page URL expected to be 'x'", INV) is None
    assert role_mismatch_finding("", INV) is None


def test_role_mismatch_finding_uses_the_locator_that_actually_failed_not_earlier_source_context():
    # real bug, found live: pytest echoes the test's source code (every locator built before the one that
    # failed) into the traceback, so a naive "first get_by_role(...) anywhere in the error" match picks up
    # an earlier, PASSING locator instead of the one Playwright actually names in its own "waiting for" line.
    error = (
        'def test_wizard(page):\n'
        '    heading = page.get_by_role("heading", name="Tune your Receiver", exact=True)\n'
        '>   expect(page.get_by_role("group", name="Passcode (default)", exact=True)).to_be_visible()\n'
        'E   AssertionError: Locator expected to be visible\n'
        'E   Call log:\n'
        'E     - waiting for get_by_role("group", name="Passcode (default)", exact=True)\n'
    )
    why = role_mismatch_finding(error, INV)
    assert why and 'role="group"' in why and 'role="heading"' not in why


# ------------------------------------------------------------------ heuristic 2: URL assertion after a non-navigating step

SPEC_BAD = '''
def test_x(page, evidence_dir):
    country_select = page.locator("#c")
    action_evidence(
        page, "01-select",
        lambda: country_select.select_option(label="Egypt"),
        lambda: expect(page).to_have_url(re.compile(r"country=Egypt")),
        evidence_dir,
    )
'''

SPEC_GOOD = '''
def test_x(page, evidence_dir):
    action_evidence(
        page, "01-select",
        lambda: page.locator("#c").select_option(label="Egypt"),
        lambda: expect(page.locator("#c")).not_to_have_value(""),
        evidence_dir,
    )
    button = page.get_by_role("button", name="Search")
    action_evidence(
        page, "02-click",
        lambda: button.click(),
        lambda: expect(page).to_have_url(re.compile(r"country=Egypt")),
        evidence_dir,
    )
'''


def test_a_url_assertion_right_after_a_select_with_no_click_is_flagged():
    why = url_after_non_navigating_step(SPEC_BAD)
    assert why and "select_option" in why and "nothing in that step clicks" in why


def test_a_url_assertion_after_an_actual_click_is_not_flagged():
    assert url_after_non_navigating_step(SPEC_GOOD) is None


def test_broken_or_empty_source_does_not_raise():
    assert url_after_non_navigating_step("not even python (") is None
    assert url_after_non_navigating_step("") is None


# ------------------------------------------------------------------ classify_failure end to end

def test_a_definite_flow_failure_is_flaky_data_not_a_test_defect():
    f = classify_failure("flow x", "flow", "https://x.test/en", "some error", "", [], definite=True)
    assert f.kind == "flaky_data" and f.severity == "P1" and "promised content never appeared" in f.summary


def test_role_mismatch_is_preferred_over_unclear():
    error = 'AssertionError: Locator expected to be visible\nE waiting for get_by_role("group", name="Passcode (default)")'
    f = classify_failure("t", "page", "https://x.test/en", error, "", INV)
    assert f.kind == "test_defect" and f.severity == "P2" and "no role recorded" in f.summary


def test_url_heuristic_is_used_when_role_heuristic_does_not_apply():
    f = classify_failure("t", "page", "https://x.test/en", "E   AssertionError: Page URL expected", SPEC_BAD, [])
    assert f.kind == "test_defect" and "nothing in that step clicks" in f.summary


def test_an_unrecognised_failure_is_left_unclear_not_guessed():
    f = classify_failure("t", "page", "https://x.test/en", "E   AssertionError: something else entirely", "", [])
    assert f.kind == "unclear" and "manual triage" in f.next_action


# ------------------------------------------------------------------ real data: the two confirmed page-spec defects

def test_the_real_frequency_search_failure_is_correctly_diagnosed_as_a_test_defect():
    error = ('E   AssertionError: Page URL expected to be \'re.compile(\'country=Afghanistan\')\'\n'
             'E   Actual value: https://sat-stg.aljazeera.tv/en/frequency-search')
    spec = (ROOT / "runs" / "sat-stg.aljazeera.tv" / "tests" / "https-sat-stg-aljazeera-tv-en-frequency-search_test.py")
    if not spec.exists():
        return                                                            # only runs when the real run data is present
    why = url_after_non_navigating_step(spec.read_text(encoding="utf-8"))
    assert why and "select_option" in why


def test_the_real_wizard_failure_is_correctly_diagnosed_as_a_test_defect():
    error = 'E     - waiting for get_by_role("group", name="Passcode (default)", exact=True)'
    inv_path = ROOT / "runs" / "sat-stg.aljazeera.tv" / "artifacts" / "https-sat-stg-aljazeera-tv-en.inventory.json"
    if not inv_path.exists():
        return
    import json
    inv = [json.loads(inv_path.read_text(encoding="utf-8"))]
    why = role_mismatch_finding(error, inv)
    assert why and 'role="group"' in why and "Passcode (default)" in why


# ------------------------------------------------------------------ heuristic 3: corrupted captured text

def test_mojibake_finding_flags_the_unicode_replacement_character():
    f = mojibake_finding("t", "page", "https://x.test/en", "footer: � 2026 Al Jazeera Media Network")
    assert f and f.kind == "capture_corruption" and f.severity == "P2" and "U+FFFD" in f.summary
    assert "2026 Al Jazeera" in f.summary


def test_mojibake_finding_is_silent_on_clean_text():
    assert mojibake_finding("t", "page", "https://x.test/en", "footer: © 2026 Al Jazeera Media Network") is None
    assert mojibake_finding("t", "page", "https://x.test/en", "") is None
    assert mojibake_finding("t", "page", "https://x.test/en", None) is None


def test_mojibake_finding_is_silent_on_legitimately_accented_text():
    # a real risk with a byte-pattern guess (e.g. "Ã©") is flagging correctly-decoded French/Arabic text;
    # U+FFFD never appears in correctly decoded text, so there is nothing to guess here.
    assert mojibake_finding("t", "page", "https://x.test/ar", "مرحبا بك") is None
    assert mojibake_finding("t", "page", "https://x.test/fr", "Bienvenue à la Cafétéria") is None


def test_collect_findings_flags_mojibake_in_an_outcome_error_and_in_a_page_inventory():
    outcome = SimpleNamespace(title="t", status="passed", error=None, evidence=[], attachments=[])
    bad = SimpleNamespace(title="t_bad", status="passed", error="E footer: � 2026", evidence=[], attachments=[])
    report = SimpleNamespace(url="https://x.test/en", spec_path=None, outcomes=[outcome, bad])
    inv = [{"url": "https://x.test/ar", "accessibility": "text: � 2026"}]
    run = SimpleNamespace(url_reports=[report], tested_flows=[])
    findings = collect_findings(run, Path("/nope"), inv, {}, {})
    kinds = {f.test: f.kind for f in findings}
    assert kinds["t_bad"] == "capture_corruption"
    assert kinds["page capture: https://x.test/ar"] == "capture_corruption"


def test_the_real_footer_corruption_found_live_is_caught(tmp_path=None):
    # found while scoping this feature: the real run's own test_results.json has a replacement character
    # where the copyright symbol should be - proof this check earns its keep on real captured data.
    results = ROOT / "runs" / "sat-stg.aljazeera.tv" / "artifacts" / "test_results.json"
    if not results.exists():
        return
    import json
    data = json.loads(results.read_text(encoding="utf-8"))
    errors = " ".join(t.get("error") or "" for t in data.get("tests", []))
    if "�" not in errors:
        return                                        # the corruption may already be fixed upstream by then
    assert mojibake_finding("t", "page", "https://sat-stg.aljazeera.tv/en", errors) is not None


# ------------------------------------------------------------------ heuristic 4: declared direction vs actual script

AR_TEXT = "heading: الجزيرة بحث ترددات"
EN_TEXT = "heading: Al Jazeera frequency search results"


def test_direction_mismatch_finding_flags_rtl_content_on_a_page_not_marked_rtl():
    f = direction_mismatch_finding({"url": "https://x.test/ar", "dir": None, "accessibility": AR_TEXT})
    assert f and f.kind == "localization_mismatch" and f.severity == "P2"
    assert 'dir="ltr"' in f.summary and "rtl-script" in f.summary


def test_direction_mismatch_finding_flags_ltr_content_on_a_page_marked_rtl():
    f = direction_mismatch_finding({"url": "https://x.test/ar", "dir": "rtl", "accessibility": EN_TEXT})
    assert f and 'dir="rtl"' in f.summary and "ltr-script" in f.summary


def test_direction_mismatch_finding_is_silent_when_direction_and_script_agree():
    assert direction_mismatch_finding({"url": "https://x.test/ar", "dir": "rtl", "accessibility": AR_TEXT}) is None
    assert direction_mismatch_finding({"url": "https://x.test/en", "dir": None, "accessibility": EN_TEXT}) is None
    assert direction_mismatch_finding({"url": "https://x.test/en", "dir": "ltr", "accessibility": EN_TEXT}) is None


def test_direction_mismatch_finding_is_silent_when_there_is_nothing_to_judge_from():
    assert direction_mismatch_finding({"url": "https://x.test", "dir": "rtl", "accessibility": ""}) is None
    assert direction_mismatch_finding({"url": "https://x.test", "dir": "rtl", "accessibility": "2026"}) is None
    assert direction_mismatch_finding({"url": "https://x.test", "dir": "auto", "accessibility": AR_TEXT}) is None


def test_collect_findings_flags_a_real_direction_mismatch_from_an_inventory():
    run = SimpleNamespace(url_reports=[], tested_flows=[])
    inv = [{"url": "https://x.test/ar", "dir": None, "accessibility": AR_TEXT}]
    findings = collect_findings(run, Path("/nope"), inv, {}, {})
    assert len(findings) == 1 and findings[0].kind == "localization_mismatch"


# ------------------------------------------------------------------ flapping

def test_detect_flapping_finds_a_mixed_recent_history_and_names_the_cause():
    ratings = {"f1": [{"source": "runner", "passed": True}, {"source": "runner", "passed": False, "error": "no content shown"},
                      {"source": "runner", "passed": True}],
              "f2": [{"source": "runner", "passed": True}, {"source": "runner", "passed": True}]}
    out = detect_flapping(ratings, {"f1": "the-flow", "f2": "steady-flow"})
    assert [r.test for r in out] == ["the-flow"]
    assert out[0].sequence == "P F P" and "no content shown" in out[0].note


def test_detect_flapping_ignores_model_opinions_and_uses_only_the_recent_window():
    ratings = {"f1": [{"source": "model", "scores": {}}] + [{"source": "runner", "passed": True}] * 10
                      + [{"source": "runner", "passed": False}]}
    out = detect_flapping(ratings, window=3)
    assert len(out) == 1 and out[0].sequence == "P P F"
    steady = {"f1": [{"source": "model"}, {"source": "runner", "passed": True}, {"source": "runner", "passed": True}]}
    assert detect_flapping(steady) == []


def test_a_flow_that_never_ran_or_always_agrees_does_not_flap():
    assert detect_flapping({}) == []
    assert detect_flapping({"f1": [{"source": "runner", "passed": False}] * 3}) == []


# ------------------------------------------------------------------ environment and full collection

def test_environment_block_has_the_basics_and_never_raises_without_settings():
    run = SimpleNamespace(base_url="https://x.test", model="m", started_at="t0", finished_at="t1")
    env = environment_block(run)
    assert env["Site"] == "https://x.test" and env["Model"] == "m" and "Run window" in env
    assert "Python" in env and "OS" in env


def test_collect_findings_covers_page_and_flow_failures_and_skips_passes():
    outcome_bad = SimpleNamespace(title="t_bad", status="failed", error="E   AssertionError: Page URL expected", evidence=[], attachments=[])
    outcome_good = SimpleNamespace(title="t_good", status="passed", error=None, evidence=[], attachments=[])
    report = SimpleNamespace(url="https://x.test/en", spec_path=None, outcomes=[outcome_bad, outcome_good])
    flow_outcome = SimpleNamespace(status="failed", error="E   Locator expected to be visible")
    flow = SimpleNamespace(flow_id="f1", title="flow one", start_url="https://x.test/en", failed=True, outcome=flow_outcome)
    passing_flow = SimpleNamespace(flow_id="f2", failed=False, title="flow two", start_url="https://x.test/en", outcome=None)
    run = SimpleNamespace(url_reports=[report], tested_flows=[flow, passing_flow])
    findings = collect_findings(run, Path("/nope"), [], {}, {})
    assert [f.test for f in findings] == ["flow one", "t_bad"]              # P1 (flow) sorts before P2 (page)
    assert findings[0].severity == "P1" and findings[1].severity == "P2"
