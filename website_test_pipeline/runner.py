"""Generic flow runner: executes each flow in flows.json step by step in a real browser,
takes a DOM snapshot after every step, and diffs it against the one before. What the
diffs show - not what a model predicted - is recorded as the flow's observed outcome.

A flow becomes verified only when every step ran AND the observed outcome matches the
predicted one AND something visibly changed. Each run appends a mechanical evaluation
to flow_ratings.json. Human decisions (approved / rejected) are never changed.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from .explorer import (
    _PLACEHOLDER_OPT, _PLACEHOLDER_VAL, _CONTROL_SEL, _CONTROLS_JS, _HEADINGS_JS, _RESULTS_JS, _RESULTS_SEL,
    _close_menus, _pick_multiselect, _plausible_value, _select_first_real,
)
from . import heuristics
from .validator import _ARIA_ROLES
from .flows import HUMAN_STATUSES, describe_step, is_blocked, load_flows, save_flows
from .pageutils import dismiss_overlays, pick_option, settle_page, wait_for_loaders
from .ratings import append_rating, derive_status, load_ratings, save_ratings

# What the browser says when it could not reach the site at all (not when a flow broke): DNS failure, no
# connection, a refused or timed-out connection. A run that ends this way says nothing about the flow.
OUTAGE_MARKERS = ("ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_CONNECTION", "ERR_ADDRESS_UNREACHABLE",
                  "ERR_NETWORK", "ERR_TIMED_OUT", "ERR_PROXY", "ERR_TUNNEL")


def is_outage(text: str) -> bool:
    """True when an error message is the browser failing to reach the site rather than the page misbehaving."""
    return any(marker in (text or "") for marker in OUTAGE_MARKERS)


_STEP_WAIT_MS = 1200
_LOADER_WAIT_MS = 5000     # longest we wait for a loading spinner to go away after a step
_SETTLE_POLL_MS = 500
_SETTLE_ROUNDS = 4         # re-snapshot at most this many times waiting for the page to stop changing
_MAX_LIST = 8


# ------------------------------------------------------------------ pure helpers

def _path(url: str) -> str:
    return (urlsplit(url or "").path or "/").rstrip("/") or "/"


def _same_url(a: str, b: str) -> bool:
    return (a or "").split("#")[0].rstrip("/") == (b or "").split("#")[0].rstrip("/")


def _same_heading_key(text: str) -> str:
    """A heading compared ignoring case, spacing and punctuation: 'Find Your Store' and
    'Find your  store' are the same heading, not a new one that appeared."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def diff_snapshots(before: dict, after: dict) -> dict:
    seen_h, seen_c = {_same_heading_key(h) for h in before["headings"]}, set(before["controls"])
    seen_r = {(r["key"], r["rows"]) for r in before["results"]}
    return {
        "url_changed": not _same_url(before["url"], after["url"]),
        "url": after["url"],
        "new_headings": [h for h in after["headings"] if _same_heading_key(h) not in seen_h][:_MAX_LIST],
        "new_controls": [c for c in after["controls"] if c not in seen_c][:_MAX_LIST],
        "results": [{"key": r["key"], "rows": r["rows"]} for r in after["results"]
                    if r["in_main"] and r["rows"] >= 3 and (r["key"], r["rows"]) not in seen_r],
    }


def classify(diff: dict) -> dict:
    """The observed outcome of a diff: navigation beats results beats a reveal."""
    if diff["url_changed"]:
        effect = "navigates"
    elif diff["results"]:
        effect = "results"
    elif diff["new_headings"] or diff["new_controls"]:
        effect = "reveals"
    else:
        effect = "no-visible-change"
    return {"effect": effect, "url": diff["url"], "new_headings": diff["new_headings"],
            "new_controls": diff["new_controls"], "results": diff["results"]}


