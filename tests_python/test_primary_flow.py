import pytest

from website_test_pipeline.models import PageInventory
from website_test_pipeline.validator import validate_python_spec, SpecError
from website_test_pipeline.generator import _compact_primary_flow, prompt_for
from website_test_pipeline.explorer import (
    _probe_primary_flow, _PLACEHOLDER_OPT, _ACTION_VERB, _plausible_value,
)

_FLOW_RESULTS = {
    "action": "Search", "action_selector": None,
    "steps": [
        {"kind": "select", "selector": "#countrylist", "name": "Country", "value": "Afghanistan"},
        {"kind": "multiselect", "selector": None, "name": "Please select a channel", "value": "Al Jazeera Arabic"},
    ],
    "effect": "results", "results_selector": "#freq-results", "results_role": "table",
    "row_count": 14, "results_text": "Frequency 11662 V ...",
}


# ------------------------------------------------------------------- rendering

def test_compact_primary_flow_results():
    out = _compact_primary_flow(_FLOW_RESULTS)
    assert 'action button: "Search"' in out
    assert "step 1: select selector=#countrylist" in out
    assert "step 2: multiselect" in out
    assert "results appeared in #freq-results (14 rows)" in out

def test_compact_primary_flow_navigates():
    out = _compact_primary_flow({"action": "Find", "steps": [{"kind": "fill", "name": "q", "value": "test"}],
                                 "effect": "navigates", "to": "https://x.test/search?q=test"})
    assert "navigated to /search" in out

def test_compact_primary_flow_none():
    assert _compact_primary_flow(None) == ""


# ------------------------------------------------------------------- prompt

def test_prompt_includes_primary_flow_block_and_rule():
    inv = PageInventory("https://x.test/find", "Find", primary_flow=_FLOW_RESULTS)
    p = prompt_for("G", "persona", inv)
    assert "reproduce it as test #1" in p and "#freq-results" in p   # the block
    assert "THIS PAGE'S SMOKE TEST" in p                              # the rule

def test_prompt_omits_primary_flow_block_when_none():
    p = prompt_for("G", "p", PageInventory("https://x.test", "T"))
    assert "reproduce it as test #1" not in p        # no block
    assert "THIS PAGE'S SMOKE TEST" in p             # rule text still present


# ------------------------------------------------------------------- validator

def test_validator_accepts_flow_selectors_and_values():
    inv = PageInventory("https://x.test/find", "Find", primary_flow=_FLOW_RESULTS)
    src = (
        'import re\n'
        'def test_search_returns_results(page, evidence_dir):\n'
        '    # https://x.test/find\n'
        '    def act():\n'
        '        page.locator("#countrylist").select_option(label="Afghanistan")\n'
        '        page.get_by_role("button", name="Search", exact=True).click()\n'
        '    action_evidence(page, "search", act,\n'
        '        lambda: expect(page.locator("#freq-results tr").first).to_be_visible(), evidence_dir)\n'
    )
    validate_python_spec(src, "https://x.test/find", inv)

def test_validator_still_rejects_unrelated_selector_with_flow():
    inv = PageInventory("https://x.test/find", "Find", primary_flow=_FLOW_RESULTS)
    src = (
        'def test_x(page, evidence_dir):\n'
        '    # https://x.test/find\n'
        '    observation_evidence(page, "x", lambda: expect(page.locator("#ghost-widget")).to_be_visible(), evidence_dir)\n'
    )
    with pytest.raises(SpecError):
        validate_python_spec(src, "https://x.test/find", inv)


# ------------------------------------------------------------------- helpers

@pytest.mark.parametrize("label", ["Please select a country", "-- choose --", "All", "Any", "  --- "])
def test_placeholder_option_matches(label):
    assert _PLACEHOLDER_OPT.match(label)

@pytest.mark.parametrize("label", ["Afghanistan", "Al Jazeera Arabic", "United Kingdom"])
def test_placeholder_option_rejects_real(label):
    assert not _PLACEHOLDER_OPT.match(label)

@pytest.mark.parametrize("name", ["Search", "Find Al Jazeera", "Show results", "Apply filters", "Go"])
def test_action_verb_matches(name):
    assert _ACTION_VERB.search(name)

@pytest.mark.parametrize("name", ["Subscribe Now", "Read more", "Download PDF", "Next"])
def test_action_verb_rejects_non_actions(name):
    assert not _ACTION_VERB.search(name)

def test_plausible_value_by_type():
    assert _plausible_value({"type": "email"}) == "test@example.com"
    assert _plausible_value({"type": "number"}) == "1"
    assert _plausible_value({}) == "test"


# ------------------------------------------------------------------- probe guards (no browser needed for the None paths)

class _NoBrowser:
    def goto(self, *a, **k): raise AssertionError("should not navigate")

def test_probe_returns_none_without_action_button():
    controls = [{"tag": "select", "name": "Country", "region": "content", "options": ["1", "2"]}]
    assert _probe_primary_flow(_NoBrowser(), "https://x.test", controls) is None

def test_probe_returns_none_without_fillables():
    controls = [{"tag": "button", "role": "button", "name": "Search", "region": "content"}]
    assert _probe_primary_flow(_NoBrowser(), "https://x.test", controls) is None
