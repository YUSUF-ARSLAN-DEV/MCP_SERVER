from website_test_pipeline.runner import apply_result, outcome_matches


def test_a_probe_that_saw_nothing_is_confirmed_by_a_run_that_sees_something():
    for effect in ("navigates", "reveals", "results", "validation"):
        assert outcome_matches({"effect": "no-visible-result"}, {"effect": effect, "url": "https://x.test/a"})


def test_nothing_happening_is_still_not_a_match_for_a_recorded_no_visible_result():
    assert not outcome_matches({"effect": "no-visible-result"}, {"effect": "no-visible-change", "url": "https://x.test/a"})


def _result(effect, url="https://x.test/a/37"):
    return {"ok": True, "steps_done": 3, "steps_total": 3, "step_effects": [], "step_urls": [url], "landed_url": "https://x.test/a",
            "observed": {"effect": effect, "url": url, "new_headings": ["Results"], "new_controls": [], "results": [{"rows": 5}]}}


def test_a_passing_run_replaces_the_probes_guess_with_the_observed_outcome():
    flow = {"id": "f", "status": "candidate", "goal": "A visitor searches", "steps": [], "outcome": {"effect": "no-visible-result"}}
    entry = apply_result(flow, _result("navigates"), "2026-10-24T00:00:00+00:00")
    assert entry["passed"] is True and flow["status"] == "verified"
    assert flow["outcome"] == {"effect": "navigates", "to": "https://x.test/a/37"}