def outcome_matches(predicted: dict, observed: dict) -> bool:
    want, got = predicted.get("effect"), observed.get("effect")
    if want == "navigates":
        to = predicted.get("to")
        return got == "navigates" and (not to or _path(to) == _path(observed["url"]))
    if want == "results":
        # a results region, or - when the search also changed the URL, so the run was classed as a
        # navigation - a new heading on the page it landed on (e.g. "Here are the results for ...")
        return (got == "results" or bool(observed.get("results"))
                or (got == "navigates" and bool(observed.get("new_headings"))))
    if want in {"reveals", "validation"}:
        return got in {"reveals", "results"}
    return False


def hop_hint(step: dict, index: int, current_url: str) -> str:
    """When a step fails on a later hop, say which page it expected and where the browser is.
    Not a check by itself: a redirect (/ -> /en) makes the expected and actual page differ
    on a flow that works, so it is only added to a failure message, and never for step 1
    (its page is wherever the start URL lands)."""
    expected = step.get("page")
    if index == 0 or not expected or _path(expected) == _path(current_url):
        return ""
    return f" (hop {index + 1}: step expects page {_path(expected)}, browser is on {_path(current_url)})"


def _echoes_input(control: str, flow: dict) -> bool:
    """A control that only repeats a value the flow itself entered (the results page's filter chip
    'Blue Widget' after picking that option) is not new content."""
    name = _same_heading_key(control.partition(":")[2])
    values = [_same_heading_key(str(s.get("value"))) for s in flow.get("steps") or [] if s.get("value")]
    return bool(name) and any(v and (v in name or name in v) and min(len(v), len(name)) >= 3 for v in values)


def content_shown(observed: dict, flow: dict | None = None) -> bool:
    """Did anything besides the URL change: a new heading, a results region, or new controls that are
    not just the flow's own input echoed back?"""
    controls = [c for c in observed.get("new_controls") or [] if not flow or not _echoes_input(c, flow)]
    return bool(observed.get("new_headings") or observed.get("results") or controls)


def sentence_expects_content(flow: dict) -> bool:
    """A flow built from a plain sentence that says the visitor sees / finds / gets results is promising
    content, not just a new URL. (Explorer flows carry a machine goal and are not judged this way.)"""
    return flow.get("source") == "intent" and heuristics.has_word("content_words", flow.get("goal") or "")


def _apply_heals(flow: dict, heals: list[dict], now: str) -> None:
    """Keep the healed steps in the flow, with the old values in heal_history (nothing is lost). Called only
    when the whole run passed with them and the flow is not one a person decided."""
    steps = flow.get("steps") or []
    for heal in heals:
        index = heal["step"] - 1
        if 0 <= index < len(steps):
            if heal.get("kind") == "option":
                steps[index]["value"] = heal["now"]["value"]
            else:
                steps[index]["selector"] = heal["now"]["selector"]
                steps[index]["name"] = heal["now"]["name"]
                if heal["now"].get("role"):
                    steps[index]["role"] = heal["now"]["role"]
            flow.setdefault("heal_history", []).append({"at": now, **heal})


def judge(flow: dict, result: dict) -> dict:
    """The verdict on one run, without recording anything: did every step run, did the outcome match the
    prediction, did anything change, and - for a flow built from a sentence that promises content - was there any."""
    observed = result["observed"]
    matched = bool(result["ok"] and outcome_matches(flow.get("outcome") or {}, observed))
    changed = observed["effect"] != "no-visible-change"
    promised = sentence_expects_content(flow) and not content_shown(observed, flow)
    return {"matched": matched, "changed": changed, "promised": promised,
            "passed": bool(result["ok"] and matched and changed and not promised)}


