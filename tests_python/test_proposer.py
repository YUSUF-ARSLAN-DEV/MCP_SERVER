import json
import logging
from types import SimpleNamespace

from website_test_pipeline.flows import load_flows
from website_test_pipeline.proposer import parse_response, propose, prompt_for, run_propose

HOME = {"url": "https://x.test/en", "title": "Home", "headings": [],
        "controls": [
            {"tag": "select", "name": "Pick a country", "selector": "#country", "region": "other",
             "options": ["Egypt", "Qatar"]},
            {"tag": "button", "name": "Search", "region": "other"},
            {"tag": "input", "type": "text", "name": "Your name", "selector": "#nm", "region": "other"}],
        "revealed": [{"trigger": "Search", "effect": "navigates", "to": "https://x.test/en/find"}]}
FIND = {"url": "https://x.test/en/find", "title": "Find", "headings": [], "controls": [
    {"tag": "button", "name": "Subscribe Now", "region": "other"}], "revealed": []}
INVS = [HOME, FIND]

def _flow(**over):
    base = {"goal": "Find frequencies by country", "start_path": "/en",
            "steps": [{"page": "/en", "action": "select", "target": "#country", "value": "Egypt"},
                      {"page": "/en", "action": "click", "target": "Search"}],
            "outcome": {"type": "navigates", "to_path": "/en/find"}, "evidence": "Home has a country select and Search."}
    base.update(over)
    return base

def test_parse_response_handles_fences_and_prose():
    raw = 'Here you go:\n```json\n{"flows": [{"goal": "g"}, "junk"]}\n```'
    assert parse_response(raw) == [{"goal": "g"}]

def test_parse_response_rejects_non_json():
    import pytest
    with pytest.raises(ValueError):
        parse_response("sorry, no")

def test_valid_flow_becomes_a_candidate_with_full_start_url():
    accepted, rejected = propose([_flow()], INVS, [], model="m", now="t")
    assert not rejected and len(accepted) == 1
    flow = accepted[0]
    assert flow["status"] == "candidate" and flow["source"] == "model"
    assert flow["start_url"] == "https://x.test/en"
    assert [s["kind"] for s in flow["steps"]] == ["select", "click"]
    assert flow["outcome"] == {"effect": "navigates", "to": "/en/find"}
    assert flow["proposed_by"] == {"model": "m", "prompt_version": "propose-v1"}

def test_unknown_control_rejects_the_whole_flow():
    bad = _flow(steps=[{"page": "/en", "action": "click", "target": "Buy now"}])
    accepted, rejected = propose([bad], INVS, [])
    assert not accepted and 'control "Buy now" not found on /en' in rejected[0][1]

def test_unexplored_page_and_outcome_are_rejected():
    _, r1 = propose([_flow(start_path="/en/map")], INVS, [])
    assert "not explored" in r1[0][1]
    _, r2 = propose([_flow(outcome={"type": "navigates", "to_path": "/en/map"})], INVS, [])
    assert "outcome page /en/map was not explored" in r2[0][1]
    _, r3 = propose([_flow(outcome={"type": "explodes"})], INVS, [])
    assert "unknown outcome" in r3[0][1]

def test_action_must_fit_the_control_type():
    bad = _flow(steps=[{"page": "/en", "action": "select", "target": "Search"}])
    assert "is not a select" in propose([bad], INVS, [])[1][0][1]
    bad = _flow(steps=[{"page": "/en", "action": "fill", "target": "Search"}])
    assert "not a text field" in propose([bad], INVS, [])[1][0][1]

def test_unknown_select_value_is_dropped_not_kept():
    flow = _flow(steps=[{"page": "/en", "action": "select", "target": "#country", "value": "Atlantis"}])
    accepted, _ = propose([flow], INVS, [])
    assert accepted[0]["steps"][0]["value"] is None

def test_duplicate_of_existing_flow_is_skipped():
    first, _ = propose([_flow()], INVS, [])
    again, rejected = propose([_flow(goal="Same steps, new words")], INVS, first)
    assert not again and rejected[0][1] == "duplicate of an existing flow"

def test_prompt_contains_rules_and_map():
    text = prompt_for("SITE MAP (1 pages explored)")
    assert "SITE MAP (1 pages explored)" in text and "Use ONLY pages and controls" in text

def test_run_propose_writes_candidates_and_only_uses_listed_urls(tmp_path):
    (tmp_path / "a.inventory.json").write_text(json.dumps(HOME), encoding="utf-8")
    (tmp_path / "b.inventory.json").write_text(json.dumps(FIND), encoding="utf-8")
    (tmp_path / "old.inventory.json").write_text(json.dumps({"url": "https://x.test/stale", "controls": []}), encoding="utf-8")
    seen = {}
    class Client:
        def generate(self, prompt, system):
            seen["prompt"] = prompt
            return json.dumps({"flows": [_flow(), _flow(goal="bad", start_path="/stale")]})
    settings = SimpleNamespace(artifacts_dir=tmp_path, flows_file=tmp_path / "flows.json",
                               urls_file="urls.txt", model="m")
    rc = run_propose(settings, ["https://x.test/en", "https://x.test/en/find"], Client(), logging.getLogger("t"))
    assert rc == 0 and "/stale" not in seen["prompt"].split("SITE MAP")[1]
    flows = load_flows(settings.flows_file)["flows"]
    assert len(flows) == 1 and flows[0]["status"] == "candidate"

def test_run_propose_needs_inventories(tmp_path):
    settings = SimpleNamespace(artifacts_dir=tmp_path, flows_file=tmp_path / "f.json", urls_file="u", model="m")
    assert run_propose(settings, ["https://x.test/"], object(), logging.getLogger("t")) == 2
