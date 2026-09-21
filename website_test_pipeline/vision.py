"""`flows judge`: a vision model looks at a flow's final screenshot and rates it.

A passing flow test proves its assertions held; it cannot notice that the page it ended on is a blank
screen, an error page, an empty results table or a spinner that never finished. A person would see that in
one glance at the last screenshot. This asks a vision-capable model to do the same glance.

It is only ever an opinion, recorded like the model critic's: an entry with source=vision in flow_ratings.json.
It never verifies or fails a flow and never changes a status (ratings.derive_status ignores it); the report
shows it as a warning to look, next to the screenshot.

Code stays in charge of trust:
  * a model that cannot really see images may ignore the picture and answer anyway. So the model must also
    list a few words it can READ in the screenshot, and code checks them against what is really on that page
    (its headings, control names, the flow's own steps). A verdict whose words are not on the page is
    discarded as "did not read the screenshot" and nothing is recorded;
  * the answer must be well-formed (a known verdict, scores 1-5, a reason) or it is discarded;
  * the same screenshot is judged once (the entry remembers which image it looked at);
  * a model that refuses images, or is unavailable, stops the command cleanly with nothing written.
"""
from __future__ import annotations
import base64
import hashlib
import io
import re
from datetime import datetime, timezone
from pathlib import Path

from .flows import FlowsFileError, describe_step, load_flows
from .ratings import RatingsFileError, append_rating, load_ratings, save_ratings

PROMPT_VERSION = "vision-v1"
VERDICTS = ("shows_expected", "looks_broken", "unclear")
MAX_SIDE = 1024                  # longest edge sent to the model; full-page screenshots are far larger
JPEG_QUALITY = 60
MIN_WORD_LEN = 4                 # a "word it can read" shorter than this proves nothing
UNSUPPORTED = {400, 415, 422}    # what a server answers when it does not accept image input

SYSTEM = "Return exactly one JSON object and no prose. Judge only what is visible in the screenshot."

RULES = (
    "You are a QA reviewer looking at the FINAL screenshot of an automated user journey on a website. "
    "Below is what the journey was meant to do and what the automated run observed. Judge only what you can see.\n"
    "- verdict: \"shows_expected\" if the screenshot shows what the journey promised (for example the results it "
    "talks about are visible); \"looks_broken\" if it shows an error page, a blank page, an empty results area, "
    "a loading spinner, a cookie wall or a login wall blocking the content, or clearly the wrong page; "
    "\"unclear\" if you cannot tell.\n"
    "- scores (1-5): matches_sentence = how well the page matches what the journey promised; "
    "content_visible = how much real content (text, tables, images) is visible rather than empty or loading.\n"
    "- visible_text: 3 to 6 short pieces of text you can actually READ in the screenshot, copied exactly. "
    "Never guess text you cannot read.\n"
    "- reason: one sentence saying what you see.\n"
    'Output: {"verdict":"shows_expected","scores":{"matches_sentence":4,"content_visible":5},'
    '"visible_text":["...","..."],"reason":"..."}'
)


class VisionError(Exception):
    """The judgement cannot be used; the message says why."""


# ------------------------------------------------------------------ the picture

def _slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", nodeid).strip("-") or "test"     # the same slug conftest.py names evidence folders with


def outcome_screenshot(settings, flow: dict, results: dict | None = None) -> Path | None:
    """The last screenshot of the flow's generated test: `99-outcome` when it has one, else the highest-numbered
    step. None when the test never ran or left no screenshots."""
    import json
    from .flowresults import match_results
    if results is None:
        path = settings.artifacts_dir / "test_results.json"
        if not path.exists():
            return None
        try:
            results = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    row = match_results(results, [flow]).get(flow["id"])
    if row is None:
        return None
    folder = settings.artifacts_dir / "evidence" / _slug(row.get("nodeid", ""))
    if not folder.is_dir():
        return None
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"})
    outcome = [p for p in images if p.stem.startswith("99-")]
    return (outcome or images or [None])[-1]


