import json
import logging
from types import SimpleNamespace

from website_test_pipeline.flowgen import emit_flow_spec, file_name, run_flowgen
from website_test_pipeline.flows import is_blocked, load_flows
from website_test_pipeline.intents import (
    add_intent, drop_intent, edit_intent, load_intents, save_intents, sentence_hash, sync_files, sync_flows,
)

SENTENCE = "A visitor picks a country and searches, then sees the frequencies page."
REWORDED = "A visitor picks a country and a channel, then searches for frequencies."
LOG = logging.getLogger("t")


def _flow(status="verified", **over):
    flow = {"id": "f1", "goal": SENTENCE, "source": "intent", "status": status, "start_url": "https://x.test/en",
            "steps": [{"kind": "click", "selector": None, "name": "Search"}],
            "outcome": {"effect": "navigates", "to": "/en/find"},
            "observed": {"effect": "navigates", "url": "https://x.test/en/find", "new_headings": ["Find"],
                         "step_urls": ["https://x.test/en/find"], "step_effects": ["navigates"]},
            "intent_id": "i-001", "intent_hash": sentence_hash(SENTENCE)}
    flow.update(over)
    return flow


def _intents():
    doc = {"version": 1, "intents": []}
    intent = add_intent(doc, SENTENCE, "human", "t")
    intent.update(status="expanded", flow_id="f1", expanded_hash=sentence_hash(SENTENCE))
    return doc


# ------------------------------------------------------------------ sync_flows

def test_an_untouched_sentence_leaves_its_flow_alone():
    flows = {"flows": [_flow()]}
    assert sync_flows(_intents(), flows) == [] and flows["flows"][0]["status"] == "verified"
    assert not is_blocked(flows["flows"][0])


def test_a_reworded_sentence_demotes_its_verified_flow_until_it_is_rebuilt():
    intents, flows = _intents(), {"flows": [_flow()]}
    edit_intent(intents, "i-001", REWORDED, "t2")
    assert sync_flows(intents, flows) == [("f1", "edited")]
    flow = flows["flows"][0]
    assert flow["status"] == "candidate" and flow["intent_state"] == "edited" and is_blocked(flow)
    assert sync_flows(intents, flows) == []                      # running it again changes nothing


def test_a_dropped_sentence_demotes_its_flow_too():
    intents, flows = _intents(), {"flows": [_flow()]}
    drop_intent(intents, "i-001", "not needed", "t2")
    assert sync_flows(intents, flows) == [("f1", "dropped")] and is_blocked(flows["flows"][0])


def test_a_flow_a_person_decided_keeps_its_status_and_is_not_blocked():
    for status in ("approved", "rejected"):
        intents, flows = _intents(), {"flows": [_flow(status)]}
        edit_intent(intents, "i-001", REWORDED, "t2")
        sync_flows(intents, flows)
        flow = flows["flows"][0]
        assert flow["status"] == status and flow["intent_state"] == "edited" and not is_blocked(flow)


def test_a_case_or_spacing_only_edit_does_not_block_the_flow():
    intents, flows = _intents(), {"flows": [_flow()]}
    edit_intent(intents, "i-001", SENTENCE.upper(), "t2")
    assert sync_flows(intents, flows) == []


def test_flows_not_built_from_a_sentence_and_covering_links_are_ignored():
    intents = _intents()
    covered = add_intent(intents, "A visitor opens the interactive map from the landing page.", "human", "t")
    covered.update(status="covered", flow_id="explorer-flow", expanded_hash=sentence_hash(covered["sentence"]))
    drop_intent(intents, covered["id"], "x", "t")
    flows = {"flows": [{"id": "explorer-flow", "status": "verified", "source": "explorer"}, _flow()]}
    assert sync_flows(intents, flows) == [] and flows["flows"][0]["status"] == "verified"


def test_a_rebuild_clears_the_block_because_it_replaces_the_flow():
    intents, flows = _intents(), {"flows": [_flow()]}
    edit_intent(intents, "i-001", REWORDED, "t2")
    sync_flows(intents, flows)
    rebuilt = _flow(status="candidate", intent_hash=sentence_hash(REWORDED))     # what expand stores (no intent_state)
    flows["flows"][0] = rebuilt
    assert sync_flows(intents, flows) == [] and not is_blocked(rebuilt)


# ------------------------------------------------------------------ what a blocked flow can no longer do

def _inventory():
    return {"url": "https://x.test/en", "controls": [{"tag": "button", "name": "Search", "hidden": False}],
            "headings": [], "revealed": [], "forms": [], "embeds": [], "accessibility": ""}


def test_a_blocked_flow_produces_no_test_but_a_person_approved_one_still_does():
    blocked = _flow(intent_state="edited")
    source, reason = emit_flow_spec(blocked, [_inventory()])
    assert source is None and "sentence was edited" in reason and "expand" in reason
    assert emit_flow_spec(_flow("approved", intent_state="edited"), [_inventory()])[0] is not None


def _settings(tmp_path):
    artifacts, tests = tmp_path / "artifacts", tmp_path / "tests"
    artifacts.mkdir()
    tests.mkdir()
    (artifacts / "x.inventory.json").write_text(json.dumps(_inventory()), encoding="utf-8")
    return SimpleNamespace(intents_file=tmp_path / "intents.json", flows_file=tmp_path / "flows.json",
                           ratings_file=tmp_path / "flow_ratings.json", artifacts_dir=artifacts, tests_dir=tests)


def test_editing_a_sentence_removes_the_stale_test_on_the_next_flowgen(tmp_path):
    settings = _settings(tmp_path)
    save_intents(settings.intents_file, _intents())
    settings.flows_file.write_text(json.dumps({"version": 1, "flows": [_flow()]}), encoding="utf-8")
    assert run_flowgen(settings, LOG) == 0
    spec = settings.tests_dir / file_name(_flow())
    assert spec.exists()                                                     # built from the sentence

    intents = load_intents(settings.intents_file)
    edit_intent(intents, "i-001", REWORDED, "t2")
    save_intents(settings.intents_file, intents)
    assert run_flowgen(settings, LOG) == 0
    assert not spec.exists()                                                 # the test no longer matches the sentence
    saved = load_flows(settings.flows_file)["flows"][0]
    assert saved["status"] == "candidate" and saved["intent_state"] == "edited"


def test_sync_files_never_raises_on_missing_or_broken_files(tmp_path):
    settings = _settings(tmp_path)
    sync_files(settings, LOG)                                                # no intents file: nothing to do
    settings.intents_file.write_text("not json", encoding="utf-8")
    settings.flows_file.write_text("{}", encoding="utf-8")
    sync_files(settings, LOG)                                                # broken files: reported, not raised
    sync_files(SimpleNamespace(), LOG)                                       # settings without the field at all
    assert settings.intents_file.read_text(encoding="utf-8") == "not json"
