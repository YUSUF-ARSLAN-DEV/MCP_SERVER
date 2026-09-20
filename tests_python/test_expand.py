import json
import logging
from types import SimpleNamespace

from website_test_pipeline.expand import (
    drop_superseded_picks, ground_options, ground_sentence_values, parse_expansion, prompt_for, repair_option_targets,
    run_expand, upgrade_menu_clicks,
)
from website_test_pipeline.flows import load_flows
from website_test_pipeline.intents import add_intent, load_intents, save_intents, sentence_hash
from website_test_pipeline.ratings import load_ratings

SENTENCE = "A visitor picks a country and a channel, searches, and sees the frequencies."
OTHER = "A visitor opens the subscribe form from the search page."

HOME = {"url": "https://x.test/en", "title": "Home", "headings": [], "forms": [],
        "controls": [
            {"tag": "select", "name": "Pick a country", "selector": "#country", "region": "other", "options": ["Egypt", "Qatar"]},
            {"tag": "button", "name": "Please select a channel", "region": "other"},
            {"tag": "button", "name": "Search", "region": "other"}],
        "revealed": [
            {"trigger": "Please select a channel", "effect": "reveals",
             "controls": [{"tag": "input", "type": "checkbox", "name": "AJ Arabic"}, {"tag": "input", "type": "checkbox", "name": "AJ English"}]},
            {"trigger": "Search", "effect": "navigates", "to": "https://x.test/en/find"}], "embeds": []}
FIND = {"url": "https://x.test/en/find", "title": "Find", "headings": [], "forms": [], "embeds": [], "revealed": [],
        "controls": [{"tag": "button", "name": "Subscribe Now", "region": "other"}]}

GOOD = {"flows": [{"start_path": "/en", "evidence": "home has the pickers",
                   "steps": [{"page": "/en", "action": "select", "target": "#country", "value": "Egypt"},
                             {"page": "/en", "action": "pick", "target": "Please select a channel", "value": None},
                             {"page": "/en", "action": "click", "target": "Search"}],
                   "outcome": {"type": "navigates", "to_path": "/en/find"}}]}
GOOD_RATING = {"ratings": [{"n": 1, "coherence": 5, "importance": 4, "outcome_strength": 3, "reason": "clear journey"}]}


class _Client:
    """Answers expansion prompts with `expansion` and critic prompts with `rating`."""
    def __init__(self, expansion, rating=GOOD_RATING):
        self.expansion, self.rating, self.calls = expansion, rating, 0

    def generate(self, prompt, system):
        self.calls += 1
        answer = self.rating if "strict QA reviewer" in prompt else self.expansion
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)


def _settings(tmp_path, inventories=(HOME, FIND)):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    for n, inv in enumerate(inventories):
        (artifacts / f"p{n}.inventory.json").write_text(json.dumps(inv), encoding="utf-8")
    return SimpleNamespace(intents_file=tmp_path / "intents.json", flows_file=tmp_path / "flows.json",
                           ratings_file=tmp_path / "flow_ratings.json", artifacts_dir=artifacts,
                           urls_file=tmp_path / "urls.txt", model="m")


URLS = ["https://x.test/en", "https://x.test/en/find"]
LOG = logging.getLogger("t")


def _with_intents(settings, *sentences, source="human"):
    doc = {"version": 1, "intents": []}
    for s in sentences:
        add_intent(doc, s, source, "t0")
    save_intents(settings.intents_file, doc)


def _expand(settings, client, only=None):
    return run_expand(settings, URLS, client, LOG, only)


# ------------------------------------------------------------------ parsing and prompt

def test_parse_expansion_returns_the_flow_or_the_reason_it_cannot_be_built():
    flow, why = parse_expansion('```json\n' + json.dumps(GOOD) + '\n```')
    assert flow["start_path"] == "/en" and why == ""
    flow, why = parse_expansion('{"flows": [], "cannot": "no channel picker on the site"}')
    assert flow is None and why == "no channel picker on the site"
    assert parse_expansion('{"flows": []}')[0] is None