def apply_result(flow: dict, result: dict, now: str) -> dict:
    """Update the flow from a run and return the evaluation entry for flow_ratings.json.

    flow["observed"] is overwritten only when this run PASSED. A single failed run is tolerated (see
    ratings.derive_status) and leaves the flow "verified", but if its thin or empty observation were kept,
    flowgen would rebuild the spec from it and could end up asserting nothing concrete - a green flow
    with no real evidence behind it. Freezing "observed" at the last passing run means a tolerated
    failure changes nothing about what the generated test proves; only an actual pass updates the
    evidence, and a flow that has never passed still has none (flowgen already refuses to emit for it)."""
    observed = result["observed"]
    verdict = judge(flow, result)
    matched, changed, promised, passed = verdict["matched"], verdict["changed"], verdict["promised"], verdict["passed"]
    heals = result.get("heals") or []
    if passed:
        flow["observed"] = {**observed, "step_effects": result["step_effects"],
                            "landed_url": result.get("landed_url"), "step_urls": result.get("step_urls", [])}
    flow["last_run_at"] = now
    if flow.get("status") not in HUMAN_STATUSES:
        flow["status"] = "verified" if passed else "candidate"
        if passed and heals:
            _apply_heals(flow, heals, now)
    evaluation = {
        "source": "runner", "at": now, "passed": passed,
        "checks": {"steps_completed": f'{result["steps_done"]}/{result["steps_total"]}',
                   "outcome_matched": matched, "observed_effect": observed["effect"]},
    }
    if heals:
        evaluation["healed" if passed and flow.get("heal_history") else "heal_not_kept"] = heals
    if result.get("error"):
        evaluation["error"] = result["error"]
    elif promised and result["ok"] and changed:
        # not "and matched": for a "results"-type outcome, matched already REQUIRES content to be visible
        # (outcome_matches), so a promised-but-empty result already implies matched is False here - requiring
        # both meant this branch (and the identical guard in run_verify that tries another option) could
        # never fire for exactly the flows it exists for. Found live: a flow lost its content-loss reason and
        # its automatic retry both silently, and a tolerated failure then overwrote its evidence (see above).
        evaluation["error"] = ("the sentence promises content but the run only saw the URL change "
                               "(no new heading, results or controls appeared)")
        evaluation["definite"] = True      # deterministic, so no benefit of the doubt (see ratings.derive_status)
    return evaluation


# ------------------------------------------------------------------ browser side

def take_snapshot(page) -> dict:
    def grab(selector: str, js: str) -> list:
        try:
            return page.locator(selector).evaluate_all(js)
        except Exception:
            return []
    headings = grab('h1,h2,h3,h4,h5,h6,[role="heading"]', _HEADINGS_JS)
    controls = grab(_CONTROL_SEL, _CONTROLS_JS)
    results = grab(_RESULTS_SEL, _RESULTS_JS)
    return {
        "url": page.url,
        "headings": sorted({h["text"][:80] for h in headings if h.get("text") and not h.get("hidden")}),
        "controls": sorted({f'{c.get("tag")}:{(c.get("name") or c.get("selector") or "")[:40]}'
                            for c in controls if not c.get("hidden") and c.get("region") != "chrome"}),
        "results": [{"key": p.get("selector") or p.get("klass") or "?", "rows": p.get("rows") or 0,
                     "in_main": bool(p.get("in_main"))} for p in results],
    }


def settled_snapshot(page) -> dict:
    """Snapshot the page after a step, once it has settled: wait the usual beat, then for spinners to
    go, then until two snapshots in a row agree. Content that loads a moment after the click (search
    results, a table) is then part of what the run observed instead of being missed."""
    page.wait_for_timeout(_STEP_WAIT_MS)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    wait_for_loaders(page, _LOADER_WAIT_MS)
    snapshot = take_snapshot(page)
    for _ in range(_SETTLE_ROUNDS):
        page.wait_for_timeout(_SETTLE_POLL_MS)
        again = take_snapshot(page)
        if snapshots_equal(snapshot, again):
            break
        snapshot = again
    return snapshot


def snapshots_equal(a: dict, b: dict) -> bool:
    return (_same_url(a["url"], b["url"]) and a["headings"] == b["headings"] and a["controls"] == b["controls"]
            and [(r["key"], r["rows"]) for r in a["results"]] == [(r["key"], r["rows"]) for r in b["results"]])


