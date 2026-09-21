import json
import logging
from types import SimpleNamespace

from website_test_pipeline.coverage import (
    actionable_controls, compute_coverage, render_coverage, render_uncovered, step_pages,
)
from website_test_pipeline.review import run_flows_command


def _c(name, tag="button", selector=None, **extra):
    return dict({"name": name, "tag": tag, "selector": selector, "region": "other", "hidden": False}, **extra)


HOME = {"url": "https://x.test/en", "controls": [
    _c("Search"), _c("Country", tag="select", selector="#country"), _c("Subscribe", tag="a", href="/en/subscribe"),
    _c("Zoom in"), _c("Home", tag="a", href="/en", region="chrome"), _c("Hidden thing", hidden=True),
    _c("Disabled thing", disabled=True), _c("", tag="input", type="hidden", selector="#tok"), _c("Search")]}
FIND = {"url": "https://x.test/en/find", "controls": [_c("Filter results"), _c("Details", tag="a", href="/d")]}
MAP = {"url": "https://x.test/en/map", "controls": [_c("Open map"), _c("Layers")]}
INVENTORIES = [HOME, FIND, MAP]


def _flow(fid="f1", status="verified", steps=None, urls=("https://x.test/en", "https://x.test/en/find"),
          landed="https://x.test/en", start="https://x.test/en"):
    """Default journey: pick a country (stays on /en), click Search (lands on /en/find)."""
    return {"id": fid, "status": status, "start_url": start,
            "steps": steps or [{"kind": "select", "selector": "#country", "name": "Country cut"},
                               {"kind": "click", "selector": None, "name": "Search"}],
            "observed": {"landed_url": landed, "step_urls": list(urls)}}


# ------------------------------------------------------------------ what counts as a control

def test_only_visible_enabled_content_controls_count_and_each_only_once():
    names = [c["name"] or c["selector"] for c in actionable_controls(HOME)]
    assert names == ["Search", "Country", "Subscribe", "Zoom in"]        # chrome, hidden, disabled, hidden inputs, duplicates left out


def test_a_link_without_an_href_and_an_unnamed_control_are_not_journey_targets():
    inv = {"controls": [_c("Anchor", tag="a"), _c("", tag="button"), _c("", tag="button", selector="#x")]}
    assert [c["selector"] for c in actionable_controls(inv)] == ["#x"]


# ------------------------------------------------------------------ which page each step was on

def test_step_pages_follow_the_journey_from_the_page_the_start_url_landed_on():
    assert step_pages(_flow()) == ["/en", "/en"]                      # step 2 (Search) happened where step 1 left the page
    assert step_pages(_flow(urls=("https://x.test/en/find", "https://x.test/en/map"))) == ["/en", "/en/find"]
    old = {"start_url": "https://x.test/en", "steps": [{}, {}], "observed": {}}
    assert step_pages(old) == ["/en", "/en"]                             # a flow verified before URLs were recorded
    assert step_pages({"start_url": "https://x.test/", "steps": []}) == []


# ------------------------------------------------------------------ coverage

def test_pages_and_controls_touched_by_a_tested_flow_are_counted():
    cov = compute_coverage(INVENTORIES, [_flow(
        steps=[{"kind": "select", "selector": "#country", "name": "x"}, {"kind": "click", "name": "Search"},
               {"kind": "click", "name": "Filter results"}],
        urls=("https://x.test/en", "https://x.test/en/find", "https://x.test/en/find"))])
    by = {p.path: p for p in cov.pages}
    assert by["/en"].visited and by["/en"].touched == 2 and by["/en"].total == 4        # the select and Search
    assert by["/en/find"].visited and by["/en/find"].touched == 1 and by["/en/find"].untouched == ["Details"]
    assert not by["/en/map"].visited and by["/en/map"].touched == 0
    assert (cov.pages_visited, cov.pages_total, cov.controls_touched, cov.controls_total) == (2, 3, 3, 8)
    assert (cov.percent_pages, cov.percent_controls) == (67, 38)


def test_a_step_is_matched_by_selector_or_by_name_ignoring_case_and_spacing():
    flow = _flow(steps=[{"kind": "click", "selector": None, "name": "zoom  IN"}], urls=("https://x.test/en",))
    cov = compute_coverage([HOME], [flow])
    assert cov.pages[0].touched == 1 and "Zoom in" not in cov.pages[0].untouched


