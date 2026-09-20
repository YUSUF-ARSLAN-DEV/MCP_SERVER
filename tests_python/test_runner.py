from website_test_pipeline.runner import apply_result, classify, diff_snapshots, hop_hint, outcome_matches


def _snap(url="https://x.test/en", headings=(), controls=(), results=()):
    return {"url": url, "headings": list(headings), "controls": list(controls), "results": list(results)}


def _rows(key, rows, in_main=True):
    return {"key": key, "rows": rows, "in_main": in_main}


def test_diff_finds_only_what_is_new():
    before = _snap(headings=["Home"], controls=["button:Search"])
    after = _snap(headings=["Home", "Results"], controls=["button:Search", "input:Menu item"])
    diff = diff_snapshots(before, after)
    assert diff["new_headings"] == ["Results"] and diff["new_controls"] == ["input:Menu item"]
    assert diff["url_changed"] is False and diff["results"] == []


def test_diff_ignores_fragment_and_trailing_slash_in_url():
    assert diff_snapshots(_snap(url="https://x.test/en/"), _snap(url="https://x.test/en#top"))["url_changed"] is False
    assert diff_snapshots(_snap(url="https://x.test/en"), _snap(url="https://x.test/en?c=1"))["url_changed"] is True


def test_results_need_rows_and_main_content_and_must_be_new():
    before = _snap(results=[_rows(".old", 5)])
    after = _snap(results=[_rows(".old", 5), _rows("#freq", 9), _rows(".side", 9, in_main=False), _rows(".tiny", 2)])
    assert diff_snapshots(before, after)["results"] == [{"key": "#freq", "rows": 9}]


def test_classify_prefers_navigation_then_results_then_reveal():
    nav = classify(diff_snapshots(_snap(), _snap(url="https://x.test/en/map", results=[_rows("#r", 4)])))
    assert nav["effect"] == "navigates" and nav["results"]
    assert classify(diff_snapshots(_snap(), _snap(results=[_rows("#r", 4)])))["effect"] == "results"
    assert classify(diff_snapshots(_snap(), _snap(headings=["New"])))["effect"] == "reveals"
    assert classify(diff_snapshots(_snap(), _snap()))["effect"] == "no-visible-change"


def test_outcome_matching_rules():
    nav = {"effect": "navigates", "url": "https://x.test/en/map?a=1"}
    assert outcome_matches({"effect": "navigates", "to": "/en/map"}, nav)
    assert outcome_matches({"effect": "navigates", "to": "https://x.test/en/map"}, nav)
    assert not outcome_matches({"effect": "navigates", "to": "/en/subscribe"}, nav)
    assert not outcome_matches({"effect": "navigates"}, {"effect": "reveals", "url": "u"})
    assert outcome_matches({"effect": "results"}, {"effect": "navigates", "url": "u", "results": [{"rows": 5}]})
    assert outcome_matches({"effect": "validation"}, {"effect": "reveals", "url": "u"})
    assert not outcome_matches({}, nav)


def _result(effect="navigates", ok=True, url="https://x.test/en/map", done=2, total=2, error=None):
    return {"ok": ok, "steps_done": done, "steps_total": total, "error": error, "step_effects": ["reveals"] * done,
            "observed": {"effect": effect, "url": url, "new_headings": [], "new_controls": [], "results": []}}


def _flow(status="candidate", outcome=None):
    return {"id": "f", "status": status, "outcome": outcome or {"effect": "navigates", "to": "/en/map"}}


def test_matching_complete_run_verifies_the_flow():
    flow = _flow()
    evaluation = apply_result(flow, _result(), "t")
    assert flow["status"] == "verified" and evaluation["passed"] is True
    assert flow["observed"]["effect"] == "navigates" and flow["last_run_at"] == "t"
    assert evaluation["checks"] == {"steps_completed": "2/2", "outcome_matched": True, "observed_effect": "navigates"}


def test_failed_step_leaves_flow_a_candidate_and_records_the_error():
    flow = _flow(status="verified")
    evaluation = apply_result(flow, _result(ok=False, done=1, error="control not found", effect="no-visible-change"), "t")
    assert flow["status"] == "candidate" and evaluation["passed"] is False
    assert evaluation["error"] == "control not found" and evaluation["checks"]["steps_completed"] == "1/2"


def test_prediction_mismatch_is_not_verified():
    flow = _flow(outcome={"effect": "navigates", "to": "/en/subscribe"})
    assert apply_result(flow, _result(), "t")["passed"] is False and flow["status"] == "candidate"


def test_no_visible_change_is_not_verified_even_when_steps_ran():
    flow = _flow(outcome={"effect": "reveals"})
    assert apply_result(flow, _result(effect="no-visible-change"), "t")["passed"] is False


def test_human_status_is_never_changed_by_a_run():
    for status in ("approved", "rejected"):
        flow = _flow(status=status)
        apply_result(flow, _result(ok=False, done=0, error="boom", effect="no-visible-change"), "t")
        assert flow["status"] == status