_KIND_TAGS = {"select": {"select"}, "fill": {"input", "textarea"}}   # what can stand in for these kinds of step
_RENAMED_RATIO = 0.75                                                 # how alike two control names must be


def _role_for(control: dict) -> str | None:
    """The ARIA role to find a control by. The explorer's own role field can hold things that are not roles
    (a button's input type, 'submit'), so it is only used when it is a real one."""
    role = str(control.get("role") or "").lower()
    if role in _ARIA_ROLES:
        return role
    tag, typ = control.get("tag"), (control.get("type") or "").lower()
    if tag == "a":
        return "link"
    if tag == "button" or (tag == "input" and typ in {"button", "submit", "reset"}):
        return "button"
    if tag == "input" and typ in {"checkbox", "radio"}:
        return typ
    if tag in {"input", "textarea"}:
        return "textbox"
    return None


def _related(stored: str, key: str, control_key: str) -> bool:
    """Same name. Only a name stored at the 40-character cut may match by its start; a short name like
    'Search' must not match 'Search results' (that is a different control, or a rename, handled below)."""
    return bool(control_key) and (control_key == key or (len(stored) >= 40 and control_key.startswith(key)))


def _candidate_key(control: dict):
    selector = None if control.get("volatile_id") else control.get("selector")
    return (selector, control["name"][:40], _role_for(control))


def find_replacement(step: dict, controls: list[dict]) -> tuple[dict | None, str]:
    """When a step's control can no longer be found, look at the controls the page has NOW and return
    ({selector, name, role}, how) if exactly one visible control can stand in for it, else (None, why).
    Deterministic and conservative: the same name with a new selector or role (ids get regenerated), or a
    name that is nearly the same (a button relabelled 'Search' -> 'Search now'). Several candidates, or
    none, means no healing: a wrong guess would make the test pass for the wrong reason."""
    name = (step.get("name") or "").strip()
    key = _same_heading_key(name)
    if not key:
        return None, "the step has no control name to look for"
    allowed = _KIND_TAGS.get(step.get("kind"))
    pool = [c for c in controls if c.get("name") and not c.get("hidden") and (not allowed or c.get("tag") in allowed)]

    same = list({_candidate_key(c): c for c in pool if _related(name, key, _same_heading_key(c["name"]))}.values())
    if len(same) > 1:
        return None, "several controls have that name: " + ", ".join(sorted({c["name"][:30] for c in same})[:4])
    if len(same) == 1:
        selector, cname, role = _candidate_key(same[0])
        if not selector and not role:
            return None, "the control is there but has neither a stable selector nor a role to find it by"
        if selector == step.get("selector") and role == step.get("role"):
            return None, "the same control is there; it failed for another reason"
        how = "same control, its selector or role changed" if step.get("selector") else "same control, found by another role"
        return {"selector": selector, "name": cname, "role": role}, how

    close = list({_candidate_key(c): c for c in pool if min(len(key), len(_same_heading_key(c["name"]))) >= 4
                  and SequenceMatcher(None, key, _same_heading_key(c["name"])).ratio() >= _RENAMED_RATIO}.values())
    if len(close) > 1:
        return None, "several controls look similar: " + ", ".join(sorted({c["name"][:30] for c in close})[:4])
    if len(close) == 1:
        selector, cname, role = _candidate_key(close[0])
        if not selector and not role:
            return None, "a similar control is there but has neither a stable selector nor a role to find it by"
        return {"selector": selector, "name": cname, "role": role}, f'control renamed (was "{name}")'
    return None, "no control on the page looks like it"


def _visible_controls(page) -> list[dict]:
    try:
        return page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)
    except Exception:
        return []