def test_parse_expansion_survives_prose_fences_and_a_second_object():
    answer, nl = json.dumps(GOOD), chr(10)
    for raw in ("Here is the flow:" + nl + answer, "```json" + nl + answer + nl + "```" + nl + "Hope that helps!",
                answer + nl + json.dumps({"flows": []})):
        assert parse_expansion(raw)[0]["start_path"] == "/en", raw


def test_parse_expansion_raises_on_a_non_answer():
    import pytest
    for bad in ("sorry", '{"intents": []}'):
        with pytest.raises(ValueError):
            parse_expansion(bad)


def test_the_prompt_carries_the_sentence_start_page_and_the_no_invention_rule():
    text = prompt_for(SENTENCE, "/en", "SITE MAP (2 pages explored)")
    assert SENTENCE in text and "STARTS ON: /en" in text and "do NOT invent it" in text
    assert "leave value null" in text and "STARTS ON" not in prompt_for(SENTENCE, None, "SITE MAP")


# ------------------------------------------------------------------ grounding a pick

def test_a_pick_step_takes_the_first_option_the_explorer_saw_open():
    step = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": None}
    assert ground_options([step], [HOME]) == "" and step["value"] == "AJ Arabic"


def test_a_named_option_must_have_been_observed_but_matching_is_forgiving():
    ok = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": "aj english"}
    assert ground_options([ok], [HOME]) == ""
    bad = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": "BBC World"}
    assert "never seen" in ground_options([bad], [HOME])


def test_a_pick_with_no_observed_options_cannot_be_built():
    step = {"kind": "multiselect", "name": "Some other menu", "page": "/en", "value": None}
    assert "nothing can be picked" in ground_options([step], [HOME])


def test_select_all_is_never_the_default_option():
    inv = dict(HOME, revealed=[{"trigger": "Please select a channel", "effect": "reveals", "controls": [
        {"tag": "input", "type": "checkbox", "name": "Select All"}, {"tag": "input", "type": "checkbox", "name": "AJ English"}]}])
    step = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": None}
    assert ground_options([step], [inv]) == "" and step["value"] == "AJ English"
    only_all = dict(HOME, revealed=[{"trigger": "Please select a channel", "effect": "reveals",
                                     "controls": [{"tag": "input", "type": "checkbox", "name": "Select all"}]}])
    assert "nothing can be picked" in ground_options([dict(step, value=None)], [only_all])


def test_a_click_on_a_menu_button_becomes_a_pick_when_the_sentence_says_to_pick_from_it():
    steps = [{"kind": "click", "name": "Please select a channel", "page": "/en", "value": None},
             {"kind": "click", "name": "Search", "page": "/en", "value": None}]
    assert upgrade_menu_clicks(steps, SENTENCE, [HOME]) == ["Please select a channel"]
    assert [s["kind"] for s in steps] == ["multiselect", "click"]           # Search has no options: untouched


def test_a_click_is_left_alone_when_the_sentence_only_opens_the_menu_or_the_button_has_no_options():
    steps = [{"kind": "click", "name": "Please select a channel", "page": "/en", "value": None}]
    assert upgrade_menu_clicks(steps, "A visitor opens the channel list and sees the channels.", [HOME]) == []
    assert steps[0]["kind"] == "click"
    assert upgrade_menu_clicks(steps, "A visitor picks a country and a channel.", [dict(HOME, revealed=[])]) == []


def test_a_pick_that_targets_an_option_is_repaired_to_target_its_menu():
    step = {"kind": "multiselect", "name": "AJ English", "page": "/en", "selector": "#opt", "value": None}
    assert repair_option_targets([step], [HOME]) == ["AJ English -> Please select a channel"]
    assert step["name"] == "Please select a channel" and step["value"] == "AJ English" and step["selector"] is None