def test_a_name_is_matched_by_its_start_only_when_it_was_cut_at_40_characters():
    long_name = "Use our interactive map to find your nearest frequency"
    inv = {"url": "https://x.test/en", "controls": [_c(long_name, tag="a", href="/m"), _c("Search results", tag="a", href="/r")]}
    cut = {"kind": "click", "name": long_name[:40]}
    short = {"kind": "click", "name": "Search"}
    flow = _flow(steps=[cut, short], urls=("https://x.test/en",))
    cov = compute_coverage([inv], [flow])
    assert cov.pages[0].touched == 1 and cov.pages[0].untouched == ["Search results"]     # "Search" is not "Search results"


def test_only_verified_or_approved_flows_count_and_the_rest_are_reported_as_planned():
    flows = [_flow("a", "verified"), _flow("b", "approved"), _flow("c", "candidate"), _flow("d", "stale"), _flow("e", "rejected")]
    cov = compute_coverage(INVENTORIES, flows)
    assert cov.tested_flows == 2 and cov.planned_flows == 2
    assert compute_coverage(INVENTORIES, [_flow("c", "candidate")]).pages_visited == 0


def test_the_same_page_explored_twice_counts_once_and_no_flows_means_nothing_is_covered():
    cov = compute_coverage([HOME, dict(HOME)], [])
    assert cov.pages_total == 1 and cov.pages_visited == 0 and cov.controls_touched == 0 and cov.controls_total == 4
    assert compute_coverage([], []).percent_pages == 0                       # no division by zero


def test_visited_pages_come_first_and_each_lists_the_flows_that_visit_it():
    cov = compute_coverage(INVENTORIES, [_flow("a")])
    assert [p.path for p in cov.pages][:2] == ["/en", "/en/find"] and cov.pages[0].flows == ["a"]


# ------------------------------------------------------------------ what a person and the AI are shown

def test_the_terminal_table_shows_totals_and_a_few_untouched_controls_per_page():
    text = render_coverage(compute_coverage(INVENTORIES, [_flow()]), limit=2)
    assert "pages visited 2/3 (67%)" in text and "flows: 1 tested, 0 not yet backed" in text
    assert "/en/map" in text and "Open map; Layers" in text
    assert "+1 more" not in text                                              # nothing hidden with limit=2 here
    assert "+1 more" in render_coverage(compute_coverage(INVENTORIES, [_flow()]), limit=1)
    assert render_coverage(compute_coverage([], []), 4).startswith("no explored pages")


def test_the_prompt_text_names_unvisited_pages_and_untouched_controls_and_is_empty_when_done():
    text = render_uncovered(compute_coverage(INVENTORIES, [_flow()]))
    assert text.startswith("NOT YET COVERED") and "pages no flow visits: /en/map" in text
    assert "/en: not acted on: Subscribe; Zoom in" in text and "Search" not in text.split("/en/find")[0].split("/en:")[1]
    finished = {"url": "https://x.test/en", "controls": [_c("Search")]}
    assert render_uncovered(compute_coverage([finished], [_flow(steps=[{"kind": "click", "name": "Search"}] * 2, urls=("https://x.test/en",))])) == ""


# ------------------------------------------------------------------ the command

def _settings(tmp_path, flows):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for n, inv in enumerate(INVENTORIES):
        (artifacts / f"p{n}.inventory.json").write_text(json.dumps(inv), encoding="utf-8")
    (tmp_path / "flows.json").write_text(json.dumps({"version": 1, "flows": flows}), encoding="utf-8")
    (tmp_path / "urls.txt").write_text("https://x.test/en\nhttps://x.test/en/find\n", encoding="utf-8")
    return SimpleNamespace(flows_file=tmp_path / "flows.json", ratings_file=tmp_path / "r.json", artifacts_dir=artifacts,
                           urls_file=tmp_path / "urls.txt", intents_file=tmp_path / "i.json")


def _run(settings, *words):
    lines = []
    code = run_flows_command(settings, list(words), out=lines.append)
    return code, "\n".join(lines)


def test_flows_coverage_prints_the_table_for_the_urls_being_tested(tmp_path):
    code, text = _run(_settings(tmp_path, [_flow()]), "coverage")
    assert code == 0 and "pages visited 2/2" in text and "/en/map" not in text          # /en/map is not in urls.txt
    assert "Subscribe" in text


def test_the_command_takes_an_optional_limit_and_works_with_no_flows(tmp_path):
    settings = _settings(tmp_path, [])
    code, text = _run(settings, "coverage", "1")
    assert code == 0 and "pages visited 0/2" in text and "+" in text
    (tmp_path / "urls.txt").unlink()                                                     # no url list: all explored pages
    assert "pages visited 0/3" in _run(settings, "coverage")[1]