def _by_role(page, role: str, name: str):
    """Find by role and accessible name exactly as the generated spec does: the whole name, or - for a name stored
    cut at 40 characters - its start. (Substring matching here would let a renamed control pass verify while the
    spec, which matches exactly, fails.)"""
    if len(name) >= 40:
        return page.get_by_role(role, name=re.compile(re.escape(name))).first
    return page.get_by_role(role, name=name, exact=True).first


def _locate(page, step: dict):
    if step.get("selector"):
        loc = page.locator(step["selector"]).first
        return loc if loc.count() else None
    name = (step.get("name") or "").strip()
    if step.get("role") and name:                       # a role recorded by healing, or by whoever wrote the step
        loc = _by_role(page, step["role"], name)
        if loc.count():
            return loc
    for role in ("button", "link", "checkbox", "textbox"):
        loc = _by_role(page, role, name)
        if name and loc.count():
            return loc
    return None


def _real_options(loc) -> list[str]:
    """The labels of a <select>'s real options (placeholders like "Please select" left out), read from the live page."""
    try:
        opts = loc.evaluate("el => [...el.options].map(o => ({v: o.value, t: (o.textContent||'').trim()}))")
    except Exception:
        return []
    return [(o.get("t") or o.get("v") or "") for o in opts
            if (o.get("v") or "").strip().lower() not in _PLACEHOLDER_VAL and not _PLACEHOLDER_OPT.match(o.get("t") or "")
            and (o.get("t") or o.get("v"))]


def _do_step(page, step: dict, seen: dict | None = None) -> None:
    loc = _locate(page, step)
    if loc is None:
        raise RuntimeError("control not found")
    kind = step.get("kind")
    if kind == "select":
        if step.get("value"):
            try:
                loc.select_option(label=step["value"], timeout=2000)
                return
            except Exception:
                pass
        options = _real_options(loc)
        chosen = _select_first_real(loc)
        if chosen is None:
            raise RuntimeError("no selectable option")
        if seen is not None:
            seen.update(options=options, chosen=chosen)      # so a default that shows nothing can be swapped for another
    elif kind == "fill":
        loc.fill(str(step.get("value") or _plausible_value({})), timeout=2000)
    elif kind == "multiselect" and step.get("value"):
        pick_option(page, loc, str(step["value"]))   # open the widget and pick the NAMED option
    elif kind == "multiselect":
        try:
            loc.click(timeout=2000)
        except Exception:
            control = {"selector": step.get("selector"), "name": step.get("name"), "role": "button", "tag": "button"}
            if _pick_multiselect(page, control) is None:
                raise RuntimeError("could not pick an option")
    else:
        try:
            loc.click(timeout=3000)
        except Exception:
            _close_menus(page)
            loc.click(timeout=3000)


