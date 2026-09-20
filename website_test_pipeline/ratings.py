"""flow_ratings.json: an append-only history of evaluations per flow, so a flow's
standing comes from recorded evidence (who judged it, with what model and prompt,
and why) rather than a single overwritten opinion. Entries marked source=human are
written by a person and are never touched by the tool.
"""
from __future__ import annotations
import json
from pathlib import Path

RATINGS_VERSION = 1


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
