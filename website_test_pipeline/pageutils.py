"""Shared page helpers used by both the explorer and the generated specs."""

# Accept-button accessible names seen across common consent frameworks.
_CONSENT_NAMES = (
    "Allow all", "Accept all", "Accept All Cookies", "Accept all cookies",
    "I Accept", "I agree", "Agree", "Got it", "OK", "Continue",
)
# Known container selectors that should detach/hide once consent is handled.
_CONSENT_CONTAINERS = ("#onetrust-banner-sdk", "#onetrust-consent-sdk", "[id*='cookie-banner']", "[aria-label='Cookie banner']")
_CONSENT_BUTTONS = ("#onetrust-accept-btn-handler", "button[aria-label='Accept all']")


def dismiss_consent(page, timeout: int = 4000) -> bool:
    """Best-effort dismissal of a cookie/consent overlay. Returns True if one was closed."""
    for selector in _CONSENT_BUTTONS:
        button = page.locator(selector)
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=timeout)
                _wait_gone(page)
                return True
        except Exception:
            pass
    for name in _CONSENT_NAMES:
        button = page.get_by_role("button", name=name, exact=True)
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=timeout)
                _wait_gone(page)
                return True
        except Exception:
            pass
    return False


def _wait_gone(page) -> None:
    for selector in _CONSENT_CONTAINERS:
        container = page.locator(selector)
        try:
            if container.count():
                container.first.wait_for(state="hidden", timeout=3000)
        except Exception:
            pass