def run_flow(page, flow: dict, log=None, heal: bool = False, overrides: dict | None = None) -> dict:
    """Run a flow. With heal=True a step whose control is gone is retried with the one control on the page that can
    stand in for it (find_replacement); every heal is recorded in result["heals"] and only kept if the whole run
    passes (apply_result). With heal=False the failure names the control that looks like the missing one."""
    steps = [dict(s) for s in (flow.get("steps") or [])]
    for index, value in (overrides or {}).items():            # try_other_options: a specific option for a defaulted select
        if 0 <= index < len(steps):
            steps[index]["value"] = value
    result = {"ok": False, "steps_done": 0, "steps_total": len(steps), "error": None,
              "step_effects": [], "step_urls": [], "landed_url": None, "heals": [], "select_choices": {}}
    try:
        page.goto(flow["start_url"], wait_until="domcontentloaded")
        settle_page(page)
        dismiss_overlays(page)
    except Exception as exc:
        result["error"] = f"could not open start page: {str(exc).splitlines()[0][:120]}"
        result["unreachable"] = is_outage(str(exc))
        result["observed"] = classify(diff_snapshots(_empty(flow), _empty(flow)))
        return result
    first = previous = take_snapshot(page)
    result["landed_url"] = first["url"]  # where the start URL really ended up (it may redirect)
    for index, step in enumerate(steps):
        seen: dict = {}
        try:
            _do_step(page, step, seen)
            if seen.get("options"):
                result["select_choices"][index] = seen
        except Exception as exc:
            reason = str(exc).splitlines()[0][:120] if str(exc) else exc.__class__.__name__
            hint = ""
            if "control not found" in str(exc):
                replacement, how = find_replacement(step, _visible_controls(page))
                if replacement and heal:
                    healed = {**step, "selector": replacement["selector"], "name": replacement["name"], "role": replacement["role"]}
                    try:
                        _do_step(page, healed)
                        result["heals"].append({
                            "step": index + 1, "how": how,
                            "was": {"selector": step.get("selector"), "name": step.get("name"), "role": step.get("role")},
                            "now": {"selector": replacement["selector"], "name": replacement["name"], "role": replacement["role"]}})
                        reason = ""
                    except Exception as again:
                        detail = str(again).splitlines()[0][:60] if str(again) else "it failed too"
                        hint = f' (tried "{replacement["name"]}" instead: {detail})'
                elif replacement:
                    hint = (f' (the page has "{replacement["name"]}", which looks like it - {how}; '
                            "this flow was decided by a person, so it was not changed)")
                else:
                    hint = f" (nothing could stand in for it: {how})"
            if reason:
                result["error"] = (f'step "{describe_step(step)}" failed: {reason}{hint}'
                                   f'{hop_hint(step, index, page.url)}')
                break
        current = settled_snapshot(page)
        result["step_effects"].append(classify(diff_snapshots(previous, current))["effect"])
        result["step_urls"].append(current["url"])
        result["steps_done"] += 1
        previous = current
    result["ok"] = result["steps_done"] == len(steps) and not result["error"]
    result["observed"] = classify(diff_snapshots(first, previous))
    return result


def _empty(flow: dict) -> dict:
    return {"url": flow.get("start_url", ""), "headings": [], "controls": [], "results": []}


MAX_OPTION_TRIES = 4        # other options tried when the default one shows nothing


def try_other_options(flow: dict, result: dict, run_once, tries: int = MAX_OPTION_TRIES) -> dict:
    """A flow built from a sentence that promises content, whose select has no chosen value, ran with the default
    (first real) option and the page showed nothing: that combination may simply have no data (a filter that
    matches nothing). Try up to `tries` other real options, in order; the first run that passes is returned with
    the option recorded as a heal (kept only if the run passes, see apply_result), else the original result.
    run_once(overrides) -> a fresh run with {step index: option label}. Never used for a flow a person decided."""
    choices = result.get("select_choices") or {}
    for index in sorted(choices):
        step = (flow.get("steps") or [])[index] if index < len(flow.get("steps") or []) else {}
        if step.get("value"):
            continue
        default, options = choices[index].get("chosen"), choices[index].get("options") or []
        for option in [o for o in options if o != default][:tries]:
            try:
                trial = run_once({index: option})
            except Exception:
                continue
            steps = [dict(s, value=option) if i == index else s for i, s in enumerate(flow.get("steps") or [])]
            if judge(dict(flow, steps=steps), trial)["passed"]:
                trial["heals"] = list(trial.get("heals") or []) + [{
                    "kind": "option", "step": index + 1,
                    "how": f'the default option "{default}" showed no content; "{option}" does',
                    "was": {"value": None, "name": step.get("name")}, "now": {"value": option}}]
                return trial
        return result                    # only the first defaulted select is explored: one at a time
    return result