def test_a_real_menu_button_is_left_alone_and_an_unknown_target_is_not_guessed():
    menu = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": None}
    unknown = {"kind": "multiselect", "name": "Nothing like it", "page": "/en", "value": None}
    assert repair_option_targets([menu, unknown], [HOME]) == []
    assert menu["name"] == "Please select a channel" and unknown["name"] == "Nothing like it"


def test_an_option_offered_by_two_menus_is_not_repaired_because_it_is_ambiguous():
    twin = dict(HOME, revealed=HOME["revealed"] + [{"trigger": "Other menu", "effect": "reveals",
                                                    "controls": [{"tag": "input", "type": "checkbox", "name": "AJ English"}]}])
    step = {"kind": "multiselect", "name": "AJ English", "page": "/en", "value": None}
    assert repair_option_targets([step], [twin]) == [] and step["name"] == "AJ English"


def test_the_sentence_naming_one_option_sets_the_value_the_model_left_out():
    select = {"kind": "select", "selector": "#country", "name": "Pick a country", "page": "/en", "value": None}
    pick = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": None}
    said = "A visitor picks Qatar and the AJ English channel, then searches."
    home = json.loads(json.dumps(HOME))
    home["controls"][0]["options"] = ["Please select a country", "Egypt", "Qatar"]
    assert len(ground_sentence_values([select, pick], said, [home])) == 2
    assert select["value"] == "Qatar" and pick["value"] == "AJ English"


def test_a_sentence_that_names_no_option_or_several_leaves_the_value_alone():
    home = json.loads(json.dumps(HOME))
    home["controls"][0]["options"] = ["Egypt", "Qatar"]
    select = {"kind": "select", "selector": "#country", "name": "Pick a country", "page": "/en", "value": None}
    pick = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": None}
    assert ground_sentence_values([select, pick], "A visitor picks a country and a channel.", [home]) == []
    assert ground_sentence_values([select], "A visitor compares Egypt with Qatar.", [home]) == []       # ambiguous
    assert select["value"] is None and pick["value"] is None
    named = dict(select, value="Egypt")
    assert ground_sentence_values([named], "picks Qatar", [home]) == [] and named["value"] == "Egypt"  # never overrides


def test_the_option_must_be_a_whole_word_and_select_all_is_never_chosen():
    home = json.loads(json.dumps(HOME))
    home["revealed"][0]["controls"].append({"tag": "input", "type": "checkbox", "name": "Select All"})
    pick = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": None}
    assert ground_sentence_values([pick], "the visitor chooses select all channels", [home]) == []
    assert ground_sentence_values([dict(pick)], "the AJ Englishman speaks", [home]) == []


def test_a_generic_pick_followed_by_a_specific_pick_of_the_same_menu_is_dropped():
    generic = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": None}
    specific = {"kind": "multiselect", "name": "Please select a channel", "page": "/en", "value": "AJ English"}
    search = {"kind": "click", "name": "Search", "page": "/en", "value": None}
    steps = [generic, specific, search]
    assert drop_superseded_picks(steps) == ["Please select a channel"] and steps == [specific, search]
    same = [dict(specific), dict(specific)]
    assert drop_superseded_picks(same) == ["Please select a channel"] and len(same) == 1
    different = [dict(specific, value="AJ Arabic"), dict(specific)]
    assert drop_superseded_picks(different) == [] and len(different) == 2        # two different options: both are wanted


# ------------------------------------------------------------------ the command

