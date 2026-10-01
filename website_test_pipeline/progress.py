"""Progress reporting for the pop-up window (progress_window.py).

The pipeline writes runs/<site>/artifacts/progress.json; the window only reads it, so closing the window never
affects a run. `all` gives each of its steps a slice of the overall bar (WTP_PROGRESS_SPAN="start,end") and the
time it began (WTP_PROGRESS_START); a step run on its own uses the whole bar. Reporting never raises.
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

_STARTED = time.time()


def progress_file(settings) -> Path:
    return Path(settings.artifacts_dir) / "progress.json"


def window_available() -> bool:
    """Can the pop-up open here? False on a server: no tkinter in the image, or no display on Linux."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def _span() -> tuple[float, float]:
    try:
        a, b = (float(x) for x in os.environ.get("WTP_PROGRESS_SPAN", "0,1").split(","))
        return a, b
    except ValueError:
        return 0.0, 1.0


def _write(settings, data: dict) -> None:
    path = progress_file(settings)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)                     # the window never reads a half-written file
    # the same state as one line of history, for a web UI that streams progress (appended, never rewritten)
    with path.with_suffix(".jsonl").open("a", encoding="utf-8") as events:
        events.write(json.dumps(data) + "\n")


def report(settings, phase: str, done: int = 0, total: int = 0, note: str = "") -> None:
    """Say what the run is doing now. `done`/`total` are progress within the current step (0/0 = unknown)."""
    try:
        start, end = _span()
        within = min(1.0, done / total) if total else 0.0
        _write(settings, {
            "started": float(os.environ.get("WTP_PROGRESS_START") or _STARTED),
            "updated": time.time(), "phase": phase, "note": note, "done": done, "total": total,
            "fraction": start + (end - start) * within, "finished": False,
        })
    except Exception:
        pass


def finish(settings, phase: str = "Done") -> None:
    try:
        _write(settings, {
            "started": float(os.environ.get("WTP_PROGRESS_START") or _STARTED),
            "updated": time.time(), "phase": phase, "note": "", "done": 0, "total": 0,
            "fraction": 1.0, "finished": True,
        })
    except Exception:
        pass
