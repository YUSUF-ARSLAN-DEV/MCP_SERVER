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
