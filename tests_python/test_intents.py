import json
import logging
from types import SimpleNamespace

import pytest

from website_test_pipeline.intents import (
    IntentError, accept_intents, add_intent, drop_intent, edit_intent, find_intent, load_intents,
    looks_technical, needs_expansion, parse_response, prompt_for, render_intents, run_intents, sentence_hash,
)
from website_test_pipeline.review import run_flows_command

PICK = "A visitor picks a country and a channel, searches, and sees the satellite frequencies."
MAP = "A visitor opens the interactive map from the landing page and sees the map."


def _doc():
    return {"version": 1, "intents": []}


# ------------------------------------------------------------------ adding and checking

def test_a_person_can_add_a_sentence_and_it_starts_as_new():
    doc = _doc()
    intent = add_intent(doc, PICK, "human", "t1")
    assert intent["id"] == "i-001" and intent["status"] == "new" and intent["source"] == "human"
    assert intent["flow_id"] is None and intent["expanded_hash"] is None and needs_expansion(intent)


def test_sentences_are_cleaned_and_length_checked():
    doc = _doc()
    assert add_intent(doc, "  A visitor   opens   the map page  and sees it. ", "human", "t")["sentence"] == \
        "A visitor opens the map page and sees it."
    with pytest.raises(IntentError, match="too short"):
        add_intent(doc, "click search", "human", "t")
    with pytest.raises(IntentError, match="too long"):
        add_intent(doc, "word " * 80, "human", "t")


def test_the_ai_may_not_write_like_a_machine_but_a_person_may():
    machine = "Select #countrylist then click button[name=go] and check https://x.test/find"
    assert looks_technical(machine) and not looks_technical(PICK)
    with pytest.raises(IntentError, match="reads like code"):
        add_intent(_doc(), machine, "ai", "t")
    assert add_intent(_doc(), machine, "human", "t")


def test_duplicates_and_near_duplicates_are_refused_with_the_twin_named():
    doc = _doc()
    add_intent(doc, PICK, "human", "t")
    with pytest.raises(IntentError, match=r"duplicate of i-001"):
        add_intent(doc, PICK.upper(), "human", "t")
    with pytest.raises(IntentError, match=r"duplicate of i-001"):
        add_intent(doc, "A visitor picks a channel and a country, then searches and sees satellite frequencies.", "ai", "t")
    assert add_intent(doc, MAP, "human", "t")["id"] == "i-002"


def test_ids_are_never_reused_even_after_a_drop():
    doc = _doc()
    add_intent(doc, PICK, "human", "t")
    drop_intent(doc, "i-001", "", "t")
    assert add_intent(doc, MAP, "human", "t")["id"] == "i-002"


def test_a_dropped_sentence_no_longer_blocks_a_duplicate():
    doc = _doc()
    add_intent(doc, PICK, "human", "t")
    drop_intent(doc, "1", "not needed", "t")
    assert add_intent(doc, PICK, "human", "t")["id"] == "i-002"


# ------------------------------------------------------------------ editing and dropping

def test_find_intent_accepts_the_id_or_just_its_number():
    doc = _doc()
    add_intent(doc, PICK, "human", "t")
    assert find_intent(doc, "i-001") is find_intent(doc, "1")
    with pytest.raises(IntentError, match="no intent"):
        find_intent(doc, "i-999")


def test_editing_a_sentence_makes_it_due_for_expansion_again():
    doc = _doc()
    intent = add_intent(doc, PICK, "human", "t")
    intent.update(status="expanded", expanded_hash=sentence_hash(PICK), flow_id="f1")
    assert not needs_expansion(intent)
    edit_intent(doc, "i-001", "A visitor picks a country only, searches, and sees the frequencies.", "t2")
    assert needs_expansion(intent) and intent["edited_at"] == "t2" and intent["flow_id"] == "f1"


