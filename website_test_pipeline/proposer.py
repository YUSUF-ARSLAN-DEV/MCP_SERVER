"""Model-proposed flows: the model reads the site map (sitemap.py) and suggests user
journeys; code then rejects any flow that names a page, control or outcome the explorer
never observed. Accepted flows enter flows.json as status=candidate - only running one
(a later step) can make it verified.
"""
from __future__ import annotations
import json
import re

from .critic import MIN_COHERENCE, rate_flows
from .flows import FlowsFileError, flow_id, load_flows, merge_flow, describe_step, FLOWS_VERSION
from .ratings import RatingsFileError, append_rating, load_ratings, save_ratings
from .sitemap import build_site_map, load_inventories, render_site_map

PROMPT_VERSION = "propose-v1"
MAX_FLOWS = 6
MAX_STEPS = 8
MIN_STEPS = 2
_ACTIONS = {"click": "click", "select": "select", "fill": "fill", "pick": "multiselect"}
_OUTCOMES = {"navigates": "navigates", "shows_results": "results",
             "shows_validation": "validation", "reveals_panel": "reveals"}

SYSTEM = "Return exactly one JSON object and no prose. Propose only what the site map supports."

RULES = (
    "You are a QA analyst. From the SITE MAP below, propose up to %d user flows: journeys a real visitor "
    "takes to accomplish a goal (find information, search or filter, subscribe or contact, follow a path "
    "across pages). Each flow is %d-%d steps and ends in an observable outcome.\n"
    "Rules:\n"
    "- Use ONLY pages and controls listed in the SITE MAP. Copy control names exactly as written there.\n"
    "- Every step names the page it happens on (its path, e.g. /en/find).\n"
    "- Step actions: click | select | fill | pick (pick = choose an option in a dropdown or checkbox menu). "
    "select/fill/pick may carry a value; leave it out if unsure.\n"
    "- Outcomes: navigates (give to_path) | shows_results | shows_validation | reveals_panel.\n"
    "- Do not propose flows that need an account, payment, or a real person's data. Do not repeat a flow. "
    "Do not propose a flow for a page the map does not show.\n"
    "- evidence: one sentence citing the page and controls that make you believe the flow exists.\n"
    'Output: {"flows":[{"goal":"...","start_path":"/...","steps":[{"page":"/...","action":"click",'
    '"target":"<control name>","value":null}],"outcome":{"type":"navigates","to_path":"/..."},"evidence":"..."}]}'
) % (MAX_FLOWS, MIN_STEPS, MAX_STEPS)


def prompt_for(site_map_text: str) -> str:
    return f"{RULES}\n\n{site_map_text}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:40]