def image_id(path: Path) -> str:
    """Which picture an entry looked at (name and content hash), so the same one is not judged twice."""
    return f"{path.name}#{hashlib.sha1(path.read_bytes()).hexdigest()[:10]}"


def encode_image(path: Path) -> str:
    """A data URL of the screenshot, downscaled and JPEG-compressed so the request stays small."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        scale = min(MAX_SIDE / max(im.width, im.height), 1.0)
        if scale < 1.0:
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        buffer = io.BytesIO()
        im.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


# ------------------------------------------------------------------ what is really on the page

def known_text(flow: dict, inventories: list[dict]) -> list[str]:
    """Strings that are really on the page the flow ended on (and in its own steps), against which the words the
    model claims to read are checked: headings and control names of that page, new headings the run saw, step names."""
    from urllib.parse import urlsplit
    observed = flow.get("observed") or {}
    landing = (urlsplit(observed.get("url") or flow.get("start_url") or "").path or "/").rstrip("/") or "/"
    texts: list[str] = list(observed.get("new_headings") or []) + [s.get("name") or "" for s in flow.get("steps") or []]
    texts += [c.split(":", 1)[-1] for c in observed.get("new_controls") or []]
    for inv in inventories:
        path = (urlsplit(inv.get("url", "")).path or "/").rstrip("/") or "/"
        if path == landing:
            texts += [h.get("text") or "" for h in inv.get("headings") or []]
            texts += [c.get("name") or "" for c in inv.get("controls") or []]
    return [t for t in texts if t and t.strip()]


def _key(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def read_check(visible_text: list[str], known: list[str]) -> list[str]:
    """The words the model says it read that really are on the page. Empty means it did not read the screenshot."""
    keys = [_key(k) for k in known if len(_key(k)) >= MIN_WORD_LEN]
    confirmed = []
    for word in visible_text:
        w = _key(word)
        if len(w) >= MIN_WORD_LEN and any(w in k or k in w for k in keys):
            confirmed.append(word)
    return confirmed


# ------------------------------------------------------------------ asking

def prompt_for(flow: dict) -> str:
    steps = "; ".join(describe_step(s) for s in flow.get("steps") or []) or "(none)"
    observed = flow.get("observed") or {}
    seen = ", ".join(observed.get("new_headings") or []) or "(no new heading)"
    predicted = flow.get("outcome") or {}
    return (f"{RULES}\n\nJOURNEY: {flow.get('goal') or flow['id']}\nSTEPS RUN: {steps}\n"
            f"EXPECTED OUTCOME: {predicted.get('effect') or 'not stated'}"
            f"{' -> ' + predicted['to'] if predicted.get('to') else ''}\n"
            f"THE RUN OBSERVED: {observed.get('effect') or 'nothing'}; new headings: {seen}\n\n"
            "Look at the screenshot and answer now with the JSON object only, starting with {.")


def parse_verdict(raw: str) -> dict:
    """A well-formed verdict, or VisionError."""
    from .intents import first_json_object
    try:
        data = first_json_object(raw)
    except ValueError as exc:
        raise VisionError(str(exc)) from exc
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        raise VisionError(f"verdict must be one of {', '.join(VERDICTS)}")
    scores = data.get("scores")
    if not isinstance(scores, dict) or not all(isinstance(scores.get(k), int) and 1 <= scores[k] <= 5
                                               for k in ("matches_sentence", "content_visible")):
        raise VisionError("scores must be whole numbers 1-5 for matches_sentence and content_visible")
    words = data.get("visible_text")
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise VisionError("visible_text must be a list of strings")
    return {"verdict": data["verdict"], "scores": {k: scores[k] for k in ("matches_sentence", "content_visible")},
            "visible_text": [w.strip() for w in words if w.strip()][:8], "reason": str(data.get("reason") or "").strip()[:300]}


def judge_flow(client, flow: dict, image: Path, inventories: list[dict], model: str, now: str) -> dict:
    """One rating entry for a flow's final screenshot. Raises VisionError when the answer cannot be trusted, and lets
    model errors (unavailable, refuses images) through for the caller to classify."""
    verdict = parse_verdict(client.generate(prompt_for(flow), SYSTEM, images=[encode_image(image)]))
    confirmed = read_check(verdict["visible_text"], known_text(flow, inventories))
    if not confirmed:
        raise VisionError("none of the words it says it read are on that page - it did not read the screenshot")
    return {"source": "vision", "at": now, "model": model, "prompt_version": PROMPT_VERSION, "image": image_id(image),
            **verdict, "verified_text": confirmed}


def latest_vision(entries: list[dict]) -> dict | None:
    return next((e for e in reversed(entries) if e.get("source") == "vision"), None)


def run_judge(settings, client, log, only: list[str] | None = None) -> int:
    """`flows judge [id ...]`. 0 done (something was judged or everything already had been), 1 nothing could be
    judged, 2 no flow has a screenshot, 4 model unavailable, 5 the model does not accept images."""
    from .llm import ModelError, is_unavailable
    from .sitemap import load_inventories
    try:
        doc = load_flows(settings.flows_file)
        ratings = load_ratings(settings.ratings_file)
    except (FlowsFileError, RatingsFileError) as exc:
        log.error("judge: %s", exc)
        return 1
    flows = [f for f in doc["flows"] if f.get("status") in {"verified", "approved", "stale"}
             and (not only or any(ref == f["id"] or ref in f["id"] for ref in only))]
    inventories = load_inventories(settings.artifacts_dir)
    todo = []
    for flow in flows:
        image = outcome_screenshot(settings, flow)
        if image is not None:
            todo.append((flow, image))
    if not todo:
        log.error("judge: no flow has a final screenshot yet - run `execute` first")
        return 2
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    judged = skipped = discarded = 0
    for flow, image in todo:
        entries = ratings["ratings"].get(flow["id"], [])
        last = latest_vision(entries)
        if last and last.get("image") == image_id(image):
            skipped += 1
            continue
        try:
            entry = judge_flow(client, flow, image, inventories, settings.model, now)
        except VisionError as exc:
            discarded += 1
            log.warning("judge: %s - not recorded (%s)", flow["id"], exc)
            continue
        except ModelError as exc:
            if exc.status in UNSUPPORTED:
                log.error("judge: the model does not accept images (%s) - use a vision-capable model", exc)
                if judged:
                    save_ratings(settings.ratings_file, ratings)
                return 5
            if is_unavailable(exc):
                log.error("judge: the model is unavailable (%s) - kept what was recorded so far", exc)
                if judged:
                    save_ratings(settings.ratings_file, ratings)
                return 4
            discarded += 1
            log.warning("judge: %s - model answer unusable (%s)", flow["id"], exc)
            continue
        except Exception as exc:
            if is_unavailable(exc):
                log.error("judge: the model is unavailable (%s) - kept what was recorded so far", exc)
                if judged:
                    save_ratings(settings.ratings_file, ratings)
                return 4
            discarded += 1
            log.warning("judge: %s - failed (%s)", flow["id"], str(exc).splitlines()[0][:100] if str(exc) else exc.__class__.__name__)
            continue
        append_rating(ratings, flow["id"], entry)
        judged += 1
        log.info("judge: %s -> %s (matches %d/5, content %d/5): %s", flow["id"], entry["verdict"],
                 entry["scores"]["matches_sentence"], entry["scores"]["content_visible"], entry["reason"])
    if judged:
        save_ratings(settings.ratings_file, ratings)
    log.info("JUDGE SUMMARY flows=%d judged=%d already_judged=%d discarded=%d", len(todo), judged, skipped, discarded)
    return 0 if (judged or skipped) else 1