def test_a_case_or_spacing_only_edit_does_not_trigger_a_new_expansion():
    doc = _doc()
    intent = add_intent(doc, PICK, "human", "t")
    intent["expanded_hash"] = sentence_hash(PICK)
    edit_intent(doc, "i-001", PICK.replace("visitor", "VISITOR"), "t2")
    assert not needs_expansion(intent)


def test_editing_into_a_duplicate_is_refused_and_changes_nothing():
    doc = _doc()
    add_intent(doc, PICK, "human", "t")
    second = add_intent(doc, MAP, "human", "t")
    with pytest.raises(IntentError, match="duplicate of i-001"):
        edit_intent(doc, "i-002", PICK, "t2")
    assert second["sentence"] == MAP


def test_drop_records_a_reason_and_editing_a_dropped_sentence_revives_it():
    doc = _doc()
    intent = add_intent(doc, PICK, "human", "t")
    drop_intent(doc, "i-001", "needs an account", "t2")
    assert intent["status"] == "dropped" and intent["reason"] == "needs an account" and not needs_expansion(intent)
    edit_intent(doc, "i-001", "A visitor picks a country, searches, and sees the frequencies.", "t3")
    assert intent["status"] == "new"


# ------------------------------------------------------------------ the AI writes sentences

def test_parse_response_handles_fences_and_rejects_non_json():
    assert parse_response('```json\n{"intents": [{"sentence": "s"}, "junk"]}\n```') == [{"sentence": "s"}]
    with pytest.raises(ValueError):
        parse_response("sorry")
    with pytest.raises(ValueError):
        parse_response('{"flows": []}')


def test_ai_sentences_are_checked_before_they_are_stored():
    doc = _doc()
    add_intent(doc, MAP, "human", "t")
    rows = [
        {"sentence": PICK, "start_path": "/en", "evidence": "home has a country select"},
        {"sentence": "A visitor opens the interactive map from the home page and sees the map.", "start_path": "/en"},
        {"sentence": "Click #search then check the URL", "start_path": "/en"},
        {"sentence": "A visitor reads the about page and finds the team.", "start_path": "/about"},
        {"sentence": "short", "start_path": "/en"},
    ]
    added, rejected = accept_intents(rows, doc, {"/en"}, "t2", model="m")
    assert [i["id"] for i in added] == ["i-002"] and added[0]["source"] == "ai" and added[0]["start_path"] == "/en"
    assert added[0]["proposed_by"] == {"model": "m", "prompt_version": "intents-v2"}
    reasons = " | ".join(r for _, r in rejected)
    assert "duplicate" in reasons and "reads like code" in reasons and "not explored" in reasons and "too short" in reasons


def test_the_prompt_asks_for_plain_english_and_lists_what_already_exists():
    text = prompt_for("SITE MAP (1 pages explored)", ["Already there sentence one"])
    assert "plain English" in text and "NO technical words" in text and "Already there sentence one" in text
    assert "(none yet)" in prompt_for("SITE MAP", [])


