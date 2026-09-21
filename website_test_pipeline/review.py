"""Human review of flows from the command line: list, show, approve, reject, reset
(and, in intents.py, add / edit / drop / intents for the plain-sentence file).

A person's decision is the one thing the tool never overwrites: approve/reject set the
flow's status (verify, propose, explore and flowgen all leave those two alone) and add a
source=human entry to flow_ratings.json holding who decided, when and why. `reset` hands
the flow back to the tool, which then derives its status from the recorded runs again.
"""
from __future__ import annotations
import getpass
import sys
from datetime import datetime, timezone

from .flows import FlowsFileError, describe_step, load_flows, save_flows
from .ratings import RatingsFileError, append_rating, derive_status, load_ratings, save_ratings

DECISIONS = {"approve": "approved", "reject": "rejected"}
# Every `flows <word>` the CLI accepts (add/edit/drop/intents are in intents.py, run is in cli.py); the docs are tested against this.
SUBCOMMANDS = ("list", "show", "coverage", "approve", "reject", "reset", "add", "edit", "drop", "intents", "run")


class ReviewError(Exception):
    """A user-facing problem (unknown flow, missing reason); the message says what to do."""


# ------------------------------------------------------------------ finding and summarising

def find_flow(doc: dict, ref: str) -> dict:
    """The flow whose id equals `ref`, else the only one whose id contains it (ids are long)."""
    ref = (ref or "").strip()
    if not ref:
        raise ReviewError("give a flow id (see `flows list`)")
    flows = doc["flows"]
    exact = [f for f in flows if f.get("id") == ref]
    if exact:
        return exact[0]
    partial = [f for f in flows if ref in f.get("id", "")]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ReviewError(f'no flow matches "{ref}" (see `flows list`)')
    raise ReviewError(f'"{ref}" matches {len(partial)} flows, be more specific:\n  '
                      + "\n  ".join(f["id"] for f in partial))


def last_result(entries: list[dict]) -> str:
    """One word-pair for the most recent real execution, e.g. 'pytest pass'."""
    for entry in reversed(entries):
        if entry.get("source") in {"runner", "pytest"}:
            return f'{entry["source"]} {"pass" if entry.get("passed") else "FAIL"}'
    return "not run"


def _clip(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 3] + "..."


def render_list(doc: dict, ratings: dict, status: str | None = None) -> str:
    flows = [f for f in doc["flows"] if status in (None, f.get("status"))]
    if not flows:
        return "no flows" + (f" with status {status}" if status else "")
    rows = [(f["id"], f.get("status", "?"), last_result(ratings["ratings"].get(f["id"], [])), _clip(f.get("goal", ""), 60) + (f' [sentence {f["intent_state"]}]' if f.get("intent_state") else ""))
            for f in flows]
    widths = [max(len(r[i]) for r in rows + [("ID", "STATUS", "LAST RUN", "")]) for i in range(3)]
    header = f'{"ID":<{widths[0]}}  {"STATUS":<{widths[1]}}  {"LAST RUN":<{widths[2]}}  GOAL'
    lines = [f"{a:<{widths[0]}}  {b:<{widths[1]}}  {c:<{widths[2]}}  {d}" for a, b, c, d in rows]
    counts: dict[str, int] = {}
    for f in flows:
        counts[f.get("status", "?")] = counts.get(f.get("status", "?"), 0) + 1
    return "\n".join([header, *lines, "", ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))])


def _rating_line(entry: dict) -> str:
    source = entry.get("source", "?")
    if source == "model":
        s = entry.get("scores") or {}
        return (f'[model {entry.get("prompt_version", "")}] coherence {s.get("coherence")} importance '
                f'{s.get("importance")} outcome {s.get("outcome_strength")} - {_clip(entry.get("reason", ""), 110)}')
    if source == "runner":
        c = entry.get("checks") or {}
        healed = "".join(f' | healed step {h["step"]}: {h["how"]}'
                         + ("" if h.get("kind") == "option" else f' ({h["was"].get("name")} -> {h["now"].get("name")})')
                         for h in entry.get("healed") or [])
        return (f'[runner] {"pass" if entry.get("passed") else "FAIL"} steps {c.get("steps_completed")} '
                f'observed {c.get("observed_effect")}' + healed + (f' - {entry["error"]}' if entry.get("error") else ""))
    if source == "pytest":
        return (f'[pytest] {"pass" if entry.get("passed") else "FAIL"}'
                + (f' - {_clip(entry.get("error", ""), 110)}' if entry.get("error") else ""))
    if source == "human":
        return f'[human {entry.get("by", "")}] {entry.get("decision")}' + (f' - {entry["reason"]}' if entry.get("reason") else "")
    return f"[{source}]"


