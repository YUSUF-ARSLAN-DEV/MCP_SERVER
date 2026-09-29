import pytest

from website_test_pipeline import humanstep
from website_test_pipeline.humanstep import CodePopup, HumanRequest, can_ask, find_captcha, human_mode, solve_before_submit

PAGE = ('<form><input name="name" type="text"><img src="/x/captcha.png" alt="Image CAPTCHA">'
        '<button type="button" aria-label="Show me a different code">r</button>'
        '<label for="c">What code is in the image?</label><input id="c" name="captcha_response" type="text">'
        '<button type="submit" id="go">Send</button></form>')
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


def test_only_the_forms_submit_button_asks_for_a_code_and_a_filled_code_is_not_asked_twice(page, monkeypatch):
    page.set_content(PAGE)
    monkeypatch.setattr(humanstep, "can_ask", lambda: (True, "test"))
    asked = []

    def asker(request, get_image, reload):
        asked.append(request.label)
        return "k7x2"

    assert solve_before_submit(page, page.locator('button[aria-label="Show me a different code"]'), asker=asker) == "none"
    assert solve_before_submit(page, page.locator("#go"), asker=asker) == "filled"
    assert page.locator("#c").input_value() == "k7x2" and len(asked) == 1
    assert solve_before_submit(page, page.locator("#go"), asker=asker) == "none"        # already answered


def test_skipping_or_a_run_that_cannot_ask_leaves_the_field_empty(page, monkeypatch):
    page.set_content(PAGE)
    monkeypatch.setattr(humanstep, "can_ask", lambda: (True, "test"))
    assert solve_before_submit(page, page.locator("#go"), asker=lambda *a: None) == "skipped"
    assert page.locator("#c").input_value() == ""
    monkeypatch.setattr(humanstep, "can_ask", lambda: (False, "no display"))
    assert solve_before_submit(page, page.locator("#go"), asker=lambda *a: "never used") == "not-asked"


def test_asking_needs_the_opt_in(monkeypatch):
    monkeypatch.delenv("WTP_HUMAN", raising=False)
    assert human_mode() == "skip" and can_ask()[0] is False
    monkeypatch.setenv("WTP_HUMAN", "ASK")
    assert human_mode() == "ask"


def _tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception as exc:
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.withdraw()
    return root


def test_the_window_returns_the_typed_code_and_skipping_or_timing_out_returns_none():
    root = _tk_root()
    try:
        popup = CodePopup(root, HumanRequest("https://x.test/subscribe", "What code is in the image?"), None, None, timeout_s=60)
        popup.entry.insert(0, "  Ab12 ")
        popup.submit()
        assert popup.result == "Ab12"
        popup2 = CodePopup(root, HumanRequest("https://x.test", "code"), None, None, timeout_s=0)
        assert popup2.result is None                     # timeout_s=0 skipped on the first tick
    finally:
        root.destroy()
