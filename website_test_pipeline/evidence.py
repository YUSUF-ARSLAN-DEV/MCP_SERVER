from pathlib import Path
import re

def action_evidence(page, test_name: str, action, verify, directory: Path) -> Path:
    action(); verify()
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
