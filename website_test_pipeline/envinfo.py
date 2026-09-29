"""Which browser build the tests ran on, recorded next to the results (artifacts/run_env.json).

A bug cannot be reproduced without the exact browser build, so the report needs it. The build is read from a real
browser launch (`browser.version`) - the same Playwright install pytest uses - instead of being guessed.
Nothing here raises: a machine where the browser cannot start gets an empty record and the report says NOT CAPTURED.
"""
from __future__ import annotations
import json
from pathlib import Path

FILE = "run_env.json"


def load_browser_info(artifacts_dir: Path) -> dict:
    try:
        data = json.loads((Path(artifacts_dir) / FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def capture_browser(artifacts_dir: Path, browser: str = "chromium", device: str = "") -> dict:
    """Launch `browser` once, record its name, version and the emulated device (if any); returns the record."""
    info: dict = {"browser": browser, "device": device}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            launched = getattr(pw, browser).launch(headless=True)
            try:
                info["version"] = launched.version
            finally:
                launched.close()
    except Exception:
        return {}
    try:
        (Path(artifacts_dir) / FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
    except OSError:
        pass
    return info