def _settings(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return SimpleNamespace(intents_file=tmp_path / "intents.json", flows_file=tmp_path / "flows.json",
                           ratings_file=tmp_path / "flow_ratings.json", artifacts_dir=artifacts,
                           urls_file=tmp_path / "urls.txt", model="m")


class _Client:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def generate(self, prompt, system):
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _inventory(tmp_path):
    inv = {"url": "https://x.test/en", "title": "Home", "headings": [], "forms": [], "revealed": [], "embeds": [],
           "controls": [{"tag": "button", "name": "Search", "region": "other"}]}
    (tmp_path / "artifacts" / "x.inventory.json").write_text(json.dumps(inv), encoding="utf-8")


def test_run_intents_writes_the_ai_sentences_to_the_file(tmp_path):
    settings = _settings(tmp_path)
    _inventory(tmp_path)
    answer = json.dumps({"intents": [{"sentence": PICK, "start_path": "/en", "evidence": "e"}]})
    client = _Client(answer)
    assert run_intents(settings, ["https://x.test/en"], client, logging.getLogger("t")) == 0
    stored = load_intents(settings.intents_file)["intents"]
    assert [i["sentence"] for i in stored] == [PICK] and stored[0]["status"] == "new"
    assert run_intents(settings, ["https://x.test/en"], client, logging.getLogger("t")) == 0
    assert len(load_intents(settings.intents_file)["intents"]) == 1        # the same sentence again is a duplicate
    assert PICK in client.prompts[-1]                                        # and the model was told it exists


def test_run_intents_fails_cleanly_without_inventories_or_with_a_bad_model_answer(tmp_path):
    settings = _settings(tmp_path)
    assert run_intents(settings, ["https://x.test/en"], _Client("{}"), logging.getLogger("t")) == 2
    _inventory(tmp_path)
    assert run_intents(settings, ["https://x.test/en"], _Client("no json"), logging.getLogger("t")) == 1
    assert run_intents(settings, ["https://x.test/en"], _Client(RuntimeError("down")), logging.getLogger("t")) == 1
    assert not settings.intents_file.exists()                                # nothing written on failure


# ------------------------------------------------------------------ the commands

def _run(settings, *words, **kw):
    lines = []
    code = run_flows_command(settings, list(words), out=lines.append, **kw)
    return code, "\n".join(lines)


def test_flows_add_edit_drop_intents_round_trip_through_the_file(tmp_path):
    settings = _settings(tmp_path)
    code, text = _run(settings, "add", "A", "visitor", "picks", "a", "country", "and", "searches", "for", "frequencies")
    assert code == 0 and text.startswith("i-001 added")                      # words need no quotes
    assert _run(settings, "edit", "1", "A visitor picks a country only and sees the frequencies page.")[0] == 0
    assert _run(settings, "drop", "i-001", reason="not needed")[0] == 0
    listing = _run(settings, "intents")[1]
    assert "dropped" in listing and "not needed" in listing and "1 dropped" in listing
    stored = json.loads(settings.intents_file.read_text(encoding="utf-8"))["intents"][0]
    assert stored["status"] == "dropped" and stored["source"] == "human"


def test_the_commands_report_problems_with_exit_codes_and_change_nothing(tmp_path):
    settings = _settings(tmp_path)
    assert _run(settings, "add", "hi")[0] == 2
    assert _run(settings, "edit", "i-001")[0] == 2
    assert _run(settings, "drop", "i-009")[0] == 2
    assert not settings.intents_file.exists()
    assert "no intents yet" in _run(settings, "intents")[1]
    settings.intents_file.write_text("not json", encoding="utf-8")
    assert _run(settings, "intents")[0] == 1


def test_the_sentence_commands_never_touch_flows_json(tmp_path):
    settings = _settings(tmp_path)
    settings.flows_file.write_text("this is not even json", encoding="utf-8")   # would fail any flows command
    assert _run(settings, "add", PICK)[0] == 0
    assert not settings.ratings_file.exists()
    assert settings.flows_file.read_text(encoding="utf-8") == "this is not even json"
    assert "i-001" in render_intents(load_intents(settings.intents_file))


def test_the_ai_may_not_propose_credentials_widget_mechanics_or_language_switches():
    for sentence, why in [
        ("A visitor types their password on the home page and presses Next to continue.", "credentials"),
        ("A visitor presses Next and three more controls appear on the screen.", "widgets appearing"),
        ("A visitor switches to Arabic from the top bar and sees the Arabic version.", "language switch"),
    ]:
        with pytest.raises(IntentError, match=why):
            add_intent(_doc(), sentence, "ai", "t")


def test_a_person_may_still_write_any_of_those_because_they_take_responsibility():
    assert add_intent(_doc(), "A visitor logs in with the demo account and sees the dashboard.", "human", "t")


def test_a_real_journey_is_not_caught_by_the_unsuitable_checks():
    assert add_intent(_doc(), PICK, "ai", "t")
    assert add_intent(_doc(), "A visitor presses Subscribe Now on the search results and reaches the subscribe page.", "ai", "t")
