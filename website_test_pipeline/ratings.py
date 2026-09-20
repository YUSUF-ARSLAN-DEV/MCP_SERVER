"""flow_ratings.json: an append-only history of evaluations per flow, so a flow's
standing comes from recorded evidence (who judged it, with what model and prompt,
and why) rather than a single overwritten opinion. Entries marked source=human are
written by a person and are never touched by the tool.
"""
from __future__ import annotations
import json
from pathlib import Path

from .flows import HUMAN_STATUSES

RATINGS_VERSION = 1
STALE_AFTER = 2                        # consecutive failed executions before a flow is called stale
_EXECUTIONS = {"runner", "pytest"}     # evidence that comes from really running the flow


class RatingsFileError(Exception):
    """The ratings file exists but cannot be read - never overwrite it silently."""


def load_ratings(path: Path) -> dict:
    if not path.exists():
        return {"version": RATINGS_VERSION, "ratings": {}}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RatingsFileError(f"{path} is not valid JSON ({exc}); fix or delete it") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("ratings"), dict):
        raise RatingsFileError(f"{path} has no 'ratings' object")
    return doc


def append_rating(doc: dict, flow_id: str, entry: dict) -> None:
    doc["ratings"].setdefault(flow_id, []).append(entry)


def save_ratings(path: Path, doc: dict) -> None:
    doc["version"] = RATINGS_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def derive_status(current: str | None, entries: list[dict]) -> str | None:
    """A flow's status from its recorded executions (the runner and its generated test).
    Latest evidence wins; one failure is tolerated (a flaky network is not a broken flow),
    STALE_AFTER in a row is not. Human decisions are never changed, and model ratings
    are opinions, not executions, so they are ignored here."""
    if current in HUMAN_STATUSES:
        return current
    runs = [e for e in entries if e.get("source") in _EXECUTIONS]
    if not runs:
        return current
    if runs[-1].get("passed"):
        return "verified"
    failing = 0
    for entry in reversed(runs):
        if entry.get("passed"):
            break
        failing += 1
    if failing >= STALE_AFTER:
        return "stale" if any(e.get("passed") for e in runs) else "candidate"
    return current if current in {"verified", "stale"} else "candidate"
