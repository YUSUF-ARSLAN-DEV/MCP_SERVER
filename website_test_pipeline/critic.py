"""Model critic: rates each proposed flow so incoherent ones never reach flows.json.

Code can prove a control exists; it cannot tell whether a sequence of real controls
makes sense as one user journey. The critic scores that, on fixed dimensions, with a
reason. Ratings are recorded in flow_ratings.json with the model and prompt version.
"""
from __future__ import annotations
import json
import re

from .flows import describe_step

PROMPT_VERSION = "critic-v1"
MIN_COHERENCE = 3
_DIMS = ("coherence", "importance", "outcome_strength")

SYSTEM = "Return exactly one JSON object and no prose. Judge only from the site map and the flows given."

RULES = (
    "You are a strict QA reviewer. Rate each numbered flow from 1 (poor) to 5 (excellent) on:\n"
    "- coherence: do the steps form ONE sensible user journey on this site? Score 1-2 if unrelated "
    "controls are glued together (for example a channel picker followed by an unrelated wizard button), "
    "if a step does nothing toward the goal, or if the goal does not match the steps.\n"
    "- importance: is this a core thing visitors come to this site to do?\n"
    "- outcome_strength: does the stated outcome actually prove the goal was met, or only that the URL "
    "changed or a menu opened?\n"
    "Give a one-sentence reason. Use only the SITE MAP as evidence.\n"
    'Output: {"ratings":[{"n":1,"coherence":4,"importance":5,"outcome_strength":2,"reason":"..."}]}'
)


def prompt_for(flows: list[dict], site_map_text: str) -> str:
    lines = []
    for n, f in enumerate(flows, 1):
        steps = "; ".join(describe_step(s) for s in f["steps"])
        lines.append(f'{n}. goal: {f["goal"]}\n   start: {f["start_url"]}\n   steps: {steps}\n'
                     f'   outcome: {f["outcome"]}\n   evidence given: {f.get("evidence", "")}')
    return f"{RULES}\n\n{site_map_text}\n\nFLOWS TO RATE\n" + "\n".join(lines)


def parse_ratings(raw: str, count: int) -> dict[int, dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in critic response")
    data = json.loads(text[start:end + 1])
    rows = data.get("ratings") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("critic response has no 'ratings' list")
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        n = row.get("n")
        scores = {d: row.get(d) for d in _DIMS}
        if (isinstance(n, int) and 1 <= n <= count
                and all(isinstance(v, int) and 1 <= v <= 5 for v in scores.values())):
            out[n] = {"scores": scores, "reason": str(row.get("reason") or "")[:300]}
    return out


def rate_flows(client, flows: list[dict], site_map_text: str, model: str, now: str) -> dict[int, dict]:
    """{1-based flow number: rating entry}. Flows the critic skipped or mis-scored are absent."""
    parsed = parse_ratings(client.generate(prompt_for(flows, site_map_text), SYSTEM), len(flows))
    return {n: {"source": "model", "model": model, "prompt_version": PROMPT_VERSION, "at": now, **r}
            for n, r in parsed.items()}
