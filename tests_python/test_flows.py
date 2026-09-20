import json
import pytest

from website_test_pipeline.flows import (
    FlowsFileError, describe_step, flow_from_primary, flow_id, load_flows, merge_flow, record_flow,
)

_PRIMARY = {
    "action": "Search", "action_selector": None,
    "steps": [{"kind": "select", "selector": "#countrylist", "name": "Country", "value": "Afghanistan"}],
    "effect": "navigates", "to": "https://x.test/en/frequency-search?country=Afghanistan",
}
_URL = "https://x.test/en"


def test_flow_from_primary_adds_click_step_and_verified_status():
    flow = flow_from_primary(_URL, _PRIMARY, now="t")
    assert flow["status"] == "verified" and flow["source"] == "explorer"
    assert [s["kind"] for s in flow["steps"]] == ["select", "click"]
    assert flow["steps"][-1]["name"] == "Search"
    assert flow["outcome"]["to"].endswith("country=Afghanistan")
    assert flow["goal"] == 'Search: select "Afghanistan" in #countrylist; click "Search"'
    assert flow["id"] == flow_id(_URL, "Search")

def test_no_visible_result_is_candidate_not_verified():
    flow = flow_from_primary(_URL, dict(_PRIMARY, effect="no-visible-result", to=None))
    assert flow["status"] == "candidate"

def test_search_form_flow_keeps_its_submit_step_without_extra_click():
    primary = {"action": 'search for "acme"', "action_selector": None, "steps": [
        {"kind": "fill", "selector": 'input[name="s"]', "name": "search field", "value": "acme"},
        {"kind": "submit", "selector": 'input[name="s"]', "name": "search field", "value": "press Enter"}],
        "effect": "results", "results_selector": ".search-results", "row_count": 5}
    flow = flow_from_primary("https://acme.io/", primary)
    assert [s["kind"] for s in flow["steps"]] == ["fill", "submit"]
    assert flow["outcome"]["results_selector"] == ".search-results" and flow["outcome"]["row_count"] == 5

def test_no_flow_when_probe_found_nothing():
    assert flow_from_primary(_URL, None) is None
    assert flow_from_primary(_URL, {"action": "Search", "steps": []}) is None

def test_describe_step_uses_short_name_when_no_selector():
    step = {"kind": "multiselect", "selector": None, "name": "Please select a channel", "value": "Al Jazeera"}
    assert describe_step(step) == 'pick "Al Jazeera" in "Please select a channel"'

def test_record_flow_writes_then_updates_without_duplicating(tmp_path):
    path = tmp_path / "flows.json"
    assert record_flow(path, _URL, _PRIMARY)["id"]
    record_flow(path, _URL, dict(_PRIMARY, effect="results", results_selector="#r", row_count=4))
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert len(doc["flows"]) == 1 and doc["version"] == 1
    assert doc["flows"][0]["outcome"]["effect"] == "results"

def test_same_flow_on_two_pages_gets_two_entries(tmp_path):
    path = tmp_path / "flows.json"
    record_flow(path, "https://x.test/", _PRIMARY)
    record_flow(path, "https://x.test/en", _PRIMARY)
    assert len(load_flows(path)["flows"]) == 2

def test_human_decision_is_never_overwritten(tmp_path):
    path = tmp_path / "flows.json"
    record_flow(path, _URL, _PRIMARY)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["flows"][0]["status"] = "approved"
    doc["flows"][0]["goal"] = "My own wording"
    path.write_text(json.dumps(doc), encoding="utf-8")
    record_flow(path, _URL, dict(_PRIMARY, to="https://x.test/changed"))
    kept = load_flows(path)["flows"][0]
    assert kept["status"] == "approved" and kept["goal"] == "My own wording"
    assert kept["outcome"]["to"].endswith("country=Afghanistan")

def test_merge_flow_reports_action():
    doc = {"flows": []}
    flow = flow_from_primary(_URL, _PRIMARY)
    assert merge_flow(doc, flow) == "added"
    assert merge_flow(doc, flow) == "updated"
    doc["flows"][0]["status"] = "rejected"
    assert merge_flow(doc, flow) == "kept"

def test_corrupt_file_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "flows.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(FlowsFileError):
        record_flow(path, _URL, _PRIMARY)
    assert path.read_text(encoding="utf-8") == "{ not json"
