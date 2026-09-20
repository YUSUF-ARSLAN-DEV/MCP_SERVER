"""Shared page helpers used by both the explorer and the generated specs."""
import json

from . import heuristics

# Accept / dismiss button accessible names across common consent + popup frameworks.
_ACCEPT_NAMES = (
    "Allow all", "Accept all", "Accept All Cookies", "Accept all cookies",
    "I Accept", "I agree", "Agree", "Got it", "OK",
)
_CLOSE_NAMES = (
    "Close Ad", "Close ad", "Close", "No thanks", "No Thanks", "Not now",
    "Not Now", "Dismiss", "Maybe later", "Skip",
)
_BUTTON_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "button[aria-label='Accept all']",
    "[aria-label*='close' i]",
    "[class*='close' i][role='button']",
)
_CONTAINERS = (
    "#onetrust-banner-sdk", "#onetrust-consent-sdk",
    "[id*='cookie-banner' i]", "[aria-label='Cookie banner']",
)


def settle_page(page, timeout: int = 8000) -> None:
    """Give a page a bounded chance to reach the 'load' state.

    Ad- and tracker-heavy sites keep firing requests long after the content is
    usable, so the 'load' event can be minutes away or never arrive. We wait a
    short, capped interval and move on - the auto-retrying ``expect`` calls in
    the tests absorb any remaining async rendering.
    """
    try:
        page.wait_for_load_state("load", timeout=timeout)
    except Exception:
        pass


def prime_lazy_content(page) -> None:
    """Scroll the page top-to-bottom and back so IntersectionObserver-driven
    content (footers, newsletter widgets, lazy sections) actually mounts.

    Without this the footer / subscribe form is often absent from the DOM at
    snapshot time and at assert time, and tests for it can only guess.
    """
    try:
        page.evaluate(
            """async () => {
                const step = Math.max(600, window.innerHeight);
                for (let y = 0; y < document.body.scrollHeight; y += step) {
                    window.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, 120));
                }
                window.scrollTo(0, 0);
                await new Promise(r => setTimeout(r, 250));
            }"""
        )
    except Exception:
        pass


def open_page(page, url: str) -> None:
    """Navigate, let the SPA settle, clear overlays, and mount lazy content."""
    page.goto(url, wait_until="domcontentloaded")
    settle_page(page)
    dismiss_overlays(page)
    prime_lazy_content(page)


def dismiss_overlays(page, rounds: int = 3) -> bool:
    """Best-effort close of consent dialogs, ad interstitials and popups.

    Runs a few rounds because sites stack layers (ad over cookie dialog, etc.).
    Returns True if anything was clicked.
    """
    acted = False
    for _ in range(rounds):
        hit = _click_first(page, _BUTTON_SELECTORS, by="css")
        hit = _click_first(page, _ACCEPT_NAMES, by="button") or hit
        hit = _click_first(page, _CLOSE_NAMES, by="button") or hit
        if not hit:
            break
        acted = True
    _wait_gone(page)
    return acted


# kept for existing imports
dismiss_consent = dismiss_overlays


def _click_first(page, candidates, *, by: str) -> bool:
    for candidate in candidates:
        locator = page.locator(candidate) if by == "css" else page.get_by_role("button", name=candidate, exact=True)
        try:
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=1200)
                page.wait_for_timeout(300)
                return True
        except Exception:
            continue
    return False


def _wait_gone(page) -> None:
    for selector in _CONTAINERS:
        try:
            container = page.locator(selector)
            if container.count():
                container.first.wait_for(state="hidden", timeout=1500)
        except Exception:
            pass


def menu_selector() -> str:
    """CSS for any open, visible dropdown / menu container (see heuristics.py; extendable per site)."""
    return heuristics.menu_selector()


OPEN_MENU_SEL = menu_selector()   # the default value, kept for importers; code calls menu_selector() at use time


def close_menus(page, trigger=None) -> None:
    """Escape, then re-click the trigger, then click a page corner, until no widget
    menu is left open. An open jQuery-UI multiselect menu intercepts the next click,
    which is why the Search click timed out after a channel was picked.

    A step that fails (typically the trigger is itself covered by the open menu) must not
    end the loop: the corner click after it is what finally closes the menu."""
    for attempt in range(4):
        try:
            if not page.locator(menu_selector()).count():
                return
        except Exception:
            return
        try:
            if attempt == 0:
                page.keyboard.press("Escape")
            elif attempt == 1 and trigger is not None:
                trigger.click(timeout=1000)
            else:
                page.mouse.click(2, 2)
            page.wait_for_timeout(300)
        except Exception:
            continue


def _menu_for(page, trigger):
    """The menu this trigger opened: the element it points to with aria-controls / aria-owns when it says
    so (the standard way widget libraries link a button to its menu), else the first open menu container."""
    try:
        target = trigger.get_attribute("aria-controls") or trigger.get_attribute("aria-owns")
        if target:
            owned = page.locator('[id=' + json.dumps(target.split()[0]) + ']')
            if owned.count():
                return owned.first
    except Exception:
        pass
    return page.locator(menu_selector()).first


def pick_option(page, trigger, text: str) -> None:
    """Open a dropdown / checkbox-menu widget and pick the option whose text contains `text`.

    Used by both the flow runner and the generated flow specs, so a spec does exactly what
    the run that verified the flow did. Falls back to the widget's underlying <select multiple>
    when the menu will not open on a synthetic click. Raises if the option does not exist."""
    trigger.click(timeout=3000)
    page.wait_for_timeout(700)
    option = _menu_for(page, trigger).locator(heuristics.option_selector()).filter(has_text=text).first
    if option.count():
        option.click(timeout=2000)
        close_menus(page, trigger)
        return
    close_menus(page, trigger)
    native = page.locator("select[multiple]").first
    if native.count():
        native.select_option(label=text, timeout=2000)
        return
    raise RuntimeError(f'option "{text}" not found in the opened menu')


def _loader_js() -> str:
    """JS that is true while any visible loading indicator exists (class hints from heuristics.py, aria-busy,
    role=progressbar)."""
    return ("() => [...document.querySelectorAll(" + json.dumps(heuristics.loader_selector()) + ")].some(e => {"
            " const r = e.getBoundingClientRect(), s = getComputedStyle(e);"
            " return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0'; })")


def wait_for_loaders(page, timeout_ms: int = 4000, poll_ms: int = 250) -> bool:
    """Wait until no visible loading spinner is left (up to timeout_ms). Returns True if the page is idle.
    A screenshot or snapshot taken while a spinner still shows records the page BEFORE its content arrived.
    Cheap when nothing is loading (one check), and never raises: an odd page just stops the wait."""
    waited = 0
    try:
        while page.evaluate(_loader_js()):
            if waited >= timeout_ms:
                return False
            page.wait_for_timeout(poll_ms)
            waited += poll_ms
    except Exception:
        return True
    return True
