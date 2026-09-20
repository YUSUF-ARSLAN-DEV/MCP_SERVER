"""The per-site flows file (runs/<site>/flows.json): user journeys the pipeline
knows about, whether the explorer observed them, a model proposed them, or a
human wrote them. Explore writes the flows it verified; a human can edit the
file (goal text, status) and re-exploring never overwrites those edits.

status: candidate (seen but not confirmed) | verified (a probe ran it and saw an
outcome) | stale (was verified, then failed repeatedly - re-run verify) |
approved / rejected (a human decided - never changed by the tool).
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path

FLOWS_VERSION = 1
HUMAN_STATUSES = {"approved", "rejected"}
_OBSERVED = {"results", "navigates"}


class FlowsFileError(Exception):
    """The flows file exists but cannot be read - never overwrite it silently."""


def _slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit] or "flow"


def flow_id(start_url: str, action: str) -> str:
    path = re.sub(r"^https?://", "", start_url or "")
    return f"{_slug(path, 50)}--{_slug(action, 30)}"


def _target(step: dict) -> str:
    if step.get("selector"):
        return step["selector"]
    return f'"{(step.get("name") or "control")[:40]}"'


def describe_step(step: dict) -> str:
    kind, value = step.get("kind"), step.get("value")
    if kind == "select":
        return f'select "{value}" in {_target(step)}' if value else f'select the first real option in {_target(step)}'
    if kind == "fill":
        return f'fill "{value}" in {_target(step)}' if value else f'fill {_target(step)}'
    if kind == "multiselect":
        return f'pick "{value}" in {_target(step)}' if value else f'pick an option in {_target(step)}'
    if kind == "submit":
        return f'{value} in {_target(step)}' if value == "press Enter" else f'click {_target(step)}'
    if kind == "click":
        return f'click {_target(step)}'
    return f'{kind} {_target(step)}'


def flow_from_primary(start_url: str, primary: dict | None, now: str | None = None) -> dict | None:
    """Turn the explorer's primary-flow probe result into a flow entry."""
    if not primary or not primary.get("steps"):
        return None
    steps = [{**s, "name": (s.get("name") or "")[:40]} for s in primary["steps"]]  # a <select>'s name is every option concatenated
    action = primary.get("action") or "action"
    if not any(s.get("kind") == "submit" for s in steps):
        steps.append({"kind": "click", "selector": primary.get("action_selector"), "name": action})
    outcome = {k: primary[k] for k in
               ("effect", "to", "results_selector", "results_role", "row_count") if primary.get(k) is not None}
    effect = outcome.get("effect")
    return {
        "id": flow_id(start_url, action),
        "goal": f'{action}: ' + "; ".join(describe_step(s) for s in steps),
        "source": "explorer",
        "status": "verified" if effect in _OBSERVED else "candidate",
        "start_url": start_url,
        "steps": steps,
        "outcome": outcome,
        "observed_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def is_blocked(flow: dict) -> bool:
    """A flow whose plain sentence was edited or dropped since it was built (intent_state, set by
    intents.sync_flows). It is neither verified nor turned into a test until it is rebuilt from the
    sentence; a person's own decision on the flow always wins."""
    return bool(flow.get("intent_state")) and flow.get("status") not in HUMAN_STATUSES


def load_flows(path: Path) -> dict:
    if not path.exists():
        return {"version": FLOWS_VERSION, "flows": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FlowsFileError(f"{path} is not valid JSON ({exc}); fix or delete it") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("flows"), list):
        raise FlowsFileError(f"{path} has no 'flows' list")
    return doc


def merge_flow(doc: dict, flow: dict) -> str:
    """Insert or refresh `flow`; returns 'added', 'updated' or 'kept' (human-decided)."""
    for i, existing in enumerate(doc["flows"]):
        if existing.get("id") != flow["id"]:
            continue
        if existing.get("status") in HUMAN_STATUSES:
            return "kept"
        doc["flows"][i] = flow
        return "updated"
    doc["flows"].append(flow)
    return "added"


def save_flows(path: Path, doc: dict) -> None:
    doc["version"] = FLOWS_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def record_flow(path: Path, start_url: str, primary: dict | None, log=None) -> dict | None:
    flow = flow_from_primary(start_url, primary)
    if flow is None:
        return None
    doc = load_flows(path)
    result = merge_flow(doc, flow)
    save_flows(path, doc)
    if log:
        log.info("flows: %s %s (%s)", result, flow["id"], flow["status"])
    return flow
