import pytest

from website_test_pipeline import runner
from website_test_pipeline.review import render_show, _rating_line
from website_test_pipeline.runner import apply_result, find_replacement, run_flow


def _c(name, tag="button", selector=None, **extra):
    return dict({"name": name, "tag": tag, "selector": selector, "hidden": False}, **extra)


# ------------------------------------------------------------------ the matcher is conservative

def test_the_same_name_with_a_new_selector_is_the_same_control():
    step = {"kind": "click", "selector": "#old-id", "name": "Search"}
    rep, how = find_replacement(step, [_c("Search", selector="#new-id"), _c("Home", selector="#home")])
    assert rep == {"selector": "#new-id", "name": "Search", "role": "button"} and "selector or role changed" in how


def test_names_compare_ignoring_case_spacing_and_the_40_character_cut():
    step = {"kind": "click", "selector": None, "name": "use our interactive map to find you"[:40]}
    long_name = "Use our  Interactive Map to find your nearest frequency"
    rep, _ = find_replacement(step, [_c(long_name, tag="a", selector="#map-link")])
    assert rep["selector"] == "#map-link" and rep["role"] == "link" and rep["name"] == long_name[:40]


def test_a_relabelled_control_is_found_when_it_is_nearly_the_same_name():
    rep, how = find_replacement({"kind": "click", "selector": None, "name": "Search"}, [_c("Search now", selector="#go"), _c("Menu")])
    assert rep["name"] == "Search now" and 'renamed (was "Search")' in how


def test_a_completely_different_control_is_never_used():
    rep, why = find_replacement({"kind": "click", "selector": None, "name": "Search"}, [_c("Subscribe Now"), _c("Login")])
    assert rep is None and "no control on the page looks like it" in why


def test_two_candidates_mean_no_healing_because_a_wrong_guess_passes_for_the_wrong_reason():
    step = {"kind": "click", "selector": None, "name": "Search"}
    rep, why = find_replacement(step, [_c("Search", selector="#a"), _c("Search", selector="#b")])
    assert rep is None and "several controls have that name" in why
    rep, why = find_replacement(step, [_c("Search now", selector="#a"), _c("Search all", selector="#b")])
    assert rep is None and "several controls look similar" in why


def test_the_kind_of_step_limits_what_can_stand_in():
    select_step = {"kind": "select", "selector": "#gone", "name": "Country"}
    assert find_replacement(select_step, [_c("Country", tag="div", selector="#x")])[0] is None
    assert find_replacement(select_step, [_c("Country", tag="select", selector="#c")])[0]["selector"] == "#c"
    fill_step = {"kind": "fill", "selector": "#gone", "name": "Email"}
    assert find_replacement(fill_step, [_c("Email", tag="input", selector="#e", type="text")])[0]["role"] == "textbox"


def test_hidden_controls_and_volatile_ids_are_not_used_as_selectors():
    step = {"kind": "click", "selector": "#old", "name": "Search"}
    assert find_replacement(step, [_c("Search", selector="#s", hidden=True)])[0] is None
    rep, _ = find_replacement(step, [_c("Search", selector="#radix-123456", volatile_id=True)])
    assert rep == {"selector": None, "name": "Search", "role": "button"}                 # found by role and name instead


def test_a_control_with_no_stable_way_to_find_it_is_not_healed():
    step = {"kind": "click", "selector": None, "name": "Filter"}
    rep, why = find_replacement(step, [_c("Filter", tag="div", selector=None)])
    assert rep is None and "neither a stable selector nor a role" in why


def test_the_same_control_that_is_already_found_is_not_reported_as_a_heal():
    step = {"kind": "click", "selector": "#s", "name": "Search", "role": "button"}
    assert "another reason" in find_replacement(step, [_c("Search", selector="#s")])[1]
    assert find_replacement({"kind": "click", "name": ""}, [_c("Search")])[0] is None


# ------------------------------------------------------------------ running a flow with healing

def _snap(url="https://x.test/en", headings=()):
    return {"url": url, "headings": list(headings), "controls": [], "results": []}


