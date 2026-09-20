"""Generic flow runner: executes each flow in flows.json step by step in a real browser,
takes a DOM snapshot after every step, and diffs it against the one before. What the
diffs show - not what a model predicted - is recorded as the flow's observed outcome.

A flow becomes verified only when every step ran AND the observed outcome matches the
predicted one AND something visibly changed. Each run appends a mechanical evaluation
to flow_ratings.json. Human decisions (approved / rejected) are never changed.
"""
from __future__ import annotations
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .explorer import (
    _CONTROL_SEL, _CONTROLS_JS, _HEADINGS_JS, _RESULTS_JS, _RESULTS_SEL,
    _close_menus, _pick_multiselect, _plausible_value, _select_first_real,
)
from .flows import HUMAN_STATUSES, describe_step, load_flows, save_flows
from .pageutils import dismiss_overlays, settle_page
from .ratings import append_rating, derive_status, load_ratings, save_ratings

_STEP_WAIT_MS = 1200
_MAX_LIST = 8


# ------------------------------------------------------------------ pure helpers

def _path(url: str) -> str:
    return (urlsplit(url or "").path or "/").rstrip("/") or "/"


def _same_url(a: str, b: str) -> bool:
    return (a or "").split("#")[0].rstrip("/") == (b or "").split("#")[0].rstrip("/")


def diff_snapshots(before: dict, after: dict) -> dict:
    seen_h, seen_c = set(before["headings"]), set(before["controls"])
    seen_r = {(r["key"], r["rows"]) for r in before["results"]}
    return {
        "url_changed": not _same_url(before["url"], after["url"]),
        "url": after["url"],
        "new_headings": [h for h in after["headings"] if h not in seen_h][:_MAX_LIST],
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
        return got == "results" or bool(observed.get("results"))
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


def apply_result(flow: dict, result: dict, now: str) -> dict:
    """Update the flow from a run and return the evaluation entry for flow_ratings.json."""
    observed = result["observed"]
    matched = result["ok"] and outcome_matches(flow.get("outcome") or {}, observed)
    changed = observed["effect"] != "no-visible-change"
    passed = bool(result["ok"] and matched and changed)
    flow["observed"] = {**observed, "step_effects": result["step_effects"],
                        "landed_url": result.get("landed_url"), "step_urls": result.get("step_urls", [])}
    flow["last_run_at"] = now
    if flow.get("status") not in HUMAN_STATUSES:
        flow["status"] = "verified" if passed else "candidate"
    evaluation = {
        "source": "runner", "at": now, "passed": passed,
        "checks": {"steps_completed": f'{result["steps_done"]}/{result["steps_total"]}',
                   "outcome_matched": matched, "observed_effect": observed["effect"]},
    }
    if result.get("error"):
        evaluation["error"] = result["error"]
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


def _locate(page, step: dict):
    if step.get("selector"):
        loc = page.locator(step["selector"]).first
        return loc if loc.count() else None
    name = (step.get("name") or "").strip()
    for role in ("button", "link", "checkbox", "textbox"):
        loc = page.get_by_role(role, name=name, exact=False).first  # names are stored cut to 40 chars
        if name and loc.count():
            return loc
    return None


def _do_step(page, step: dict) -> None:
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
        if _select_first_real(loc) is None:
            raise RuntimeError("no selectable option")
    elif kind == "fill":
        loc.fill(str(step.get("value") or _plausible_value({})), timeout=2000)
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


def run_flow(page, flow: dict, log=None) -> dict:
    steps = flow.get("steps") or []
    result = {"ok": False, "steps_done": 0, "steps_total": len(steps), "error": None,
              "step_effects": [], "step_urls": [], "landed_url": None}
    try:
        page.goto(flow["start_url"], wait_until="domcontentloaded")
        settle_page(page)
        dismiss_overlays(page)
    except Exception as exc:
        result["error"] = f"could not open start page: {str(exc).splitlines()[0][:120]}"
        result["observed"] = classify(diff_snapshots(_empty(flow), _empty(flow)))
        return result
    first = previous = take_snapshot(page)
    result["landed_url"] = first["url"]  # where the start URL really ended up (it may redirect)
    for index, step in enumerate(steps):
        try:
            _do_step(page, step)
        except Exception as exc:
            result["error"] = (f'step "{describe_step(step)}" failed: {str(exc).splitlines()[0][:120]}'
                               f'{hop_hint(step, index, page.url)}')
            break
        page.wait_for_timeout(_STEP_WAIT_MS)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        current = take_snapshot(page)
        result["step_effects"].append(classify(diff_snapshots(previous, current))["effect"])
        result["step_urls"].append(current["url"])
        result["steps_done"] += 1
        previous = current
    result["ok"] = result["steps_done"] == len(steps) and not result["error"]
    result["observed"] = classify(diff_snapshots(first, previous))
    return result


def _empty(flow: dict) -> dict:
    return {"url": flow.get("start_url", ""), "headings": [], "controls": [], "results": []}


def run_verify(settings, log) -> int:
    from playwright.sync_api import sync_playwright
    doc = load_flows(settings.flows_file)
    ratings = load_ratings(settings.ratings_file)
    todo = [f for f in doc["flows"] if f.get("status") != "rejected"]
    if not todo:
        log.error("verify: no flows in %s - run explore or propose first", settings.flows_file)
        return 2
    verified = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        try:
            for flow in todo:
                context = browser.new_context()
                page = context.new_page()
                page.set_default_navigation_timeout(settings.navigation_timeout_ms)
                try:
                    result = run_flow(page, flow, log)
                except Exception as exc:
                    result = {"ok": False, "steps_done": 0, "steps_total": len(flow.get("steps") or []),
                              "error": f"runner crashed: {str(exc).splitlines()[0][:120]}", "step_effects": [],
                              "observed": classify(diff_snapshots(_empty(flow), _empty(flow)))}
                finally:
                    context.close()
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
    log.info("VERIFY SUMMARY flows=%d passed=%d", len(todo), verified)
    return 0