def test_a_sentence_becomes_a_candidate_flow_linked_back_to_it(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    assert _expand(settings, _Client(GOOD)) == 0
    flow = load_flows(settings.flows_file)["flows"][0]
    assert flow["goal"] == SENTENCE and flow["source"] == "intent" and flow["status"] == "candidate"
    assert flow["intent_id"] == "i-001" and flow["intent_hash"] == sentence_hash(SENTENCE)
    assert flow["start_url"] == "https://x.test/en" and flow["outcome"] == {"effect": "navigates", "to": "/en/find"}
    assert [s["kind"] for s in flow["steps"]] == ["select", "multiselect", "click"]
    assert flow["steps"][1]["value"] == "AJ Arabic"                      # grounded in what the explorer saw
    assert flow["proposed_by"] == {"model": "m", "prompt_version": "expand-v1"}
    intent = load_intents(settings.intents_file)["intents"][0]
    assert intent["status"] == "expanded" and intent["flow_id"] == flow["id"] and intent["expanded_hash"] == sentence_hash(SENTENCE)
    rating = load_ratings(settings.ratings_file)["ratings"][flow["id"]][0]
    assert rating["source"] == "model" and rating["kept"] is True


def test_a_pick_written_as_a_click_by_the_model_is_repaired_before_the_flow_is_stored(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    sloppy = json.loads(json.dumps(GOOD))
    sloppy["flows"][0]["steps"][1]["action"] = "click"
    _expand(settings, _Client(sloppy))
    steps = load_flows(settings.flows_file)["flows"][0]["steps"]
    assert steps[1]["kind"] == "multiselect" and steps[1]["value"] == "AJ Arabic"


def test_the_models_own_goal_is_ignored_the_sentence_is_the_goal(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    answer = json.loads(json.dumps(GOOD))
    answer["flows"][0]["goal"] = "Something the model preferred"
    _expand(settings, _Client(answer))
    assert load_flows(settings.flows_file)["flows"][0]["goal"] == SENTENCE


def _unbuildable(tmp_path, answer, rating=GOOD_RATING):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    assert _expand(settings, _Client(answer, rating)) == 0
    intent = load_intents(settings.intents_file)["intents"][0]
    assert intent["status"] == "unbuildable" and not load_flows(settings.flows_file)["flows"]
    return intent["reason"]


def test_a_sentence_the_site_cannot_support_is_marked_unbuildable_with_the_models_reason(tmp_path):
    assert "no channel picker" in _unbuildable(tmp_path, {"flows": [], "cannot": "no channel picker"})


def test_an_invented_control_makes_the_sentence_unbuildable_and_no_flow_is_written(tmp_path):
    bad = json.loads(json.dumps(GOOD))
    bad["flows"][0]["steps"][2]["target"] = "Buy now"
    assert 'control "Buy now" not found' in _unbuildable(tmp_path, bad)


def test_a_named_option_that_was_never_seen_makes_it_unbuildable(tmp_path):
    bad = json.loads(json.dumps(GOOD))
    bad["flows"][0]["steps"][1]["value"] = "BBC World"
    assert "never seen" in _unbuildable(tmp_path, bad)


def test_a_flow_the_critic_finds_incoherent_is_dropped_with_its_reason(tmp_path):
    poor = {"ratings": [{"n": 1, "coherence": 1, "importance": 2, "outcome_strength": 2, "reason": "steps ignore the sentence"}]}
    reason = _unbuildable(tmp_path, GOOD, poor)
    assert reason.startswith("critic:") and "steps ignore the sentence" in reason


def test_if_the_critic_is_down_the_flow_is_kept_unrated(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    _expand(settings, _Client(GOOD, RuntimeError("critic down")))
    assert len(load_flows(settings.flows_file)["flows"]) == 1
    assert load_ratings(settings.ratings_file)["ratings"] == {}


def test_a_sentence_that_repeats_an_existing_flow_is_marked_covered_not_duplicated(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE, "A visitor chooses a country and a channel, presses Search and lands on the results page.")
    _expand(settings, _Client(GOOD))
    intents = load_intents(settings.intents_file)["intents"]
    flows = load_flows(settings.flows_file)["flows"]
    assert len(flows) == 1 and [i["status"] for i in intents] == ["expanded", "covered"]
    assert intents[1]["flow_id"] == flows[0]["id"]


def test_a_transient_model_failure_leaves_the_sentence_untouched_to_retry(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    assert _expand(settings, _Client(RuntimeError("timeout"))) == 1
    intent = load_intents(settings.intents_file)["intents"][0]
    assert intent["status"] == "new" and intent["expanded_hash"] is None
    assert _expand(settings, _Client(GOOD)) == 0                           # next run picks it up
    assert load_intents(settings.intents_file)["intents"][0]["status"] == "expanded"


def test_nothing_is_redone_unless_the_sentence_changed_or_its_id_is_passed(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    client = _Client(GOOD)
    _expand(settings, client)
    calls = client.calls
    assert _expand(settings, client) == 0 and client.calls == calls        # already answered: no model call
    _expand(settings, client, ["i-001"])
    assert client.calls > calls                                            # forced by id
    _expand(settings, client, ["1"])                                       # a bare number works too


def test_a_reworded_sentence_rebuilds_the_same_flow_id_and_resets_it_to_candidate(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    _expand(settings, _Client(GOOD))
    doc = load_flows(settings.flows_file)
    flow_id = doc["flows"][0]["id"]
    doc["flows"][0]["status"] = "verified"
    settings.flows_file.write_text(json.dumps(doc), encoding="utf-8")
    intents = load_intents(settings.intents_file)
    intents["intents"][0]["sentence"] = "A visitor picks a country and a channel and searches for frequencies."
    save_intents(settings.intents_file, intents)
    _expand(settings, _Client(GOOD))
    flows = load_flows(settings.flows_file)["flows"]
    assert [f["id"] for f in flows] == [flow_id] and flows[0]["status"] == "candidate"
    assert flows[0]["goal"].endswith("searches for frequencies.")


def test_a_flow_decided_by_a_person_is_never_overwritten_by_a_rebuild(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    _expand(settings, _Client(GOOD))
    doc = load_flows(settings.flows_file)
    doc["flows"][0]["status"] = "approved"
    settings.flows_file.write_text(json.dumps(doc), encoding="utf-8")
    _expand(settings, _Client(GOOD), ["i-001"])
    assert load_flows(settings.flows_file)["flows"][0]["status"] == "approved"
    intent = load_intents(settings.intents_file)["intents"][0]
    assert intent["status"] == "unbuildable" and "decided by a person" in intent["reason"]


def test_two_sentences_that_would_get_the_same_flow_id_are_kept_apart(tmp_path):
    settings = _settings(tmp_path)
    second = "A visitor picks a country and then just presses Search to see the frequency page."
    _with_intents(settings, SENTENCE, second)
    short = {"flows": [{"start_path": "/en", "steps": [{"page": "/en", "action": "click", "target": "Search"}],
                        "outcome": {"type": "navigates", "to_path": "/en/find"}}]}

    class Two(_Client):
        def generate(self, prompt, system):
            if second in prompt and "strict QA reviewer" not in prompt:
                return json.dumps(short)
            return super().generate(prompt, system)

    _expand(settings, Two(GOOD))
    ids = [f["id"] for f in load_flows(settings.flows_file)["flows"]]
    assert len(ids) == 2 and len(set(ids)) == 2 and ids[1] == ids[0] + "-2"


def test_dropped_sentences_are_skipped(tmp_path):
    settings = _settings(tmp_path)
    _with_intents(settings, SENTENCE)
    intents = load_intents(settings.intents_file)
    intents["intents"][0]["status"] = "dropped"
    save_intents(settings.intents_file, intents)
    client = _Client(GOOD)
    assert _expand(settings, client) == 0 and client.calls == 0


def test_without_explored_pages_there_is_nothing_to_ground_on(tmp_path):
    settings = _settings(tmp_path, inventories=())
    _with_intents(settings, SENTENCE)
    assert _expand(settings, _Client(GOOD)) == 2


def test_a_broken_intents_file_stops_the_run_without_writing(tmp_path):
    settings = _settings(tmp_path)
    settings.intents_file.write_text("not json", encoding="utf-8")
    assert _expand(settings, _Client(GOOD)) == 1
    assert not settings.flows_file.exists()
