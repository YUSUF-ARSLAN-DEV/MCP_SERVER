from pathlib import Path
import re

def action_evidence(page, test_name: str, action, verify, directory: Path) -> Path:
    # P7: "found but not interactable" (element exists, but a click lands before it settles - a transition,
    # a re-render, a moment where it is briefly covered) is a readiness problem, not a real failure - one
    # retry after a short pause resolves it far more often than it masks a genuine defect. Confirmed live on
    # a wizard's "Next" input failing intermittently on one executor and not the other for exactly this reason.
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    try:
        action()
    except PlaywrightTimeoutError:
        page.wait_for_timeout(500)
        action()
    verify()
    try:
        from .pageutils import wait_for_content, wait_for_loaders
        wait_for_loaders(page, 3000)   # a spinner in the screenshot means the content had not arrived
        wait_for_content(page, 1500)   # a client-rendered page can still be blank right after a navigation step
    except Exception:
        pass
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", test_name).strip("-") or "evidence"
    path = directory / f"{safe_name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path

def observation_evidence(page, test_name: str, verify, directory: Path) -> Path:
    verify(); return action_evidence(page, test_name, lambda: None, lambda: None, directory)
