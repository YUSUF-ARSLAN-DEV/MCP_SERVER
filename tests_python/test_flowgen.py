import ast
import json
import logging
import re
from types import SimpleNamespace

from website_test_pipeline.flowgen import (
    MARKER, _url_regex, emit_flow_spec, file_name, run_flowgen,
)

START = "https://x.test/"


def _inventory(url=START, controls=(), headings=(), revealed=()):
    return {"url": url, "controls": list(controls), "headings": list(headings),
            "revealed": list(revealed), "forms": [], "embeds": [], "accessibility": ""}


SELECT = {"tag": "select", "role": "combobox", "name": "Country", "selector": "#countrylist",
          "options": ["Afghanistan", "Albania"], "hidden": False}
SEARCH = {"tag": "button", "role": None, "name": "Search", "selector": None, "hidden": False}


def _flow(**over):
    flow = {
        "id": "x-test--search", "goal": "Search: pick a country", "source": "explorer", "status": "verified",
        "start_url": START,
        "steps": [{"kind": "select", "selector": "#countrylist", "name": "Country", "value": "Afghanistan"},
                  {"kind": "click", "selector": None, "name": "Search"}],
        "outcome": {"effect": "navigates", "to": "/en/results"},
        "observed": {"effect": "navigates", "url": "https://x.test/en/results?country=Afghanistan",
                     "new_headings": ["Results for your country"], "new_controls": [], "results": [],
                     "step_effects": ["no-visible-change", "navigates"]},
    }
    flow.update(over)
    return flow


def test_url_regex_ignores_query_and_trailing_slash_but_not_longer_paths():
    rx = re.compile(_url_regex("/en"))
    assert rx.search("https://x.test/en") and rx.search("https://x.test/en/?a=1")
    assert not rx.search("https://x.test/en/results")
    assert re.compile(_url_regex("/")).search("https://x.test/?a=1")
    assert not re.compile(_url_regex("/")).search("https://x.test/en")


def test_navigating_flow_becomes_a_valid_strict_spec():
    source, reason = emit_flow_spec(_flow(), [_inventory(controls=[SELECT, SEARCH])])
    assert reason == "" and source.startswith(MARKER)
    ast.parse(source)
    assert "page.locator('#countrylist').first" in source
    assert "select_option(label='Afghanistan')" in source
    assert "page.get_by_role('button', name='Search', exact=True).first" in source
    assert "/en/results" in source and "to_have_url" in source
    assert "get_by_role('heading', name='Results for your country', exact=True)" in source
    assert source.count("action_evidence(") == 2  # one per step


def test_a_goal_full_of_quotes_cannot_break_the_generated_file():
    # real goal from explore: it ends in a double quote, which broke a triple-quoted docstring
    flow = _flow(goal='Search: select "Afghanistan" in #countrylist; click "Search"')
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SELECT, SEARCH])])
    assert reason == ""
    ast.parse(source)


def test_assertions_come_only_from_the_observed_run():
    source, _ = emit_flow_spec(_flow(), [_inventory(controls=[SELECT, SEARCH])])
    assert "Afghanistan" in source and "Albania" not in source
    assert "frequency" not in source


def test_only_verified_or_approved_flows_are_emitted():
    inv = [_inventory(controls=[SELECT, SEARCH])]
    for status in ("candidate", "rejected"):
        source, reason = emit_flow_spec(_flow(status=status), inv)
        assert source is None and "verified/approved/stale" in reason
    for status in ("approved", "stale"):  # a stale flow keeps its test: failing is the alarm
        assert emit_flow_spec(_flow(status=status), inv)[0] is not None


def test_a_flow_that_was_never_run_or_saw_nothing_is_skipped():
    inv = [_inventory(controls=[SELECT, SEARCH])]
    flow = _flow()
    del flow["observed"]
    assert "verify" in emit_flow_spec(flow, inv)[1]
    flow = _flow()
    flow["observed"] = {"effect": "no-visible-change", "url": START, "step_effects": []}
    assert "nothing to assert" in emit_flow_spec(flow, inv)[1]


