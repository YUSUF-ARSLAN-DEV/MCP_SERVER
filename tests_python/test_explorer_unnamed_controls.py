"""Controls with no accessible name (an unlabeled checkbox beside a bare caption) used to be invisible to flows:
no name and no selector meant nothing could refer to them. They now get the caption beside them as a name and a
positional selector, and flows can check / uncheck them."""
import pytest

from website_test_pipeline.explorer import _CONTROL_SEL, _CONTROLS_JS
from website_test_pipeline.flowgen import emit_flow_spec
from website_test_pipeline.proposer import validate_flow
from website_test_pipeline.runner import _do_step

PAGE = ('<main><form><input type="checkbox"> checkbox 1<br><input type="checkbox" checked> checkbox 2</form>'
        '<label for="n">Name</label><input id="n" type="text"><input type="text" placeholder="City">'
        '<input type="text" aria-label="Zip"></main>')


@pytest.fixture(scope="module")
def page():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium here: {exc}")
        yield browser.new_page()
        browser.close()


def _controls(page):
    page.set_content(PAGE)
    return page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)


def test_a_nameless_checkbox_is_named_by_its_caption_and_reachable_by_position(page):
    boxes = [c for c in _controls(page) if c.get("type") == "checkbox"]
    assert [b["name"] for b in boxes] == ["checkbox 1", "checkbox 2"]
    assert [b["selector"] for b in boxes] == ['input[type="checkbox"] >> nth=0', 'input[type="checkbox"] >> nth=1']
    # the selector must really land on the control it was made for
    assert page.locator(boxes[1]["selector"]).is_checked() is True
    assert page.locator(boxes[0]["selector"]).is_checked() is False


def test_a_control_that_already_has_a_way_to_find_it_is_left_alone(page):
    by_name = {c["name"]: c for c in _controls(page) if c.get("tag") == "input" and c.get("type") == "text"}
    assert by_name["Name"]["selector"] == "#n"                       # id wins
    assert by_name["City"]["selector"] is None                        # placeholder is an accessible name
    assert by_name["Zip"]["selector"] is None                         # so is aria-label


def test_check_and_uncheck_steps_drive_the_real_checkbox(page):
    controls = _controls(page)
    first, second = [c for c in controls if c.get("type") == "checkbox"]
    _do_step(page, {"kind": "check", "selector": first["selector"], "name": first["name"]})
    _do_step(page, {"kind": "check", "selector": first["selector"], "name": first["name"]})    # again: not a failure
    _do_step(page, {"kind": "uncheck", "selector": second["selector"], "name": second["name"]})
    assert page.locator(first["selector"]).is_checked() is True
    assert page.locator(second["selector"]).is_checked() is False


INV = {"url": "https://x.test/f", "title": "F", "headings": [], "revealed": [],
       "controls": [{"tag": "input", "type": "checkbox", "name": "checkbox 1", "selector": 'input[type="checkbox"] >> nth=0'},
                    {"tag": "input", "type": "text", "name": "Your name", "selector": "#nm"},
                    {"tag": "button", "name": "Save"}]}


def _raw(action, target):
    return {"goal": "Tick and save", "start_path": "/f", "outcome": {"type": "reveals_panel"},
            "steps": [{"page": "/f", "action": action, "target": target},
                      {"page": "/f", "action": "click", "target": "Save"}]}


def _validate(raw):
    from website_test_pipeline.proposer import _index
    index = _index([INV])
    return validate_flow(raw, index, set(index))


def test_the_model_may_check_a_checkbox_but_not_a_text_field():
    steps, outcome, reason = _validate(_raw("check", "checkbox 1"))
    assert reason == "" and steps[0]["kind"] == "check" and steps[0]["selector"] == 'input[type="checkbox"] >> nth=0'
    _, _, reason = _validate(_raw("check", "Your name"))
    assert "not a checkbox" in reason
    steps, _, reason = _validate(_raw("uncheck", "checkbox 1"))
    assert reason == "" and steps[0]["kind"] == "uncheck"


def test_a_generated_spec_ticks_the_box_and_asserts_it_is_ticked():
    flow = {"id": "f", "goal": "Tick and save", "source": "intent", "status": "verified", "start_url": "https://x.test/f",
            "steps": [{"kind": "check", "selector": 'input[type="checkbox"] >> nth=0', "name": "checkbox 1"},
                      {"kind": "click", "selector": None, "name": "Save"}],
            "outcome": {"effect": "reveals"},
            "observed": {"effect": "reveals", "url": "https://x.test/f", "new_headings": ["Saved"], "new_controls": [],
                         "results": [], "step_effects": ["no-visible-change", "reveals"]}}
    source, reason = emit_flow_spec(flow, [INV])
    assert reason == "", reason
    assert ".check()" in source and "to_be_checked()" in source


def test_evidence_for_a_positional_control_is_named_after_its_caption_not_its_selector():
    from website_test_pipeline.flowgen import _target_slug
    assert _target_slug({"selector": 'input[type="checkbox"] >> nth=0', "name": "A checkbox"}) == "a-checkbox"
    assert _target_slug({"selector": "#go", "name": "Go"}) == "go"
