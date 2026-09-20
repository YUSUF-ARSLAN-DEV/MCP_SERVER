import pytest

from website_test_pipeline.pageutils import OPEN_MENU_SEL, pick_option


class _Loc:
    def __init__(self, page, name, count=1):
        self.page, self.name, self._count = page, name, count
        self.first = self

    def count(self):
        return self._count

    def locator(self, selector):
        return _Loc(self.page, f"option:{selector}", self.page.option_count)

    def filter(self, has_text):
        self.page.log.append(("filter", has_text))
        return self

    def click(self, timeout=None):
        self.page.log.append(("click", self.name))
        if self.name.startswith("option:"):
            self.page.menu_open = False       # picking an option closes the menu

    def select_option(self, label=None, timeout=None):
        self.page.log.append(("select_option", label))


class _Page:
    def __init__(self, option_count=1, native_count=0):
        self.option_count, self.native_count, self.log, self.menu_open = option_count, native_count, [], True

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
