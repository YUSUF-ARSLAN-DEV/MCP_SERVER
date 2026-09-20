"""The plain-sentence starting point of flow testing: runs/<site>/intents.json.

An intent is one user journey written the way a person would say it ("A visitor picks a
country and a channel, searches, and sees the satellite frequencies"). The AI writes the
first batch from the site map (`intents`), a person can add (`flows add`), reword
(`flows edit`) or drop (`flows drop`) any of them, and a later stage (`expand`) turns each
sentence into concrete steps that code checks against the explored pages.

A sentence cannot be fact-checked on its own, so this file never produces a test: it only
holds intent. Everything downstream still needs a verified run before a test exists.

  status: new (not expanded yet) | expanded (has a flow) | unbuildable (see reason) |
          covered (an existing flow already does this) | dropped (a person removed it)
The sentence is the source of truth: editing it makes the intent due for expansion again.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

from .sitemap import build_site_map, load_inventories, render_site_map

INTENTS_VERSION = 1
PROMPT_VERSION = "intents-v1"
MAX_NEW = 8
MIN_LEN, MAX_LEN = 15, 240
DUPLICATE_OVERLAP = 0.8
STATUSES = {"new", "expanded", "unbuildable", "covered", "dropped"}

SYSTEM = "Return exactly one JSON object and no prose. Propose only what the site map supports."

RULES = (
    "You are a QA analyst writing test ideas for a non-technical reader. From the SITE MAP below, write up to %d "
    "user journeys as plain English sentences, the way a person would describe them out loud, for example: "
    "\"A visitor picks a country and a channel, searches, and sees the satellite frequencies.\"\n"
    "Rules:\n"
    "- One sentence per journey: what the visitor does, then what they should see. Cross pages when a real link exists.\n"
    "- NO technical words: no selectors, ids, CSS, URLs, button ids, or code. Use the names visitors see on screen.\n"
    "- Only journeys the SITE MAP supports. Do not invent pages, buttons, or results.\n"
    "- Never a journey that needs an account, password, payment or a real person's data, a language switch, or one "
    "that only shows widgets appearing. Each sentence must describe something a visitor achieves.\n"
    "- Do not repeat or reword anything in EXISTING.\n"
    "- start_path is the page the visitor starts on (a path from the SITE MAP). evidence is one sentence citing the "
    "page and controls that make you believe the journey exists.\n"
    'Output: {"intents":[{"sentence":"...","start_path":"/...","evidence":"..."}]}'
) % MAX_NEW


class IntentsFileError(Exception):
    """The intents file exists but cannot be read - never overwrite it silently."""


class IntentError(Exception):
    """A user-facing problem (bad sentence, unknown intent); the message says what to do."""


# ------------------------------------------------------------------ the file

def load_intents(path: Path) -> dict:
    if not path.exists():
        return {"version": INTENTS_VERSION, "intents": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntentsFileError(f"{path} is not valid JSON ({exc}); fix or delete it") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("intents"), list):
        raise IntentsFileError(f"{path} has no 'intents' list")
    return doc


def save_intents(path: Path, doc: dict) -> None:
    doc["version"] = INTENTS_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ sentences

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def sentence_hash(sentence: str) -> str:
    return hashlib.sha1(_norm(sentence).encode("utf-8")).hexdigest()[:12]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]{3,}", _norm(text)))


def _overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


_TECHNICAL = re.compile(r"#[\w-]+|\[[\w-]+\s*[*^$]?=|https?://|</?\w+>|\.\w+\(|\bselector\b|\bxpath\b|\bcss\b", re.I)


def looks_technical(sentence: str) -> bool:
    return bool(_TECHNICAL.search(sentence))


# Things the AI must not write as a journey: each would become a weak, unsafe or untestable flow.
_UNSUITABLE = (
    (re.compile(r"\b(password|passcode|log ?in|sign ?in|sign ?up|credit card|payment|checkout)\b", re.I),
     "needs credentials or payment details"),
    (re.compile(r"\b(controls?|elements?|widgets?|fields?|options?)\b[^.]{0,20}\b(appear|show|display|become visible)", re.I),
     "describes widgets appearing, not something a visitor achieves"),
    (re.compile(r"\b(language|arabic version|english version|switch(es)? to (arabic|english))\b", re.I),
     "a language switch leaves the site and cannot be tested"),
)


def unsuitable_reason(sentence: str) -> str:
    for pattern, reason in _UNSUITABLE:
        if pattern.search(sentence):
            return reason
    return ""


def check_sentence(sentence: str, *, technical_ok: bool) -> str:
    """'' when acceptable, else why not. A person may write anything reasonable; the AI may not sound like a machine."""
    text = " ".join((sentence or "").split())
    if len(text) < MIN_LEN:
        return f"too short to describe a journey (min {MIN_LEN} characters)"
    if len(text) > MAX_LEN:
        return f"too long for one sentence (max {MAX_LEN} characters)"
    if not technical_ok and looks_technical(text):
        return "reads like code (selectors/URLs); write what a visitor sees and does"
    if not technical_ok and (why := unsuitable_reason(text)):     # the AI is held to these; a person may override
        return why
    return ""


def find_duplicate(doc: dict, sentence: str, skip_id: str | None = None) -> dict | None:
    for intent in doc["intents"]:
        if intent.get("status") == "dropped" or intent.get("id") == skip_id:
            continue
        if _norm(intent["sentence"]) == _norm(sentence) or _overlap(intent["sentence"], sentence) >= DUPLICATE_OVERLAP:
            return intent
    return None


def _next_id(doc: dict) -> str:
    numbers = [int(m.group(1)) for i in doc["intents"] if (m := re.fullmatch(r"i-(\d+)", str(i.get("id", ""))))]
    return f"i-{(max(numbers) + 1 if numbers else 1):03d}"      # ids are never reused, dropped ones included


def add_intent(doc: dict, sentence: str, source: str, now: str, start_path: str | None = None,
               evidence: str = "", proposed_by: dict | None = None) -> dict:
    """Append a new intent, or raise IntentError with the reason (bad wording, duplicate)."""
    text = " ".join((sentence or "").split())
    reason = check_sentence(text, technical_ok=source == "human")
    if reason:
        raise IntentError(reason)
    twin = find_duplicate(doc, text)
    if twin:
        raise IntentError(f'duplicate of {twin["id"]}: "{twin["sentence"]}"')
    intent = {"id": _next_id(doc), "sentence": text, "source": source, "status": "new",
              "created_at": now, "flow_id": None, "expanded_hash": None, "reason": ""}
    if start_path:
        intent["start_path"] = start_path
    if evidence:
        intent["evidence"] = evidence[:300]
    if proposed_by:
        intent["proposed_by"] = proposed_by
    doc["intents"].append(intent)
    return intent


def find_intent(doc: dict, ref: str) -> dict:
    ref = (ref or "").strip()
    if not ref:
        raise IntentError("give an intent id (see `flows intents`)")
    wanted = f"i-{int(ref):03d}" if ref.isdigit() else ref
    for intent in doc["intents"]:
        if intent.get("id") == wanted:
            return intent
    raise IntentError(f'no intent "{ref}" (see `flows intents`)')


def edit_intent(doc: dict, ref: str, sentence: str, now: str) -> dict:
    intent = find_intent(doc, ref)
    text = " ".join((sentence or "").split())
    reason = check_sentence(text, technical_ok=True)
    if reason:
        raise IntentError(reason)
    twin = find_duplicate(doc, text, skip_id=intent["id"])
    if twin:
        raise IntentError(f'duplicate of {twin["id"]}: "{twin["sentence"]}"')
    intent["sentence"], intent["edited_at"] = text, now
    if intent["status"] == "dropped":
        intent["status"] = "new"          # editing a dropped sentence brings it back
    return intent


def drop_intent(doc: dict, ref: str, reason: str, now: str) -> dict:
    intent = find_intent(doc, ref)
    intent.update(status="dropped", reason=(reason or "").strip() or "dropped by a person", dropped_at=now)
    return intent


def needs_expansion(intent: dict) -> bool:
    """New, or the sentence changed since it was last expanded / attempted."""
    return intent.get("status") != "dropped" and intent.get("expanded_hash") != sentence_hash(intent["sentence"])


# ------------------------------------------------------------------ rendering

def render_intents(doc: dict) -> str:
    rows = doc["intents"]
    if not rows:
        return "no intents yet - run `intents` (AI writes them) or `flows add \"...\"`"
    lines = []
    for i in rows:
        note = f' [{i["reason"]}]' if i.get("reason") and i["status"] in {"unbuildable", "dropped"} else ""
        flag = " (edited, will re-expand)" if i["status"] != "dropped" and needs_expansion(i) and i.get("expanded_hash") else ""
        link = f' -> {i["flow_id"]}' if i.get("flow_id") else ""
        lines.append(f'{i["id"]}  {i["status"]:<11} {i["source"]:<5} {i["sentence"]}{note}{link}{flag}')
    counts: dict[str, int] = {}
    for i in rows:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    return "\n".join([*lines, "", ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))])


# ------------------------------------------------------------------ AI writes the sentences

def prompt_for(site_map_text: str, existing: list[str]) -> str:
    shown = "\n".join(f"- {s}" for s in existing[:40]) or "(none yet)"
    return f"{RULES}\n\nEXISTING\n{shown}\n\n{site_map_text}"


def parse_response(raw: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model response")
    data = json.loads(text[start:end + 1])
    rows = data.get("intents") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("response has no 'intents' list")
    return [r for r in rows if isinstance(r, dict)]


def accept_intents(raw_rows: list[dict], doc: dict, pages: set[str], now: str, model: str = ""
                   ) -> tuple[list[dict], list[tuple[str, str]]]:
    """Add the model's sentences that pass the checks. Returns (added intents, [(sentence, reason)] rejected)."""
    added, rejected = [], []
    for row in raw_rows[:MAX_NEW]:
        sentence = str(row.get("sentence") or "").strip()
        start = str(row.get("start_path") or "").split("?")[0].split("#")[0] or "/"
        if start not in pages:
            rejected.append((sentence[:120], f"start page {start} was not explored"))
            continue
        try:
            added.append(add_intent(doc, sentence, "ai", now, start, str(row.get("evidence") or ""),
                                    {"model": model, "prompt_version": PROMPT_VERSION}))
        except IntentError as exc:
            rejected.append((sentence[:120], str(exc)))
    return added, rejected