def render_show(flow: dict, entries: list[dict]) -> str:
    out = [f'{flow["id"]}', f'  goal:     {flow.get("goal", "")}',
           f'  status:   {flow.get("status")}   source: {flow.get("source")}',
           f'  start:    {flow.get("start_url")}']
    if flow.get("intent_id"):
        out.append(f'  intent:   {flow["intent_id"]} (from the plain-sentence file; the goal above is that sentence)')
    out.append("  steps:")
    out += [f"    {i}. {describe_step(s)}" for i, s in enumerate(flow.get("steps") or [], 1)]
    predicted = flow.get("outcome") or {}
    out.append(f'  expected: {predicted.get("effect")}' + (f' -> {predicted["to"]}' if predicted.get("to") else ""))
    observed = flow.get("observed")
    if observed:
        out.append(f'  observed: {observed.get("effect")} at {observed.get("url")}  (last run {flow.get("last_run_at")})')
        if observed.get("new_headings"):
            out.append("    new headings: " + " | ".join(observed["new_headings"]))
        if observed.get("step_urls"):
            out.append("    step urls: " + " -> ".join(observed["step_urls"]))
    else:
        out.append("  observed: never run (use `verify`)")
    if flow.get("heal_history"):
        out.append("  healed (the tool re-found a control that moved or was renamed; the old values are kept here):")
        for heal in flow["heal_history"]:
            was, now = heal.get("was") or {}, heal.get("now") or {}
            if heal.get("kind") == "option":
                out.append(f'    {heal.get("at", "")[:19]}  step {heal.get("step")}: {heal.get("how")}')
            else:
                out.append(f'    {heal.get("at", "")[:19]}  step {heal.get("step")}: {heal.get("how")} - '
                           f'was {was.get("selector") or was.get("name")!r}, now {now.get("selector") or now.get("name")!r}')
    out.append("  history:")
    out += [f"    {e.get('at', '')[:19]}  {_rating_line(e)}" for e in entries] or ["    (none)"]
    return "\n".join(out)


# ------------------------------------------------------------------ decisions

def decide(doc: dict, ratings: dict, ref: str, action: str, reason: str = "", by: str = "human", now: str = "") -> dict:
    """approve / reject / reset a flow. Returns the flow. Raises ReviewError when it cannot."""
    if action not in {*DECISIONS, "reset"}:
        raise ReviewError(f"unknown action {action!r}")
    flow = find_flow(doc, ref)
    reason = (reason or "").strip()
    if action == "reject" and not reason:
        raise ReviewError("rejecting needs a reason: --reason \"why this flow is wrong\"")
    entries = ratings["ratings"].setdefault(flow["id"], [])
    if action in DECISIONS:
        flow["status"] = DECISIONS[action]
        decision = DECISIONS[action]
    else:
        decision = "reset"
        flow["status"] = derive_status("candidate", entries)   # back to the tool: re-derived from its recorded runs
    entry = {"source": "human", "at": now, "by": by, "decision": decision}
    if reason:
        entry["reason"] = reason
    append_rating(ratings, flow["id"], entry)
    return flow


# ------------------------------------------------------------------ command

def safe_print(text: str) -> None:
    """print() that cannot crash on a console whose encoding lacks a character in a goal (e.g. Arabic on cp1252)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding))


def run_flows_command(settings, words: list[str], reason: str = "", status: str | None = None, out=safe_print) -> int:
    """`flows list|show|approve|reject|reset ...`. Prints to `out`; returns an exit code."""
    action = words[0] if words else "list"
    rest = words[1:]
    try:
        if action in {"add", "edit", "drop", "intents"}:      # the plain-sentence file, not flows.json
            from .intents import run_intent_action
            return run_intent_action(settings, action, rest, reason, out)
        doc = load_flows(settings.flows_file)
        ratings = load_ratings(settings.ratings_file)
        if action == "list":
            out(render_list(doc, ratings, status))
            return 0
        if action == "coverage":
            out(_coverage_text(settings, doc, rest))
            return 0
        if action == "show":
            flow = find_flow(doc, rest[0] if rest else "")
            out(render_show(flow, ratings["ratings"].get(flow["id"], [])))
            return 0
        if action in {*DECISIONS, "reset"}:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            flow = decide(doc, ratings, rest[0] if rest else "", action, reason, _user(), now)
            save_flows(settings.flows_file, doc)
            save_ratings(settings.ratings_file, ratings)
            out(f'{flow["id"]} -> {flow["status"]}')
            return 0
        raise ReviewError(f"unknown flows command {action!r}: use {', '.join(SUBCOMMANDS)}")
    except ReviewError as exc:
        out(f"flows: {exc}")
        return 2
    except (FlowsFileError, RatingsFileError) as exc:
        out(f"flows: {exc}")
        return 1


def _coverage_text(settings, doc: dict, rest: list[str]) -> str:
    """`flows coverage [N]`: what the tested flows touch, and up to N untouched controls per page."""
    from .coverage import compute_coverage, render_coverage
    from .sitemap import load_inventories
    inventories = load_inventories(settings.artifacts_dir)
    try:
        from .urls import read_urls
        wanted = set(read_urls(settings.urls_file))
    except Exception:
        wanted = set()
    if wanted:
        inventories = [i for i in inventories if i.get("url") in wanted]
    limit = int(rest[0]) if rest and rest[0].isdigit() else 4
    return render_coverage(compute_coverage(inventories, doc["flows"]), limit)


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "human"
