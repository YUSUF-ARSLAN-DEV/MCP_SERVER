"""Deterministic healing of a flow step whose control can no longer be found.

Sites change: an id is regenerated, a button is relabelled. The flow then fails with "control not
found" although the journey still exists. Here the page as it is NOW is searched for the one control
the step must have meant, with no model involved:

  1. the same name under a different selector (the id moved), or
  2. a very similar name (the label was reworded), and in both cases exactly one such control.

Zero or several candidates means no healing: the flow fails as before, now with a hint, and is never
"fixed" by a guess. A healed step only replaces the stored one after the whole flow has run and passed
with it (runner.apply_result), the old step stays in the flow's heal_history, and a flow a person
approved or rejected is never changed at all - it gets the hint instead.
"""
from __future__ import annotations
from difflib import SequenceMatcher

MIN_SIMILARITY = 0.75        # how alike two names must be (0-1) to count as "the same control, reworded"
_NAME_CUT = 40               # flows store control names cut to 40 characters
_KIND_TAGS = {"select": {"select"}, "fill": {"input", "textarea"}}


def name_key(text: str) -> str:
    """A name compared ignoring case, spacing and punctuation (unicode aware)."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _usable(control: dict, kind: str | None) -> bool:
    if control.get("hidden") or control.get("volatile_id") or not control.get("name"):
        return False
    tags = _KIND_TAGS.get(kind or "")
    return tags is None or control.get("tag") in tags


def _same_name(control: dict, want: str) -> bool:
    key = name_key(control.get("name"))
    return bool(key) and (key == want or key.startswith(want) or (len(key) >= _NAME_CUT and want.startswith(key[:len(want)])))


def _fields(control: dict) -> dict:
    return {"selector": control.get("selector"), "name": (control.get("name") or "")[:_NAME_CUT]}


def _unique(controls: list[dict]) -> dict | None:
    """The single control the list amounts to, or None if it is empty or holds different controls."""
    distinct = {(c.get("selector"), name_key(c.get("name"))): c for c in controls}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def find_replacement(step: dict, controls: list[dict]) -> tuple[dict | None, str]:
    """(fields to change on the step, how) when exactly one control on the page can be what the step meant,
    else (None, why not)."""
    want = name_key(step.get("name"))
    if not want:
        return None, "the step has no name to look for"
    pool = [c for c in controls if _usable(c, step.get("kind"))]

    same = [c for c in pool if _same_name(c, want)]
    if same:
        found = _unique(same)
        if found is None:
            return None, "several controls have that name: " + ", ".join(sorted({(c.get("selector") or c["name"])[:30] for c in same}))
        old = step.get("selector")
        how = ("the control is still there under a new selector" if old and found.get("selector") != old
               else "the control is there but was not found by its role; using it directly")
        return _fields(found), how

    scored = [(SequenceMatcher(None, want, name_key(c["name"])).ratio(), c) for c in pool]
    close = [c for score, c in scored if score >= MIN_SIMILARITY]
    if not close:
        return None, "no control on the page has that name or a similar one"
    found = _unique(close)
    if found is None:
        return None, "several controls look similar: " + ", ".join(sorted({c["name"][:30] for c in close}))
    return _fields(found), f'the control was renamed (now "{found["name"][:_NAME_CUT]}")'


def describe(fields: dict) -> str:
    return f'{fields.get("selector") or "by name"} "{fields.get("name", "")}"'