@pytest.fixture
def fake_browser(monkeypatch):
    """A page where '#old-search' no longer exists but a control called 'Search now' does."""
    log = {"steps": []}

    class _Page:
        url = "https://x.test/en"

        def goto(self, *a, **k): pass

    def do_step(page, step, seen=None):
        log["steps"].append((step.get("selector"), step.get("name")))
        if step.get("selector") == "#old-search":
            raise RuntimeError("control not found")

    monkeypatch.setattr(runner, "settle_page", lambda p: None)
    monkeypatch.setattr(runner, "dismiss_overlays", lambda p: None)
    monkeypatch.setattr(runner, "take_snapshot", lambda p: _snap())
    monkeypatch.setattr(runner, "settled_snapshot", lambda p: _snap("https://x.test/en/find", ["Results"]))
    monkeypatch.setattr(runner, "_do_step", do_step)
    monkeypatch.setattr(runner, "_visible_controls", lambda p: [_c("Search now", selector="#new-search")])
    return _Page(), log


def _flow(status="candidate"):
    return {"id": "f", "status": status, "start_url": "https://x.test/en", "outcome": {"effect": "navigates", "to": "/en/find"},
            "steps": [{"kind": "click", "selector": "#old-search", "name": "Search"}]}


def test_with_healing_the_step_is_retried_with_the_replacement_and_recorded(fake_browser):
    page, log = fake_browser
    result = run_flow(page, _flow(), heal=True)
    assert result["ok"] and result["error"] is None
    assert log["steps"] == [("#old-search", "Search"), ("#new-search", "Search now")]
    heal = result["heals"][0]
    assert heal["step"] == 1 and heal["was"]["selector"] == "#old-search" and heal["now"]["selector"] == "#new-search"


def test_without_healing_the_failure_names_the_control_that_looks_like_it(fake_browser):
    page, log = fake_browser
    result = run_flow(page, _flow("approved"), heal=False)
    assert not result["ok"] and result["heals"] == []
    assert 'the page has "Search now"' in result["error"] and "decided by a person" in result["error"]
    assert len(log["steps"]) == 1                                                        # nothing was retried


def test_no_replacement_gives_a_clear_reason(fake_browser, monkeypatch):
    page, _ = fake_browser
    monkeypatch.setattr(runner, "_visible_controls", lambda p: [_c("Subscribe")])
    result = run_flow(page, _flow(), heal=True)
    assert not result["ok"] and "nothing could stand in for it" in result["error"]


def test_a_replacement_that_also_fails_is_reported_and_not_kept(fake_browser, monkeypatch):
    page, _ = fake_browser

    def always_fail(page, step, seen=None):
        raise RuntimeError("control not found" if step.get("selector") == "#old-search" else "Timeout 2000ms exceeded")

    monkeypatch.setattr(runner, "_do_step", always_fail)
    result = run_flow(page, _flow(), heal=True)
    assert not result["ok"] and result["heals"] == [] and 'tried "Search now" instead' in result["error"]


def test_other_failures_are_not_treated_as_a_missing_control(fake_browser, monkeypatch):
    page, log = fake_browser
    monkeypatch.setattr(runner, "_do_step", lambda p, s, seen=None: (_ for _ in ()).throw(RuntimeError("no selectable option")))
    result = run_flow(page, _flow(), heal=True)
    assert not result["ok"] and result["heals"] == [] and "no selectable option" in result["error"]


# ------------------------------------------------------------------ keeping a heal

def _passing_result(heals):
    return {"ok": True, "steps_done": 1, "steps_total": 1, "error": None, "step_effects": ["navigates"], "step_urls": ["u"],
            "landed_url": "u", "heals": heals,
            "observed": {"effect": "navigates", "url": "https://x.test/en/find", "new_headings": ["Results"], "new_controls": [], "results": []}}


HEAL = {"step": 1, "how": 'control renamed (was "Search")', "was": {"selector": "#old-search", "name": "Search", "role": None},
        "now": {"selector": "#new-search", "name": "Search now", "role": "button"}}


def test_a_heal_is_kept_only_when_the_whole_run_passed_and_the_old_values_are_remembered():
    flow = _flow()
    evaluation = apply_result(flow, _passing_result([HEAL]), "t1")
    assert evaluation["passed"] and evaluation["healed"] == [HEAL] and flow["status"] == "verified"
    step = flow["steps"][0]
    assert (step["selector"], step["name"], step["role"]) == ("#new-search", "Search now", "button")
    assert flow["heal_history"][0]["was"]["selector"] == "#old-search" and flow["heal_history"][0]["at"] == "t1"


