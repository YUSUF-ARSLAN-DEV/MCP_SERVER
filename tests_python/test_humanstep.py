import pytest

from website_test_pipeline import humanstep
from website_test_pipeline.humanstep import (
    CodePopup, HumanAnswer, HumanRequest, can_ask, default_value, find_captcha, find_form_fields, human_mode,
    solve_before_submit,
)

PAGE = ('<form><input name="name" type="text"><img src="/x/captcha.png" alt="Image CAPTCHA">'
        '<button type="button" aria-label="Show me a different code">r</button>'
        '<label for="c">What code is in the image?</label><input id="c" name="captcha_response" type="text">'
        '<button type="submit" id="go">Send</button></form>')
FORM = ('<form><label for="n">Name *</label><input id="n" type="text" required>'
        '<label for="e">Email *</label><input id="e" type="email" required>'
        '<label for="k">Country *</label><select id="k" required><option value="">- Select Country -</option>'
        '<option>Albania</option><option>Egypt</option></select>'
        '<input type="hidden" name="form_id" value="x">'
        '<img src="/api/captcha.png" alt="Image CAPTCHA"><label for="c">What code is in the image?</label>'
        '<input id="c" type="text"><button type="submit" id="go">Send</button></form>')
NO_CAPTCHA = '<form><input name="q" type="text"><button type="submit">Search</button></form>'
IMAGE_ONLY = '<form><input name="a" type="text"><img src="/verify/code.png" alt="security code"><input name="b" type="text"><button>Go</button></form>'


