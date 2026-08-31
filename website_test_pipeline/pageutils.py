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
                locator.first.click(timeout=2500)
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
                container.first.wait_for(state="hidden", timeout=2500)
        except Exception:
            pass