def test_a_control_whose_role_is_unknown_is_skipped_not_guessed():
    source, reason = emit_flow_spec(_flow(), [_inventory(controls=[SELECT])])
    assert source is None and "role" in reason and "Search" in reason


def test_a_step_can_carry_its_own_role():
    flow = _flow()
    flow["steps"][1]["role"] = "button"
    assert emit_flow_spec(flow, [_inventory(controls=[SELECT])])[0] is not None


def test_a_multiselect_step_picks_the_named_option_through_the_shared_helper():
    flow = _flow()
    flow["steps"][1] = {"kind": "multiselect", "selector": None, "name": "Search", "value": "Al Jazeera 2"}
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SELECT, SEARCH])])
    assert reason == ""
    assert "pick_option(page, control, 'Al Jazeera 2')" in source
    assert "import open_page, pick_option" in source


def test_a_multiselect_step_with_no_option_is_skipped_not_guessed():
    flow = _flow()
    flow["steps"][1] = {"kind": "multiselect", "selector": None, "name": "Search", "value": None}
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SELECT, SEARCH])])
    assert source is None and "no option to pick" in reason


def test_long_names_match_by_prefix_because_flows_store_them_cut():
    stored = "Use our interactive map to find the nearest"[:40]
    button = {"tag": "a", "role": None, "name": stored + " frequency", "hidden": False}
    flow = _flow(outcome={"effect": "navigates", "to": "/en/map"})
    flow["steps"] = [{"kind": "click", "selector": None, "name": stored}]
    flow["observed"].update(url="https://x.test/en/map", step_effects=["navigates"], new_headings=[])
    source, reason = emit_flow_spec(flow, [_inventory(controls=[button])])
    locator_line = next(line for line in source.splitlines() if line.strip().startswith("control ="))
    assert reason == "" and "name=re.compile(" in locator_line and "exact=True" not in locator_line


def test_selector_with_double_quotes_survives_the_validator():
    flow = _flow()
    flow["steps"] = [{"kind": "click", "selector": 'input[name="next"]', "name": "Next"}]
    flow["observed"].update(step_effects=["reveals"], effect="reveals", url=START, new_headings=["Passcode"])
    control = {"tag": "input", "type": "submit", "name": "Next", "selector": 'input[name="next"]'}
    source, reason = emit_flow_spec(flow, [_inventory(controls=[control])])
    assert reason == "" and "'input[name=\"next\"]'" in source


def test_reveals_needs_a_heading_or_a_known_control_to_assert():
    flow = _flow(outcome={"effect": "reveals"})
    flow["observed"].update(effect="reveals", url=START, new_headings=[], new_controls=["input:Al Jazeera 2"],
                            step_effects=["no-visible-change", "reveals"])
    inv = _inventory(controls=[SELECT, SEARCH])
    assert "nothing stable" in emit_flow_spec(flow, [inv])[1]
    checkbox = {"tag": "input", "type": "checkbox", "name": "Al Jazeera 2", "hidden": False}
    inv = _inventory(controls=[SELECT, SEARCH], revealed=[{"trigger": "Search", "controls": [checkbox]}])
    source, reason = emit_flow_spec(flow, [inv])
    assert reason == "" and "get_by_role('checkbox', name='Al Jazeera 2', exact=True)" in source


def test_headings_with_numbers_or_cut_off_text_are_not_asserted():
    flow = _flow()
    flow["observed"]["new_headings"] = ["Top 10 stories today", "x" * 80, "Find your channel"]
    source, _ = emit_flow_spec(flow, [_inventory(controls=[SELECT, SEARCH])])
    assert "Find your channel" in source and "Top 10" not in source and "xxxxx" not in source


def test_results_region_is_asserted_when_the_run_saw_one():
    flow = _flow(outcome={"effect": "results"})
    flow["observed"].update(effect="results", url=START, new_headings=[], step_effects=["no-visible-change", "results"],
                            results=[{"key": "#freq-table", "rows": 9}])
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SELECT, SEARCH])])
    assert reason == "" and "page.locator('#freq-table').first" in source