def run_intents(settings, urls: list[str], client, log) -> int:
    from datetime import datetime, timezone
    from .flows import FlowsFileError, load_flows
    wanted = set(urls)
    inventories = [i for i in load_inventories(settings.artifacts_dir) if i.get("url") in wanted]
    if not inventories:
        log.error("intents: no inventories for the URLs in %s - run explore first", settings.urls_file)
        return 2
    try:
        doc = load_intents(settings.intents_file)
        flows = load_flows(settings.flows_file)["flows"]
    except (IntentsFileError, FlowsFileError) as exc:
        log.error("intents: %s", exc)
        return 1
    site_map = build_site_map(inventories)
    existing = [i["sentence"] for i in doc["intents"] if i.get("status") != "dropped"] + [f.get("goal", "") for f in flows]
    try:
        rows = parse_response(client.generate(prompt_for(render_site_map(site_map), existing), SYSTEM))
    except Exception as exc:
        log.error("intents: model response unusable (%s)", exc)
        return 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added, rejected = accept_intents(rows, doc, {p["path"] for p in site_map["pages"]}, now, settings.model)
    for i in added:
        log.info("intents: added %s :: %s", i["id"], i["sentence"])
    for sentence, reason in rejected:
        log.info("intents: rejected '%s' (%s)", sentence, reason)
    save_intents(settings.intents_file, doc)
    log.info("INTENTS SUMMARY proposed=%d added=%d rejected=%d file=%s", len(rows), len(added), len(rejected), settings.intents_file)
    return 0


# ------------------------------------------------------------------ `flows add|edit|drop|intents`

def run_intent_action(settings, action: str, rest: list[str], reason: str, out) -> int:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        doc = load_intents(settings.intents_file)
        if action == "intents":
            out(render_intents(doc))
            return 0
        if action == "add":
            intent = add_intent(doc, " ".join(rest), "human", now)
            out(f'{intent["id"]} added: {intent["sentence"]}')
        elif action == "edit":
            if len(rest) < 2:
                raise IntentError('usage: flows edit <intent id> "the new sentence"')
            intent = edit_intent(doc, rest[0], " ".join(rest[1:]), now)
            out(f'{intent["id"]} reworded; it will be expanded again: {intent["sentence"]}')
        else:  # drop
            intent = drop_intent(doc, rest[0] if rest else "", reason, now)
            out(f'{intent["id"]} dropped')
        save_intents(settings.intents_file, doc)
        return 0
    except IntentError as exc:
        out(f"flows: {exc}")
        return 2
    except IntentsFileError as exc:
        out(f"flows: {exc}")
        return 1