def parse_response(raw: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model response")
    data = json.loads(text[start:end + 1])
    flows = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(flows, list):
        raise ValueError("response has no 'flows' list")
    return [f for f in flows if isinstance(f, dict)]


def _page_key(path: str) -> str:
    return (path or "").split("?")[0].split("#")[0] or "/"


def _index(inventories: list[dict]) -> dict[str, dict]:
    """path -> {normalised name/selector -> control} across static and revealed controls."""
    index: dict[str, dict] = {}
    for inv in inventories:
        page = _page_key(re.sub(r"^https?://[^/]+", "", inv.get("url", "")))
        controls = list(inv.get("controls") or [])
        for r in inv.get("revealed") or []:
            controls += [c for c in (r.get("controls") or []) if c.get("name") or c.get("selector")]
            if r.get("trigger"):
                controls.append({"tag": "button", "name": r["trigger"]})
        by_name: dict[str, dict] = {}
        for c in controls:
            for key in (c.get("name"), c.get("selector")):
                if key:
                    by_name.setdefault(_norm(key), c)
        index[page] = by_name
    return index


def _check_step(step: dict, index: dict, pages: set[str]) -> tuple[dict | None, str]:
    page = _page_key(str(step.get("page") or ""))
    action = step.get("action")
    if page not in pages:
        return None, f"step page {page} was not explored"
    if action not in _ACTIONS:
        return None, f"unknown action {action!r}"
    control = index.get(page, {}).get(_norm(str(step.get("target") or "")))
    if control is None:
        return None, f'control "{step.get("target")}" not found on {page}'
    tag = control.get("tag")
    if action == "select" and tag != "select":
        return None, f'"{step.get("target")}" is not a select'
    if action == "fill" and tag not in {"input", "textarea"}:
        return None, f'"{step.get("target")}" is not a text field'
    value = step.get("value")
    options = [str(o) for o in (control.get("options") or [])]
    if action == "select" and value is not None and options and str(value) not in options:
        value = None  # unknown label: let the runner pick the first real option
    kind = "select" if tag == "select" else _ACTIONS[action]  # 'pick' on a real <select> is a select
    return {"page": page, "kind": kind, "selector": control.get("selector"),
            "name": (control.get("name") or str(step.get("target")))[:40], "value": value}, ""


def validate_flow(raw: dict, index: dict, pages: set[str],
                  linked: frozenset = frozenset()) -> tuple[list[dict] | None, dict | None, str]:
    """(steps, outcome, '') when every step and the outcome exist on the site; else (None, None, reason)."""
    goal, steps_in = str(raw.get("goal") or "").strip(), raw.get("steps")
    if not goal or not isinstance(steps_in, list) or not MIN_STEPS <= len(steps_in) <= MAX_STEPS:
        return None, None, "needs a goal and %d-%d steps (one click is not a journey)" % (MIN_STEPS, MAX_STEPS)
    if _page_key(str(raw.get("start_path") or "")) not in pages:
        return None, None, f'start page {raw.get("start_path")} was not explored'
    steps = []
    for s in steps_in:
        step, reason = _check_step(s if isinstance(s, dict) else {}, index, pages)
        if step is None:
            return None, None, reason
        steps.append(step)
    outcome_in = raw.get("outcome") if isinstance(raw.get("outcome"), dict) else {}
    effect = _OUTCOMES.get(outcome_in.get("type"))
    if effect is None:
        return None, None, f'unknown outcome {outcome_in.get("type")!r}'
    outcome = {"effect": effect}
    if effect == "navigates":
        to = _page_key(str(outcome_in.get("to_path") or ""))
        if to == _page_key(str(raw.get("start_path"))):
            return None, None, "outcome is the page the flow started on"
        if to not in pages and to not in linked:
            return None, None, f"outcome page {to} is neither explored nor linked from an explored page"
        outcome["to"] = to
    return steps, outcome, ""


def _signature(steps: list[dict]) -> tuple:
    """What a flow does, ignoring which page it starts from and the values it uses."""
    return tuple(("choose" if s.get("kind") in {"select", "multiselect"} else s.get("kind"),
                  _norm(s.get("selector") or s.get("name") or "")) for s in steps)


def propose(raw_flows: list[dict], inventories: list[dict], existing: list[dict],
            model: str = "", now: str = "") -> tuple[list[dict], list[tuple[str, str]]]:
    """Validate the model's flows. Returns (accepted flow entries, [(goal, reason)] rejected)."""
    index = _index(inventories)
    pages = set(index)
    linked = frozenset(_page_key(e["to"]) for e in build_site_map(inventories)["edges"])
    url_of = {_page_key(re.sub(r"^https?://[^/]+", "", i.get("url", ""))): i.get("url", "") for i in inventories}
    seen = {_signature(f.get("steps") or []) for f in existing}
    accepted, rejected = [], []
    for raw in raw_flows[:MAX_FLOWS]:
        goal = str(raw.get("goal") or "(no goal)")[:120]
        steps, outcome, reason = validate_flow(raw, index, pages, linked)
        if steps is None:
            rejected.append((goal, reason))
            continue
        sig = _signature(steps)
        if sig in seen:
            rejected.append((goal, "duplicate of an existing flow (same steps, any page)"))
            continue
        seen.add(sig)
        start = url_of[_page_key(str(raw.get("start_path")))]
        accepted.append({
            "id": flow_id(start, goal),
            "goal": goal,
            "source": "model",
            "status": "candidate",
            "start_url": start,
            "steps": steps,
            "outcome": outcome,
            "evidence": str(raw.get("evidence") or "")[:300],
            "proposed_by": {"model": model, "prompt_version": PROMPT_VERSION},
            "observed_at": now,
        })
    return accepted, rejected


def _apply_critic(flows: list[dict], client, site_map_text: str, settings, now: str, log) -> list[dict]:
    """Rate the flows, log every rating in flow_ratings.json, drop incoherent ones.
    If the critic or the ratings file fails, keep the flows unrated rather than lose them."""
    if not flows:
        return flows
    try:
        ratings = rate_flows(client, flows, site_map_text, settings.model, now)
        doc = load_ratings(settings.ratings_file)
    except (RatingsFileError, ValueError, Exception) as exc:
        log.warning("propose: critic unavailable, keeping %d flow(s) unrated (%s)", len(flows), exc)
        return flows
    kept = []
    for n, flow in enumerate(flows, 1):
        entry = ratings.get(n)
        if entry is None:
            log.info("propose: critic gave no usable rating for %s - kept unrated", flow["id"])
            kept.append(flow)
            continue
        entry["kept"] = entry["scores"]["coherence"] >= MIN_COHERENCE
        append_rating(doc, flow["id"], entry)
        if entry["kept"]:
            kept.append(flow)
        else:
            log.info("propose: critic dropped '%s' (coherence %d: %s)", flow["goal"],
                     entry["scores"]["coherence"], entry["reason"])
    save_ratings(settings.ratings_file, doc)
    return kept


def run_propose(settings, urls: list[str], client, log) -> int:
    from datetime import datetime, timezone
    wanted = set(urls)
    inventories = [i for i in load_inventories(settings.artifacts_dir) if i.get("url") in wanted]
    if not inventories:
        log.error("propose: no inventories for the URLs in %s - run explore first", settings.urls_file)
        return 2
    text = render_site_map(build_site_map(inventories))
    log.info("propose: site map of %d page(s), %d chars", len(inventories), len(text))
    try:
        raw_flows = parse_response(client.generate(prompt_for(text), SYSTEM))
    except Exception as exc:
        log.error("propose: model response unusable (%s)", exc)
        return 1
    try:
        doc = load_flows(settings.flows_file)
    except FlowsFileError as exc:
        log.error("propose: %s", exc)
        return 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    accepted, rejected = propose(raw_flows, inventories, doc["flows"], settings.model, now)
    accepted = _apply_critic(accepted, client, text, settings, now, log)
    for flow in accepted:
        merge_flow(doc, flow)
        log.info("propose: accepted %s :: %s", flow["id"], " -> ".join(describe_step(s) for s in flow["steps"]))
    for goal, reason in rejected:
        log.info("propose: rejected '%s' (%s)", goal, reason)
    doc["version"] = FLOWS_VERSION
    settings.flows_file.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("PROPOSE SUMMARY proposed=%d accepted=%d rejected=%d file=%s",
             len(raw_flows), len(accepted), len(rejected), settings.flows_file)
    return 0