def test_a_heal_is_not_kept_when_the_run_still_failed():
    flow = _flow()
    result = _passing_result([HEAL])
    result.update(ok=False, error="step 2 failed")
    evaluation = apply_result(flow, result, "t1")
    assert not evaluation["passed"] and "healed" not in evaluation and evaluation["heal_not_kept"] == [HEAL]
    assert flow["steps"][0]["selector"] == "#old-search" and "heal_history" not in flow


def test_a_flow_a_person_decided_is_never_rewritten_by_a_heal():
    flow = _flow("approved")
    apply_result(flow, _passing_result([HEAL]), "t1")
    assert flow["steps"][0]["selector"] == "#old-search" and flow["status"] == "approved" and "heal_history" not in flow


def test_a_step_with_a_role_is_found_by_that_role_first():
    class _Loc:
        def __init__(self, found): self.found, self.first = found, self
        def count(self): return 1 if self.found else 0

    class _Page:
        def __init__(self): self.roles = []
        def get_by_role(self, role, name, exact):
            self.roles.append(role)
            return _Loc(role == "tab")

    page = _Page()
    assert runner._locate(page, {"kind": "click", "name": "Details", "role": "tab"}) is not None and page.roles == ["tab"]


# ------------------------------------------------------------------ showing it to a person

def test_flows_show_and_history_lines_tell_the_story_of_a_heal():
    flow = dict(_flow(), goal="g", source="intent", observed=None, heal_history=[{"at": "2026-09-21T10:00:00", **HEAL}])
    text = render_show(flow, [])
    assert "healed" in text and "step 1" in text and "'#old-search'" in text and "'#new-search'" in text
    line = _rating_line({"source": "runner", "passed": True, "checks": {"steps_completed": "1/1", "observed_effect": "navigates"},
                         "healed": [HEAL]})
    assert "healed step 1" in line and "Search -> Search now" in line


# ------------------------------------------------------------------ roles and name matching agree with the spec

def test_a_recorded_role_that_is_not_a_real_aria_role_is_replaced_by_one_derived_from_the_tag():
    from website_test_pipeline.runner import _role_for
    assert _role_for({"tag": "button", "role": "submit"}) == "button"           # the explorer can record an input type here
    assert _role_for({"tag": "input", "type": "submit", "role": "submit"}) == "button"
    assert _role_for({"tag": "div", "role": "tab"}) == "tab"                    # a real role is kept
    assert _role_for({"tag": "a"}) == "link" and _role_for({"tag": "textarea"}) == "textbox"
    assert _role_for({"tag": "div"}) is None


def test_flowgen_uses_the_same_role_rule_as_the_runner():
    from website_test_pipeline.flowgen import _role_of
    assert _role_of({"tag": "button", "role": "submit"}) == "button"


def test_names_are_matched_exactly_or_by_prefix_when_cut_like_the_generated_spec():
    import re

    calls = []

    class _Loc:
        first = None

    class _Page:
        def get_by_role(self, role, name, exact=None):
            calls.append((role, name, exact))
            loc = _Loc()
            loc.first = loc
            return loc

    runner._by_role(_Page(), "button", "Search")
    assert calls[-1] == ("button", "Search", True)                              # not a substring match
    long_name = "x" * 40
    runner._by_role(_Page(), "link", long_name)
    role, name, exact = calls[-1]
    assert role == "link" and isinstance(name, re.Pattern) and name.search(long_name + " and more") and exact is None


# ------------------------------------------------------------------ choosing another option when the default shows nothing

def _promise_flow(status="candidate", value=None):
    return {"id": "f", "source": "intent", "status": status, "goal": "A visitor picks a country and sees the results.",
            "start_url": "https://x.test/en", "outcome": {"effect": "navigates", "to": "/en/find"},
            "steps": [{"kind": "select", "selector": "#country", "name": "Country", "value": value},
                      {"kind": "click", "selector": None, "name": "Search"}]}


def _run_result(content, choices=None):
    observed = {"effect": "navigates", "url": "https://x.test/en/find", "new_headings": ["Results"] if content else [],
                "new_controls": [], "results": []}
    return {"ok": True, "steps_done": 2, "steps_total": 2, "error": None, "step_effects": ["no-visible-change", "navigates"],
            "step_urls": ["a", "b"], "landed_url": "a", "heals": [], "observed": observed,
            "select_choices": choices if choices is not None else {}}