@pytest.fixture()
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:                       # no browser installed here
        pytest.skip(f"no Chromium: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    pw.stop()


def test_the_captcha_answer_field_is_found_by_its_own_signals_not_by_being_first(page):
    page.set_content(PAGE)
    info = find_captcha(page)
    assert info["input"] == "#c" and info["image"] == 'img[src="/x/captcha.png"]'
    assert info["reload"] == 'button[aria-label="Show me a different code"]' and "code" in info["label"]


def test_a_form_without_a_captcha_is_left_alone(page):
    page.set_content(NO_CAPTCHA)
    assert find_captcha(page) is None


def test_with_no_label_the_field_after_the_captcha_image_is_the_answer(page):
    page.set_content(IMAGE_ONLY)
    assert find_captcha(page)["input"] == 'input[name="b"]'


def test_every_other_visible_field_of_the_form_is_listed_with_its_label_and_options(page):
    page.set_content(FORM)
    fields = find_form_fields(page, find_captcha(page))
    assert [f["label"] for f in fields] == ["Name *", "Email *", "Country *"]        # not the code, not the hidden field
    country = fields[2]
    assert country["type"] == "select" and country["options"] == ["- Select Country -", "Albania", "Egypt"]
    assert country["placeholder"] == "- Select Country -" and fields[0]["required"] is True


def test_empty_fields_get_a_plausible_default_so_the_whole_form_is_populated():
    assert default_value({"type": "email", "label": "Email *", "current": ""}) == "qa-test@example.com"
    assert default_value({"type": "text", "label": "Mobile *", "current": ""}) == "0500000000"
    assert default_value({"type": "text", "label": "Name *", "current": ""}) == "Test"
    assert default_value({"type": "text", "label": "Name *", "current": "Sam"}) == "Sam"          # a filled field keeps its value
    country = {"type": "select", "current": "- Select Country -", "placeholder": "- Select Country -",
               "options": ["- Select Country -", "Albania", "Egypt"]}
    assert default_value(country) == "Albania"                                                    # the first real option, not the placeholder


def test_only_the_forms_submit_button_asks_for_a_code_and_a_filled_code_is_not_asked_twice(page, monkeypatch):
    page.set_content(PAGE)
    monkeypatch.setattr(humanstep, "can_ask", lambda: (True, "test"))
    asked = []

    def asker(request, get_image, reload):
        asked.append(request.label)
        return HumanAnswer("k7x2", {})

    assert solve_before_submit(page, page.locator('button[aria-label="Show me a different code"]'), asker=asker) == "none"
    assert solve_before_submit(page, page.locator("#go"), asker=asker) == "filled"
    assert page.locator("#c").input_value() == "k7x2" and len(asked) == 1
    assert solve_before_submit(page, page.locator("#go"), asker=asker) == "none"        # already answered


def test_the_whole_form_is_filled_from_what_the_person_confirmed(page, monkeypatch):
    page.set_content(FORM)
    monkeypatch.setattr(humanstep, "can_ask", lambda: (True, "test"))
    seen = {}

    def asker(request, get_image, reload):
        seen["labels"] = [f["label"] for f in request.fields]
        return HumanAnswer("zz9", {"#n": "Sam Test", "#e": "sam@example.com", "#k": "Egypt"})

    assert solve_before_submit(page, page.locator("#go"), asker=asker) == "filled"
    assert seen["labels"] == ["Name *", "Email *", "Country *"]
    assert page.locator("#n").input_value() == "Sam Test" and page.locator("#e").input_value() == "sam@example.com"
    assert page.locator("#k").input_value() == "Egypt" or page.locator("#k option:checked").inner_text() == "Egypt"
    assert page.locator("#c").input_value() == "zz9"


def test_skipping_or_a_run_that_cannot_ask_leaves_the_field_empty(page, monkeypatch):
    page.set_content(PAGE)
    monkeypatch.setattr(humanstep, "can_ask", lambda: (True, "test"))
    assert solve_before_submit(page, page.locator("#go"), asker=lambda *a: None) == "skipped"
    assert page.locator("#c").input_value() == ""
    monkeypatch.setattr(humanstep, "can_ask", lambda: (False, "no display"))
    assert solve_before_submit(page, page.locator("#go"), asker=lambda *a: HumanAnswer("never used")) == "not-asked"


def test_asking_is_automatic_when_a_person_is_there_and_never_when_turned_off_or_unattended(monkeypatch):
    from website_test_pipeline import authpopup
    monkeypatch.delenv("WTP_HUMAN", raising=False)
    humanstep.reset_declined()
    assert human_mode() == "ask"                                     # the default: no switch to flip
    monkeypatch.setattr(authpopup, "is_interactive", lambda: True)
    assert can_ask()[0] is True
    monkeypatch.setattr(authpopup, "is_interactive", lambda: False)
    assert can_ask()[0] is False                                     # unattended: never
    monkeypatch.setattr(authpopup, "is_interactive", lambda: True)
    monkeypatch.setenv("WTP_HUMAN", "skip")
    assert human_mode() == "skip" and can_ask()[0] is False          # --no-ask-human


def test_after_one_skip_the_window_is_not_shown_again_in_the_same_run(page, monkeypatch):
    from website_test_pipeline import authpopup
    monkeypatch.delenv("WTP_HUMAN", raising=False)
    monkeypatch.setattr(authpopup, "is_interactive", lambda: True)
    humanstep.reset_declined()
    page.set_content(PAGE)
    shown = []
    assert solve_before_submit(page, page.locator("#go"), asker=lambda *a: shown.append(1)) == "skipped"
    assert solve_before_submit(page, page.locator("#go"), asker=lambda *a: shown.append(1)) == "not-asked"
    assert len(shown) == 1                                           # asked once, then left alone
    humanstep.reset_declined()


def _tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.withdraw()
    return root


FIELDS = [{"selector": "#n", "label": "Name *", "type": "text", "required": True, "current": ""},
          {"selector": "#k", "label": "Country *", "type": "select", "required": True, "current": "- Select Country -",
           "placeholder": "- Select Country -", "options": ["- Select Country -", "Albania", "Egypt"]}]


def test_the_window_shows_every_field_prefilled_and_returns_them_with_the_code():
    root = _tk_root()
    try:
        popup = CodePopup(root, HumanRequest("https://x.test/subscribe", "What code is in the image?", fields=FIELDS), None, None, timeout_s=60)
        assert popup.values() == {"#n": "Test", "#k": "Albania"}                      # populated, not blank
        popup.widgets["#n"][1].delete(0, "end")
        popup.widgets["#n"][1].insert(0, "Sam")
        popup.widgets["#k"][1].set("Egypt")
        popup.entry.insert(0, "  Ab12 ")
        popup.submit()
        assert popup.result == HumanAnswer("Ab12", {"#n": "Sam", "#k": "Egypt"})
    finally:
        root.destroy()


def test_no_code_typed_skipping_and_timing_out_all_mean_no_answer():
    root = _tk_root()
    try:
        popup = CodePopup(root, HumanRequest("https://x.test", "code", fields=FIELDS), None, None, timeout_s=60)
        popup.submit()                                   # Continue with the code box empty
        assert popup.result is None
        popup2 = CodePopup(root, HumanRequest("https://x.test", "code"), None, None, timeout_s=0)
        assert popup2.result is None                     # timeout_s=0 skipped on the first tick
    finally:
        root.destroy()
