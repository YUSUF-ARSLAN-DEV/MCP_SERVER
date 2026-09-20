"""Expand plain sentences (intents.json) into concrete flows (flows.json).

The model reads ONE sentence plus the site map (built from the pages' DOM) and proposes the
steps. Code then decides, with the same checks `propose` uses: every page, control and outcome
must exist in the explored pages, chained pages must be linked, duplicates of an existing flow
are recognised, and the critic must find the steps a coherent match for the sentence. A
"pick an option" step gets its option from what the explorer saw open, never from the model.

Nothing is ever guessed: a sentence that cannot be built is marked unbuildable with the reason
(shown by `flows intents`) and produces no flow, so it can never produce a weak test. A flow
that is built enters flows.json as status=candidate and still needs a real `verify` run.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone

from .critic import MIN_COHERENCE, rate_flows
from .flows import FlowsFileError, flow_id, load_flows, merge_flow, save_flows
from .intents import (
    IntentsFileError, first_json_object, load_intents, needs_expansion, save_intents, sentence_hash,
)
from .proposer import MAX_STEPS, _index, _page_key, _signature, _transitions, validate_flow
from .ratings import RatingsFileError, append_rating, load_ratings, save_ratings
from .sitemap import build_site_map, load_inventories, render_site_map

PROMPT_VERSION = "expand-v1"
MAX_PER_RUN = 10

SYSTEM = "Return exactly one JSON object and no prose. Use only what the site map supports."

RULES = (
    "You are a QA engineer turning ONE plain-English sentence into the concrete steps a browser will run. "
    "The SITE MAP below is the only source of truth.\n"
    "Rules:\n"
    "- Do exactly what the sentence says, in order. Do not add steps it does not ask for and do not skip steps it does. "
    "At most %d steps.\n"
    "- Use ONLY pages and controls listed in the SITE MAP. Copy control names exactly as written there.\n"
    "- Every step names the page it happens on (its path). After a step that navigates, later steps happen on the "
    "destination page; chain pages only in the direction the SITE MAP's LINKS or NAV connect them.\n"
    "- Step actions: click | select | fill | pick (pick = choose an option in a dropdown or checkbox menu). "
    "Give a value ONLY if the sentence names a specific option or text; otherwise leave value null. A button that opens a checkbox or dropdown menu must be a pick step, not a click, when the sentence says to pick, choose or select one of its options.\n"
    "- Outcomes: navigates (give to_path) | shows_results | shows_validation | reveals_panel. Choose the one that "
    "matches what the sentence says the visitor should see.\n"
    "- If the sentence needs something the SITE MAP does not show (a control, a page or a result), do NOT invent it: "
    'return {"flows":[],"cannot":"what is missing"}.\n'
    'Output: {"flows":[{"start_path":"/...","steps":[{"page":"/...","action":"click","target":"<control name>",'
    '"value":null}],"outcome":{"type":"navigates","to_path":"/..."},"evidence":"one sentence"}]}'
) % MAX_STEPS


def prompt_for(sentence: str, start_path: str | None, site_map_text: str) -> str:
    starts = f"\nSTARTS ON: {start_path}" if start_path else ""
    return f"{RULES}\n\nSENTENCE: {sentence}{starts}\n\n{site_map_text}"


def parse_expansion(raw: str) -> tuple[dict | None, str]:
    """(flow dict, '') or (None, why the model could not build one). Raises ValueError on a non-answer."""
    data = first_json_object(raw)
    if not isinstance(data, dict) or not isinstance(data.get("flows"), list):
        raise ValueError("response has no 'flows' list")
    flows = [f for f in data["flows"] if isinstance(f, dict)]
    if flows:
        return flows[0], ""
    return None, str(data.get("cannot") or "the model found no way to build this from the site map")[:200]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


_OPTION_ROLES = {"checkbox", "option", "menuitemcheckbox", "radio", "menuitemradio"}
_ALL_OPTION = re.compile(r"^(select|check|uncheck|deselect|clear)?" + chr(92) + "s*(all|none|everything)$", re.I)
_PICK_VERB = re.compile(r"(?:" + chr(92) + "b(pick|picks|picked|choose|chooses|chosen|select|selects|selecting|filter|filters)" + chr(92) + "b)", re.I)
_TRIGGER_STOP = {"please", "select", "choose", "pick", "your", "the"}


def _is_option(control: dict) -> bool:
    role = (control.get("role") or "").lower()
    return bool(control.get("name")) and (role in _OPTION_ROLES or (
        control.get("tag") == "input" and (control.get("type") or "").lower() in {"checkbox", "radio"}))


def _options_of(trigger: str, page: str, inventories: list[dict]) -> list[str]:
    """Option names the explorer saw when it opened `trigger` on `page` (checkbox / option controls only)."""
    wanted = _norm(trigger)[:40]
    out: list[str] = []
    for inv in inventories:
        if _page_key(re.sub(r"^https?://[^/]+", "", inv.get("url", ""))) != page:
            continue
        for entry in inv.get("revealed") or []:
            if _norm(entry.get("trigger") or "")[:40] == wanted:
                out += [c["name"] for c in entry.get("controls") or [] if _is_option(c)]
    return out


def _sentence_picks(sentence: str, trigger: str) -> bool:
    """Does the sentence say to pick/choose something that is this menu (its key word follows a pick verb)?"""
    keys = [w for w in re.findall(r"[^" + chr(92) + "W" + chr(92) + "d_]{4,}", trigger.lower()) if w not in _TRIGGER_STOP]
    for verb in _PICK_VERB.finditer(sentence):
        window = sentence[verb.end():verb.end() + 70].lower()
        if any(k in window for k in keys):
            return True
    return False


def _triggers_offering(option: str, page: str, inventories: list[dict]) -> list[str]:
    """Menu buttons on `page` whose explored menu holds an option that matches `option`."""
    wanted, found = _norm(option), []
    for inv in inventories:
        if _page_key(re.sub(r"^https?://[^/]+", "", inv.get("url", ""))) != page:
            continue
        for entry in inv.get("revealed") or []:
            trigger = entry.get("trigger") or ""
            if trigger and trigger not in found and any(
                    wanted and (wanted in _norm(c["name"]) or _norm(c["name"]) in wanted)
                    for c in entry.get("controls") or [] if _is_option(c)):
                found.append(trigger)
    return found


def repair_option_targets(steps: list[dict], inventories: list[dict]) -> list[str]:
    """The model sometimes names the OPTION as the thing to pick from ('pick Al Jazeera Documentary' with that
    option as the target) instead of the menu button. When the target is not a menu but is an option of exactly
    one explored menu, that is what was meant: the menu becomes the target and the option the value.
    Returns 'option -> menu' for each repair."""
    fixed = []
    for step in steps:
        if step.get("kind") != "multiselect" or _options_of(step.get("name") or "", step["page"], inventories):
            continue
        menus = _triggers_offering(step.get("name") or "", step["page"], inventories)
        if len(menus) == 1:
            fixed.append(f'{step.get("name")} -> {menus[0]}')
            step.update(value=step.get("value") or step.get("name"), name=menus[0][:40], selector=None)
    return fixed


def _select_options(step: dict, page: str, inventories: list[dict]) -> list[str]:
    for inv in inventories:
        if _page_key(re.sub(r"^https?://[^/]+", "", inv.get("url", ""))) != page:
            continue
        for c in inv.get("controls") or []:
            if c.get("tag") == "select" and step.get("selector") and c.get("selector") == step.get("selector"):
                return [str(o) for o in c.get("options") or [] if o]
    return []


def ground_sentence_values(steps: list[dict], sentence: str, inventories: list[dict]) -> list[str]:
    """When a select or pick step has no value but the sentence names exactly one of that control's real
    options ('picks Qatar'), use it. The option comes from the explored page, the model only had to leave
    it out. Returns 'step -> value' for each one set."""
    said, out = _norm(sentence), []
    for step in steps:
        if step.get("value") or step.get("kind") not in {"select", "multiselect"}:
            continue
        options = (_select_options(step, step["page"], inventories) if step["kind"] == "select"
                   else _options_of(step.get("name") or "", step["page"], inventories))
        named = [o for o in options if len(_norm(o)) >= 3 and not _ALL_OPTION.match(o.strip())
                 and re.search(r"(?<![a-z0-9])" + re.escape(_norm(o)) + r"(?![a-z0-9])", said)]
        if len(named) == 1:
            step["value"] = named[0][:80]
            out.append(f'{step.get("name") or step.get("selector")} -> {named[0]}')
    return out


def drop_superseded_picks(steps: list[dict]) -> list[str]:
    """Two picks in a row from the same menu where the second one names its option: the first one only
    said 'pick a channel' and the second says which, so it is dropped (the flow would otherwise choose twice)."""
    dropped, keep = [], []
    for i, step in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if (nxt and step.get("kind") == "multiselect" and nxt.get("kind") == "multiselect"
                and step.get("page") == nxt.get("page") and _norm(step.get("name") or "") == _norm(nxt.get("name") or "")
                and (not step.get("value") or _norm(step["value"]) == _norm(nxt.get("value") or ""))):
            dropped.append(step.get("name") or "")
            continue
        keep.append(step)
    steps[:] = keep
    return dropped


def upgrade_menu_clicks(steps: list[dict], sentence: str, inventories: list[dict]) -> list[str]:
    """The model often writes 'pick a channel' as a plain click, which only opens the menu. When a click targets a
    button whose explored menu holds options and the sentence says to pick from it, make it a real pick step.
    Returns the names of upgraded triggers."""
    upgraded = []
    for step in steps:
        if step.get("kind") == "click" and _options_of(step.get("name") or "", step["page"], inventories)                 and _sentence_picks(sentence, step.get("name") or ""):
            step["kind"] = "multiselect"
            upgraded.append(step.get("name") or "")
    return upgraded


def ground_options(steps: list[dict], inventories: list[dict]) -> str:
    """Give every 'pick' step an option the explorer really saw. '' when fine, else the reason it cannot be built."""
    for step in steps:
        if step.get("kind") != "multiselect":
            continue
        options = _options_of(step.get("name") or "", step["page"], inventories)
        value = step.get("value")
        if value:
            if options and not any(_norm(value) in _norm(o) or _norm(o) in _norm(value) for o in options):
                return f'option "{value}" was never seen in "{step.get("name")}" on {step["page"]}'
            continue
        real = [o for o in options if not _ALL_OPTION.match(o.strip())]      # "Select All" is not "a channel"
        if not real:
            return f'no option was ever seen inside "{step.get("name")}" on {step["page"]}, so nothing can be picked'
        step["value"] = real[0][:80]          # the first real option the explorer saw: a fact, not a guess
    return ""


def _flow_signatures(flows: list[dict], skip_id: str | None) -> dict[tuple, str]:
    return {_signature(f.get("steps") or []): f["id"] for f in flows if f.get("id") != skip_id}


def expand_intent(intent: dict, client, inventories: list[dict], site_map: dict, site_map_text: str,
                  flows: list[dict], model: str, now: str, log=None) -> tuple[dict | None, dict | None, str]:
    """Try to build one flow. Returns (flow, rating entry, '') on success, (None, None, reason) when the sentence
    cannot be built, or (None, None, '') when the model call itself failed (retry later)."""
    try:
        raw, cannot = parse_expansion(client.generate(prompt_for(intent["sentence"], intent.get("start_path"), site_map_text), SYSTEM))
    except Exception as exc:
        if log:
            log.warning("expand: %s - model answer unusable (%s); will retry", intent["id"], exc)
        return None, None, ""
    if raw is None:
        return None, None, f"model: {cannot}"
    raw = {**raw, "goal": intent["sentence"]}
    index = _index(inventories)
    pages = set(index)
    edges = site_map["edges"]
    steps, outcome, reason = validate_flow(
        raw, index, pages, frozenset(_page_key(e["to"]) for e in edges), _transitions(edges, site_map["nav"]))
    if steps is None:
        return None, None, reason
    for repair in repair_option_targets(steps, inventories):
        if log:
            log.info("expand: %s - repaired a pick that targeted an option: %s", intent["id"], repair)
    upgraded = upgrade_menu_clicks(steps, intent["sentence"], inventories)
    if upgraded and log:
        log.info("expand: %s - %s is a pick, not a click (the sentence says to pick from it)", intent["id"], ", ".join(upgraded))
    for done in ground_sentence_values(steps, intent["sentence"], inventories):
        if log:
            log.info("expand: %s - the sentence names the option: %s", intent["id"], done)
    for menu in drop_superseded_picks(steps):
        if log:
            log.info("expand: %s - dropped a pick of %s that a later pick of the same menu supersedes", intent["id"], menu)
    reason = ground_options(steps, inventories)
    if reason:
        return None, None, reason
    twin = _flow_signatures(flows, intent.get("flow_id")).get(_signature(steps))
    url_of = {_page_key(re.sub(r"^https?://[^/]+", "", i.get("url", ""))): i.get("url", "") for i in inventories}
    start = url_of[_page_key(str(raw.get("start_path")))]
    fid = intent.get("flow_id") or flow_id(start, intent["sentence"])
    flow = {"id": fid, "goal": intent["sentence"], "source": "intent", "status": "candidate", "start_url": start,
            "steps": steps, "outcome": outcome, "evidence": str(raw.get("evidence") or "")[:300],
            "intent_id": intent["id"], "intent_hash": sentence_hash(intent["sentence"]),
            "proposed_by": {"model": model, "prompt_version": PROMPT_VERSION}, "observed_at": now}
    if twin:
        flow["duplicate_of"] = twin
        return flow, None, ""
    rating = None
    try:
        rating = rate_flows(client, [flow], site_map_text, model, now).get(1)
    except Exception as exc:
        if log:
            log.warning("expand: %s - critic unavailable, keeping the flow unrated (%s)", intent["id"], exc)
    if rating is not None:
        rating["kept"] = rating["scores"]["coherence"] >= MIN_COHERENCE
        if not rating["kept"]:
            return None, None, f'critic: the steps do not match the sentence (coherence {rating["scores"]["coherence"]}: {rating["reason"]})'
    return flow, rating, ""


def _unique_id(doc: dict, wanted: str, intent_id: str) -> str:
    taken = {f["id"]: f for f in doc["flows"]}
    if wanted not in taken or taken[wanted].get("intent_id") == intent_id:
        return wanted
    n = 2
    while f"{wanted}-{n}" in taken:
        n += 1
    return f"{wanted}-{n}"


def run_expand(settings, urls: list[str], client, log, only: list[str] | None = None) -> int:
    wanted_urls = set(urls)
    inventories = [i for i in load_inventories(settings.artifacts_dir) if i.get("url") in wanted_urls]
    if not inventories:
        log.error("expand: no inventories for the URLs in %s - run explore first", settings.urls_file)
        return 2
    try:
        intents = load_intents(settings.intents_file)
        flows = load_flows(settings.flows_file)
        ratings = load_ratings(settings.ratings_file)
    except (IntentsFileError, FlowsFileError, RatingsFileError) as exc:
        log.error("expand: %s", exc)
        return 1
    forced = {(o if o.startswith("i-") else f"i-{int(o):03d}") for o in (only or []) if o.startswith("i-") or o.isdigit()}
    todo = [i for i in intents["intents"]
            if i.get("status") != "dropped" and (i["id"] in forced or (not forced and needs_expansion(i)))][:MAX_PER_RUN]
    if not todo:
        log.info("expand: nothing to expand (every sentence already has an answer; edit one or pass its id to redo it)")
        return 0
    site_map = build_site_map(inventories)
    site_map_text = render_site_map(site_map)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    built = failed = retry = 0
    for intent in todo:
        flow, rating, reason = expand_intent(intent, client, inventories, site_map, site_map_text,
                                             flows["flows"], settings.model, now, log)
        if flow is None and not reason:
            retry += 1                      # transient model trouble: leave the intent as it was
            continue
        intent["expanded_hash"] = sentence_hash(intent["sentence"])
        if flow is None:
            intent.update(status="unbuildable", reason=reason)
            failed += 1
            log.info("expand: %s unbuildable (%s)", intent["id"], reason)
            continue
        if flow.get("duplicate_of"):
            intent.update(status="covered", flow_id=flow["duplicate_of"], reason="")
            log.info("expand: %s is already covered by flow %s", intent["id"], flow["duplicate_of"])
            continue
        flow["id"] = _unique_id(flows, flow["id"], intent["id"])
        if merge_flow(flows, flow) == "kept":
            intent.update(status="unbuildable", flow_id=flow["id"], reason=(
                f'flow {flow["id"]} was decided by a person; `flows reset` it, then `expand {intent["id"]}` to rebuild'))
            failed += 1
            log.info("expand: %s not rebuilt (%s)", intent["id"], intent["reason"])
            continue
        if rating is not None:
            append_rating(ratings, flow["id"], rating)
        intent.update(status="expanded", flow_id=flow["id"], reason="", expanded_at=now)
        built += 1
        log.info("expand: %s -> %s (%d steps)", intent["id"], flow["id"], len(flow["steps"]))
    save_intents(settings.intents_file, intents)
    save_flows(settings.flows_file, flows)
    save_ratings(settings.ratings_file, ratings)
    log.info("EXPAND SUMMARY sentences=%d built=%d unbuildable_or_kept=%d retry_later=%d", len(todo), built, failed, retry)
    return 1 if retry else 0