def test_apply_result_keeps_where_every_step_ended_up():
    flow = {"id": "f", "status": "candidate", "outcome": {"effect": "navigates", "to": "/en/map"}}
    result = {"ok": True, "steps_done": 2, "steps_total": 2, "error": None,
              "step_effects": ["navigates", "navigates"], "landed_url": "https://x.test/en",
              "step_urls": ["https://x.test/en/list", "https://x.test/en/map"],
              "observed": {"effect": "navigates", "url": "https://x.test/en/map", "new_headings": [],
                           "new_controls": [], "results": []}}
    apply_result(flow, result, "now")
    assert flow["observed"]["landed_url"] == "https://x.test/en"
    assert flow["observed"]["step_urls"] == ["https://x.test/en/list", "https://x.test/en/map"]
    assert flow["observed"]["step_effects"] == ["navigates", "navigates"]


def test_apply_result_still_works_for_results_without_url_history():
    flow = {"id": "f", "status": "candidate", "outcome": {"effect": "reveals"}}
    result = {"ok": True, "steps_done": 1, "steps_total": 1, "error": None, "step_effects": ["reveals"],
              "observed": {"effect": "reveals", "url": "u", "new_headings": ["h"], "new_controls": [], "results": []}}
    apply_result(flow, result, "now")
    assert flow["observed"]["step_urls"] == [] and flow["observed"]["landed_url"] is None


def test_hop_hint_names_the_expected_and_actual_page_on_a_later_hop():
    step = {"kind": "click", "name": "Subscribe", "page": "/en/list"}
    assert hop_hint(step, 1, "https://x.test/en/map") ==         " (hop 2: step expects page /en/list, browser is on /en/map)"


def test_hop_hint_is_silent_when_pages_agree_or_it_is_the_first_step_or_there_is_no_page():
    step = {"kind": "click", "name": "Go", "page": "/en/list"}
    assert hop_hint(step, 1, "https://x.test/en/list/?a=1") == ""   # same page, query and slash ignored
    assert hop_hint(step, 0, "https://x.test/en") == ""             # step 1: start URL may redirect (/ -> /en)
    assert hop_hint({"kind": "click", "name": "Go"}, 2, "https://x.test/en") == ""   # explorer flows carry no page


# ------------------------------------------------------------------ settling and content promises

def test_a_heading_spelled_differently_is_not_a_new_heading():
    before = _snap(headings=["Find Al Jazeera Near You", "Satellite Frequencies"])
    after = _snap(headings=["Find Aljazeera Near You", "Satellite  frequencies", "Here are the results"])
    assert diff_snapshots(before, after)["new_headings"] == ["Here are the results"]


def test_non_latin_headings_are_still_told_apart():
    diff = diff_snapshots(_snap(headings=["العربية"]), _snap(headings=["العربية", "نتائج البحث"]))
    assert diff["new_headings"] == ["نتائج البحث"]


def _intent_flow(goal, status="verified"):
    return {"id": "f", "source": "intent", "goal": goal, "status": status, "outcome": {"effect": "navigates", "to": "/en/find"}}


def _nav_result(new_headings=(), new_controls=(), results=()):
    observed = {"effect": "navigates", "url": "https://x.test/en/find", "new_headings": list(new_headings),
                "new_controls": list(new_controls), "results": list(results)}
    return {"ok": True, "steps_done": 1, "steps_total": 1, "error": None, "step_effects": ["navigates"], "observed": observed}


def test_a_sentence_that_promises_content_fails_when_only_the_url_changed():
    from website_test_pipeline.runner import sentence_expects_content
    flow = _intent_flow("A visitor picks a country and sees the frequencies page.")
    assert sentence_expects_content(flow)
    evaluation = apply_result(flow, _nav_result(), "now")
    assert evaluation["passed"] is False and evaluation["definite"] is True
    assert "promises content" in evaluation["error"] and flow["status"] == "candidate"


def test_the_promise_is_kept_when_a_heading_results_or_new_controls_appeared():
    for kwargs in ({"new_headings": ["Results"]}, {"new_controls": ["button:Subscribe"]}, {"results": [{"key": "#t", "rows": 5}]}):
        evaluation = apply_result(_intent_flow("A visitor sees the frequencies."), _nav_result(**kwargs), "now")
        assert evaluation["passed"] is True and "definite" not in evaluation


def test_sentences_that_promise_nothing_and_machine_goals_are_not_judged_this_way():
    from website_test_pipeline.runner import sentence_expects_content
    assert not sentence_expects_content(_intent_flow("A visitor opens the subscribe page."))
    assert not sentence_expects_content(dict(_intent_flow("Search: sees results"), source="explorer"))
    assert apply_result(_intent_flow("A visitor opens the subscribe page."), _nav_result(), "now")["passed"] is True


def test_settled_snapshot_waits_for_content_that_arrives_late(monkeypatch):
    from website_test_pipeline import runner
    states = iter([_snap(headings=["Form"]), _snap(headings=["Form", "Results"]), _snap(headings=["Form", "Results"])])
    monkeypatch.setattr(runner, "take_snapshot", lambda page: next(states))
    monkeypatch.setattr(runner, "wait_for_loaders", lambda page, ms: True)

    class _Page:
        def wait_for_timeout(self, ms): pass
        def wait_for_load_state(self, *a, **k): pass

    assert runner.settled_snapshot(_Page())["headings"] == ["Form", "Results"]      # it did not stop at the first look


