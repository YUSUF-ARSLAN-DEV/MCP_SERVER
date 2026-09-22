import pytest

from website_test_pipeline.pageutils import OPEN_MENU_SEL, pick_option


class _Loc:
    def __init__(self, page, name, count=1):
        self.page, self.name, self._count = page, name, count
        self.first = self

    def count(self):
        return self._count

    def locator(self, selector):
        if self.name.startswith("option:") and "input[type" in selector and self.page.checked is not None:
            return _CheckState(self.page, self.page.checked)
        return _Loc(self.page, f"option:{selector}", self.page.option_count)

    def filter(self, has_text):
        self.page.log.append(("filter", has_text))
        return self

    def click(self, timeout=None):
        self.page.log.append(("click", self.name))
        if self.name.startswith("option:"):
            self.page.picked = True           # a real widget's box is now checked (until close_menus runs)
            self.page.menu_open = False       # picking an option closes the menu

    def select_option(self, label=None, timeout=None):
        self.page.log.append(("select_option", label))


class _CheckState:
    """The option element itself, when the fake widget's checked state is known - so is_checked() can be
    tested directly on `option`, matching _confirm_checked's first candidate before its input[] fallback."""
    def __init__(self, page, checked):
        self.page, self._checked, self.first = page, checked, self

    def count(self):
        return 1

    def is_checked(self, timeout=None):
        self.page.log.append(("is_checked", self._checked))
        return self._checked


class _Page:
    def __init__(self, option_count=1, native_count=0, checked=None):
        self.option_count, self.native_count, self.checked = option_count, native_count, checked
        self.log, self.picked, self.menu_open = [], False, True

    def locator(self, selector):
        if selector == OPEN_MENU_SEL:
            return _Loc(self, "menu", 1 if self.menu_open else 0)
        if selector == "select[multiple]":
            return _Loc(self, "native", self.native_count)
        raise AssertionError(selector)

    def wait_for_timeout(self, ms):
        pass

    class keyboard:
        @staticmethod
        def press(key):
            pass


def test_pick_option_opens_the_widget_then_clicks_the_option_with_that_text():
    page = _Page()
    trigger = _Loc(page, "trigger")
    pick_option(page, trigger, "Al Jazeera 2")
    assert page.log[0] == ("click", "trigger")
    assert ("filter", "Al Jazeera 2") in page.log
    assert any(step[0] == "click" and step[1].startswith("option:") for step in page.log)


def test_pick_option_falls_back_to_the_native_multiple_select():
    page = _Page(option_count=0, native_count=1)
    pick_option(page, _Loc(page, "trigger"), "Al Jazeera 2")
    assert ("select_option", "Al Jazeera 2") in page.log


def test_pick_option_raises_when_the_option_exists_nowhere():
    page = _Page(option_count=0, native_count=0)
    with pytest.raises(RuntimeError, match='option "Nope" not found'):
        pick_option(page, _Loc(page, "trigger"), "Nope")


def test_pick_option_confirms_the_click_actually_checked_the_box():
    # real gap, found live: a click that silently does nothing (a dead handler) looked identical to a
    # working pick. When a checked state is confirmed False, that is now caught, not trusted.
    page = _Page(checked=True)
    pick_option(page, _Loc(page, "trigger"), "Al Jazeera 2")
    assert ("is_checked", True) in page.log


def test_pick_option_raises_when_the_click_did_not_actually_check_it():
    page = _Page(checked=False)
    with pytest.raises(RuntimeError, match='picking "Al Jazeera 2" did not check it'):
        pick_option(page, _Loc(page, "trigger"), "Al Jazeera 2")


def test_pick_option_never_guesses_when_no_checked_state_can_be_determined():
    # a custom div-based widget with no native input and no aria-checked: _confirm_checked already covers
    # this (the default _Page has no is_checked support), kept here as an explicit regression marker.
    page = _Page()
    pick_option(page, _Loc(page, "trigger"), "Al Jazeera 2")   # must not raise


# ------------------------------------------------------------------ waiting for spinners

class _BusyPage:
    def __init__(self, busy_polls):
        self.busy_polls, self.waited = busy_polls, 0

    def evaluate(self, script):
        busy = self.busy_polls > 0
        self.busy_polls -= 1
        return busy

    def wait_for_timeout(self, ms):
        self.waited += ms


def test_wait_for_loaders_returns_at_once_when_nothing_is_loading():
    from website_test_pipeline.pageutils import wait_for_loaders
    page = _BusyPage(0)
    assert wait_for_loaders(page) is True and page.waited == 0


def test_wait_for_loaders_waits_until_the_spinner_is_gone():
    from website_test_pipeline.pageutils import wait_for_loaders
    page = _BusyPage(3)
    assert wait_for_loaders(page, timeout_ms=4000, poll_ms=250) is True and page.waited == 750


def test_wait_for_loaders_gives_up_on_a_spinner_that_never_goes():
    from website_test_pipeline.pageutils import wait_for_loaders
    page = _BusyPage(10 ** 6)
    assert wait_for_loaders(page, timeout_ms=1000, poll_ms=250) is False and page.waited == 1000


def test_wait_for_loaders_never_raises_on_a_page_that_cannot_evaluate():
    from website_test_pipeline.pageutils import wait_for_loaders

    class _Broken:
        def evaluate(self, script): raise RuntimeError("closed")

    assert wait_for_loaders(_Broken()) is True


def test_the_evidence_screenshot_is_taken_after_the_page_stops_loading(tmp_path):
    from website_test_pipeline.evidence import action_evidence
    order = []

    class _Page:
        def evaluate(self, script):
            order.append("loader-check")
            return False

        def wait_for_timeout(self, ms): pass

        def screenshot(self, path, full_page): order.append("screenshot")

    action_evidence(_Page(), "01-x", lambda: order.append("action"), lambda: order.append("verify"), tmp_path)
    assert order == ["action", "verify", "loader-check", "screenshot"]