DEFAULTED = {0: {"options": ["Afghanistan", "Albania", "Algeria", "Andorra"], "chosen": "Afghanistan"}}


def test_judge_gives_the_verdict_without_recording_anything():
    from website_test_pipeline.runner import judge
    flow = _promise_flow()
    assert judge(flow, _run_result(True))["passed"] is True
    empty = judge(flow, _run_result(False))
    assert empty["passed"] is False and empty["promised"] is True and empty["matched"] and empty["changed"]
    assert "observed" not in flow and flow["status"] == "candidate"               # judging changes nothing


def test_the_next_options_are_tried_in_order_until_one_shows_content():
    from website_test_pipeline.runner import try_other_options
    tried = []

    def run_once(overrides):
        tried.append(overrides[0])
        return _run_result(content=overrides[0] == "Algeria", choices=DEFAULTED)

    better = try_other_options(_promise_flow(), _run_result(False, DEFAULTED), run_once)
    assert tried == ["Albania", "Algeria"]                                          # the default is not retried; it stops at the first hit
    heal = better["heals"][-1]
    assert heal["kind"] == "option" and heal["step"] == 1 and heal["now"] == {"value": "Algeria"}
    assert '"Afghanistan"' in heal["how"] and '"Algeria"' in heal["how"]


def test_the_search_is_bounded_and_gives_up_without_changing_the_result():
    from website_test_pipeline.runner import try_other_options, MAX_OPTION_TRIES
    calls = []
    original = _run_result(False, DEFAULTED | {0: {"options": [f"C{n}" for n in range(30)], "chosen": "C0"}})
    out = try_other_options(_promise_flow(), original, lambda o: calls.append(o) or _run_result(False))
    assert out is original and len(calls) == MAX_OPTION_TRIES


def test_a_step_with_a_value_already_chosen_by_someone_is_never_changed():
    from website_test_pipeline.runner import try_other_options
    calls = []
    original = _run_result(False, DEFAULTED)
    assert try_other_options(_promise_flow(value="Afghanistan"), original, lambda o: calls.append(o)) is original and calls == []


def test_a_trial_that_crashes_is_skipped_not_fatal():
    from website_test_pipeline.runner import try_other_options

    def run_once(overrides):
        if overrides[0] == "Albania":
            raise RuntimeError("browser died")
        return _run_result(True, DEFAULTED)

    assert try_other_options(_promise_flow(), _run_result(False, DEFAULTED), run_once)["heals"][-1]["now"]["value"] == "Algeria"


def test_a_chosen_option_becomes_the_steps_explicit_value_and_is_remembered():
    from website_test_pipeline.runner import try_other_options
    flow = _promise_flow()
    better = try_other_options(flow, _run_result(False, DEFAULTED), lambda o: _run_result(True, DEFAULTED))
    evaluation = apply_result(flow, better, "t1")
    assert evaluation["passed"] and flow["status"] == "verified" and evaluation["healed"][0]["kind"] == "option"
    assert flow["steps"][0]["value"] == "Albania"                                   # the spec will now select this label
    assert flow["heal_history"][0]["now"] == {"value": "Albania"} and flow["heal_history"][0]["at"] == "t1"


def test_the_option_history_reads_clearly_for_a_person():
    heal = {"at": "2026-09-21T10:00:00", "kind": "option", "step": 1, "how": 'the default option "A" showed no content; "B" does',
            "was": {"value": None, "name": "Country"}, "now": {"value": "B"}}
    flow = dict(_promise_flow(), goal="g", observed=None, heal_history=[heal])
    text = render_show(flow, [])
    assert 'the default option "A" showed no content; "B" does' in text
    line = _rating_line({"source": "runner", "passed": True, "checks": {"steps_completed": "2/2", "observed_effect": "navigates"},
                         "healed": [heal]})
    assert "healed step 1" in line and '"B" does' in line and "None" not in line


def test_real_options_leave_out_placeholders():
    class _Sel:
        def evaluate(self, js):
            return [{"v": "", "t": "Please select a country"}, {"v": "1", "t": "Egypt"}, {"v": "-1", "t": "All"}, {"v": "2", "t": "Qatar"}]

    assert runner._real_options(_Sel()) == ["Egypt", "Qatar"]
    class _Broken:
        def evaluate(self, js): raise RuntimeError("detached")
    assert runner._real_options(_Broken()) == []