def test_snapshots_equal_compares_url_headings_controls_and_results():
    from website_test_pipeline.runner import snapshots_equal
    a = _snap(headings=["H"], controls=["button:Go"], results=[_rows("#r", 3)])
    assert snapshots_equal(a, dict(a)) and not snapshots_equal(a, _snap(headings=["H", "New"], controls=["button:Go"], results=[_rows("#r", 3)]))
    assert not snapshots_equal(a, dict(a, url="https://x.test/other"))


def test_a_filter_chip_that_echoes_the_picked_option_is_not_new_content():
    flow = dict(_intent_flow("A visitor picks a channel and sees the frequencies."),
                steps=[{"kind": "multiselect", "name": "Channel", "value": "Al Jazeera 2 HD Channel"}])
    echo = _nav_result(new_controls=["button:Al Jazeera 2 HD Channel"])
    evaluation = apply_result(flow, echo, "now")
    assert evaluation["passed"] is False and "promises content" in evaluation["error"]
    real = _nav_result(new_controls=["button:Al Jazeera 2 HD Channel", "button:Subscribe"])
    assert apply_result(dict(flow, status="candidate"), real, "now")["passed"] is True


def test_content_shown_without_a_flow_counts_every_new_control():
    from website_test_pipeline.runner import content_shown
    assert content_shown({"new_controls": ["button:Go"]}) and not content_shown({})


def test_a_predicted_results_outcome_is_met_by_a_search_that_navigated_to_a_page_with_a_new_heading():
    predicted = {"effect": "results"}
    landed = {"effect": "navigates", "url": "https://x.test/find?c=1", "new_headings": ["Here are the results"], "results": []}
    assert outcome_matches(predicted, landed) is True
    assert outcome_matches(predicted, dict(landed, new_headings=[])) is False          # a bare URL change is not results
    assert outcome_matches(predicted, {"effect": "reveals", "new_headings": ["x"], "results": []}) is False
    assert outcome_matches(predicted, {"effect": "results", "results": [{"key": "#r", "rows": 5}], "new_headings": []}) is True


# ------------------------------------------------------------------ choosing which flows to verify

def _f(fid, status="verified", **extra):
    return dict({"id": fid, "status": status}, **extra)


def test_select_flows_skips_rejected_and_blocked_flows_by_default():
    from website_test_pipeline.runner import select_flows
    flows = [_f("a"), _f("b", "rejected"), _f("c", "candidate", intent_state="edited"), _f("d", "candidate")]
    todo, unmatched = select_flows(flows)
    assert [f["id"] for f in todo] == ["a", "d"] and unmatched == []


def test_failed_only_keeps_just_the_flows_that_are_not_verified_yet():
    from website_test_pipeline.runner import select_flows
    flows = [_f("a"), _f("b", "candidate"), _f("c", "stale"), _f("d", "approved")]
    assert [f["id"] for f in select_flows(flows, failed_only=True)[0]] == ["b", "c"]


def test_ids_can_be_fragments_and_unknown_ones_are_reported():
    from website_test_pipeline.runner import select_flows
    flows = [_f("site--search-by-country"), _f("site--open-map"), _f("other--search-by-name")]
    todo, unmatched = select_flows(flows, only=["search", "nope"])
    assert [f["id"] for f in todo] == ["site--search-by-country", "other--search-by-name"] and unmatched == ["nope"]
    assert [f["id"] for f in select_flows(flows, only=["site--open-map", "open-map"])[0]] == ["site--open-map"]   # no duplicates


def test_only_and_failed_only_combine():
    from website_test_pipeline.runner import select_flows
    flows = [_f("a--x", "candidate"), _f("a--y"), _f("b--x", "stale")]
    assert [f["id"] for f in select_flows(flows, only=["x"], failed_only=True)[0]] == ["a--x", "b--x"]


def test_verify_exit_codes_for_empty_and_unmatched_selections(tmp_path):
    import json, logging
    from types import SimpleNamespace
    from website_test_pipeline.runner import run_verify
    flows = tmp_path / "flows.json"
    flows.write_text(json.dumps({"version": 1, "flows": [_f("a", "verified")]}), encoding="utf-8")
    settings = SimpleNamespace(flows_file=flows, ratings_file=tmp_path / "r.json", headless=True, navigation_timeout_ms=1000,
                               intents_file=tmp_path / "i.json")
    log = logging.getLogger("t")
    assert run_verify(settings, log, failed_only=True) == 0            # nothing is failing: nothing to run is fine
    assert run_verify(settings, log, only=["zzz"]) == 2                # a reference that matches nothing is an error
    flows.write_text(json.dumps({"version": 1, "flows": []}), encoding="utf-8")
    assert run_verify(settings, log) == 2                              # no flows at all
