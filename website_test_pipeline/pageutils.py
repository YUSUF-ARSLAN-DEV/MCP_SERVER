"""Shared page helpers used by both the explorer and the generated specs."""

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