def test_a_redirect_at_load_is_not_mistaken_for_the_start_url():
    # real case: the start URL is / but the site lands on /en before any step runs
    flow = _flow(outcome={"effect": "reveals"})
    flow["steps"] = [{"kind": "click", "selector": None, "name": "Search"}]
    flow["observed"].update(effect="reveals", url="https://x.test/en", step_effects=["reveals"],
                            new_headings=["Passcode"])
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SEARCH])])
    assert reason == "" and "/en/?" in source.replace("\\", "") and '"^https?' not in source


def test_a_navigation_in_the_middle_is_not_asserted_with_a_guessed_url():
    flow = _flow()
    flow["steps"] = [{"kind": "click", "selector": None, "name": "Search"}] * 3
    flow["observed"]["step_effects"] = ["navigates", "reveals", "navigates"]
    source, _ = emit_flow_spec(flow, [_inventory(controls=[SEARCH])])
    # the hop at step 1 and the step after it happen on a page whose URL the run did not record
    assert source.count('expect(page.locator("body")).to_be_visible()') == 2
    assert source.count("to_have_url") == 1                                   # only the last hop's landing URL is known


def test_run_flowgen_writes_specs_and_removes_only_stale_generated_ones(tmp_path):
    artifacts, tests = tmp_path / "artifacts", tmp_path / "tests"
    artifacts.mkdir()
    tests.mkdir()
    (artifacts / "x.inventory.json").write_text(json.dumps(_inventory(controls=[SELECT, SEARCH])), encoding="utf-8")
    flows = tmp_path / "flows.json"
    good, rejected = _flow(), _flow(id="x-test--old", status="rejected")
    flows.write_text(json.dumps({"version": 1, "flows": [good, rejected]}), encoding="utf-8")
    (tests / file_name(rejected)).write_text(MARKER + "\n", encoding="utf-8")   # left over from an earlier run
    (tests / "flow_by_hand_test.py").write_text("# written by a person\n", encoding="utf-8")
    settings = SimpleNamespace(flows_file=flows, artifacts_dir=artifacts, tests_dir=tests)

    assert run_flowgen(settings, logging.getLogger("t")) == 0
    assert (tests / file_name(good)).exists()
    assert not (tests / file_name(rejected)).exists()
    assert (tests / "flow_by_hand_test.py").exists()


def test_run_flowgen_needs_flows(tmp_path):
    settings = SimpleNamespace(flows_file=tmp_path / "none.json", artifacts_dir=tmp_path, tests_dir=tmp_path / "t")
    assert run_flowgen(settings, logging.getLogger("t")) == 2


def test_every_hop_is_asserted_with_its_own_landing_url_when_the_run_recorded_them():
    flow = _flow(outcome={"effect": "navigates", "to": "/en/map"})
    flow["steps"] = [{"kind": "click", "selector": None, "name": "Search"}] * 3
    flow["observed"].update(url="https://x.test/en/map", new_headings=[],
                            step_effects=["navigates", "reveals", "navigates"],
                            step_urls=["https://x.test/en/list", "https://x.test/en/list", "https://x.test/en/map"])
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SEARCH])])
    assert reason == ""
    assert 'expect(page.locator("body"))' not in source           # no hop is left unchecked any more
    urls = re.findall(r'to_have_url\(re\.compile\(r"([^"]+)"', source)
    assert [u.split("/?")[0].replace("\\", "") for u in urls] == ["/en/list", "/en/list", "/en/map"]


def test_a_redirect_at_load_no_longer_matters_when_step_urls_are_known():
    flow = _flow(outcome={"effect": "reveals"})
    flow["steps"] = [{"kind": "click", "selector": None, "name": "Search"}]
    flow["observed"].update(effect="reveals", url="https://x.test/en", step_effects=["reveals"],
                            step_urls=["https://x.test/en"], landed_url="https://x.test/en", new_headings=["Passcode"])
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SEARCH])])
    assert reason == "" and "/en" in source and '"^https?' not in source


def test_a_run_with_a_different_number_of_step_urls_falls_back_to_the_old_behaviour():
    flow = _flow()
    flow["observed"]["step_urls"] = ["https://x.test/en/results"]   # 1 url for 2 steps: not trustworthy
    source, reason = emit_flow_spec(flow, [_inventory(controls=[SELECT, SEARCH])])
    assert reason == "" and source.count("to_have_url") == 1