def select_flows(flows: list[dict], only: list[str] | None = None, failed_only: bool = False) -> tuple[list[dict], list[str]]:
    """Which flows `verify` should run, and any reference that matched nothing.
    Never a rejected flow, and never one waiting for its sentence to be rebuilt (see flows.is_blocked).
    `only` limits it to flows whose id equals or contains one of the given fragments; `failed_only` to
    flows that are not verified yet or went stale, i.e. the ones worth re-running after a site change."""
    todo = [f for f in flows if f.get("status") != "rejected" and not is_blocked(f)]
    if failed_only:
        todo = [f for f in todo if f.get("status") in {"candidate", "stale"}]
    unmatched: list[str] = []
    if only:
        chosen = []
        for ref in only:
            hits = [f for f in todo if ref == f["id"] or ref in f["id"]]
            if not hits:
                unmatched.append(ref)
            chosen += [f for f in hits if f not in chosen]
        todo = chosen
    return todo, unmatched


def run_verify(settings, log, only: list[str] | None = None, failed_only: bool = False) -> int:
    from playwright.sync_api import sync_playwright
    from .intents import sync_files
    sync_files(settings, log)                    # a flow whose sentence changed must not be re-verified as-is
    doc = load_flows(settings.flows_file)
    ratings = load_ratings(settings.ratings_file)
    todo, unmatched = select_flows(doc["flows"], only, failed_only)
    for ref in unmatched:
        log.error("verify: no runnable flow matches '%s' (see `flows list`; rejected flows and flows waiting for a "
                  "rebuilt sentence are skipped)", ref)
    if not todo:
        if not (only or failed_only):
            log.error("verify: no flows in %s - run explore or propose first", settings.flows_file)
        else:
            log.info("verify: nothing to run for this selection")
        return 2 if unmatched or not (only or failed_only) else 0
    verified = unreachable = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        try:
            for flow in todo:
                context = browser.new_context()
                page = context.new_page()
                page.set_default_navigation_timeout(settings.navigation_timeout_ms)
                try:
                    result = run_flow(page, flow, log, heal=flow.get("status") not in HUMAN_STATUSES)
                except Exception as exc:
                    result = {"ok": False, "steps_done": 0, "steps_total": len(flow.get("steps") or []),
                              "error": f"runner crashed: {str(exc).splitlines()[0][:120]}", "step_effects": [],
                              "observed": classify(diff_snapshots(_empty(flow), _empty(flow)))}
                finally:
                    context.close()
                if result.get("unreachable"):
                    unreachable += 1
                    log.warning("verify: %s - skipped: %s. The site could not be reached, which says nothing about the "
                                "flow, so nothing was recorded.", flow["id"], result["error"])
                    continue
                if flow.get("status") not in HUMAN_STATUSES and result.get("ok"):
                    verdict = judge(flow, result)
                    if verdict["promised"] and verdict["changed"]:  # not "and matched": see apply_result's comment
                        def run_once(overrides, flow=flow):
                            ctx = browser.new_context()
                            try:
                                trial_page = ctx.new_page()
                                trial_page.set_default_navigation_timeout(settings.navigation_timeout_ms)
                                return run_flow(trial_page, flow, log, heal=True, overrides=overrides)
                            finally:
                                ctx.close()
                        better = try_other_options(flow, result, run_once)
                        if better is not result:
                            log.info("verify: %s - %s", flow["id"], better["heals"][-1]["how"])
                        result = better
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                before = flow.get("status")
                evaluation = apply_result(flow, result, now)
                append_rating(ratings, flow["id"], evaluation)
                flow["status"] = derive_status(before, ratings["ratings"][flow["id"]])
                verified += evaluation["passed"]
                log.info("verify: %s -> %s (%s steps, observed %s%s)", flow["id"], flow["status"],
                         evaluation["checks"]["steps_completed"], evaluation["checks"]["observed_effect"],
                         f', {result["error"]}' if result.get("error") else "")
                save_flows(settings.flows_file, doc)
                save_ratings(settings.ratings_file, ratings)
        finally:
            browser.close()
    log.info("VERIFY SUMMARY flows=%d passed=%d unreachable=%d", len(todo), verified, unreachable)
    return 3 if unreachable == len(todo) else 0      # 3: the site could not be reached for any flow (nothing recorded)
