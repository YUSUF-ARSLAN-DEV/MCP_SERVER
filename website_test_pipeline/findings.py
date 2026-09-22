"""Findings: a QA-triage layer over a completed run.

A pass/fail count tells a reader WHAT happened. It does not say which failures are worth fixing the
test versus fixing the app, which "passing" flows are actually shaky, or what to look at first. This
module turns the run's own recorded facts - the assertion that failed, what the explorer/runner
actually observed about the control it names, and the flow's rating history - into a short, deterministic
triage: severity, kind, a one-line reproduction, and a next action. Nothing here re-runs a test, opens a
browser, or asks a model; every verdict is a rule over facts already on disk, named so a reader can check
it in one line instead of reading the whole trace.

Two kinds of failure are told apart automatically:
  - test_defect: the assertion itself is provably wrong given what was actually observed (it checks a
    role the page was never confirmed to have, or expects a URL to change right after a step that only
    selected/filled a field, never clicked or submitted).
  - flaky_data: the flow's own evaluation already recorded "definite": True for missing content (see
    runner.apply_result) - a live, content-dependent failure, not a locator problem.
Anything else is left "unclear" rather than guessed at, with the honest instruction to look at the trace.

A few more findings are not tied to any one failing test - they are deterministic checks over text and
pages the run already captured, regardless of pass/fail:
  - capture_corruption: some captured text contains the Unicode replacement character (U+FFFD), which a
    decoder only ever produces when a byte sequence could not be turned into text - proof something in the
    capture chain (or the source page itself) mangled it, never a guess.
  - localization_mismatch: a page's declared <html dir> does not match the writing direction its own
    captured text actually is (heuristics.dominant_script, detected from Unicode ranges - not from the
    page's own lang code or the run's URL pattern, either of which a site could get wrong).
"""
from __future__ import annotations
import ast
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SEVERITIES = ("P1", "P2")          # P1: a user journey (flow) is broken or unproven; P2: a page-level check
KINDS = ("test_defect", "flaky_data", "unclear", "capture_corruption", "localization_mismatch")
FLAP_WINDOW = 6                     # how many recent real executions are looked at per test/flow


@dataclass
class Finding:
    test: str
    scope: str            # "flow" | "page"
    url: str
    severity: str
    kind: str
    summary: str
    repro: str             # one line: what to run/open to see it again
    next_action: str


@dataclass
class FlapRecord:
    test: str
    scope: str
    sequence: str          # e.g. "F P P F P", oldest to newest, real executions only
    last_at: str
    note: str


def _first_error_line(error: str | None) -> str:
    match = re.search(r"^E\s+(.+)$", error or "", re.M)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()[:220]
    return (error or "").strip().splitlines()[0][:220] if error else ""


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


# ------------------------------------------------------------------ heuristic 1: a role the page never had

# Playwright's own failure message names the ACTUAL locator it was resolving as "- waiting for
# get_by_role(...)"; the rest of a pytest traceback usually also echoes the test's source code
# (every locator built before the one that failed), so searching the whole text for the first
# get_by_role(...) call finds a passing line's locator, not the failing one. Anchoring on "waiting
# for" first, and only falling back to a bare search when that is absent, is what makes this reliable.
_ROLE_WAITING_FOR = re.compile(r'waiting for get_by_role\(\s*["\']([^"\'\n]+)["\']\s*,\s*name\s*=\s*["\']([^"\'\n]*)["\']')
_ROLE_FROM_ERROR = re.compile(r'get_by_role\(\s*["\']([^"\'\n]+)["\']\s*,\s*name\s*=\s*["\']([^"\'\n]*)["\']')


def recorded_roles(name: str, inventories: list[dict], flow: dict | None = None) -> set[str | None]:
    """Every role recorded anywhere (any explored page, plus a flow's own steps) for a control whose
    name matches `name` (compared loosely - case, spacing, punctuation ignored)."""
    want = _norm(name)
    roles: set[str | None] = set()
    for inv in inventories:
        for c in inv.get("controls") or []:
            if want and (_norm(c.get("name") or "") == want or want in _norm(c.get("name") or "")):
                roles.add((c.get("role") or None) and str(c["role"]).lower())
        for entry in inv.get("revealed") or []:
            for c in entry.get("controls") or []:
                if want and (_norm(c.get("name") or "") == want or want in _norm(c.get("name") or "")):
                    roles.add((c.get("role") or None) and str(c["role"]).lower())
    if flow:
        for step in flow.get("steps") or []:
            if want and _norm(step.get("name") or "") == want:
                roles.add(step.get("role"))
    return roles


def role_mismatch_finding(error: str, inventories: list[dict], flow: dict | None = None) -> str | None:
    """None if the error is not a get_by_role lookup, else why it is (or is not) a confirmed test defect."""
    match = _ROLE_WAITING_FOR.search(error or "") or _ROLE_FROM_ERROR.search(error or "")
    if not match:
        return None
    role, name = match.group(1), match.group(2)
    if not name:
        return None
    seen = recorded_roles(name, inventories, flow)
    if not seen:
        return f'no explored page ever recorded a control named "{name}" at all'
    if role.lower() not in {r for r in seen if r}:
        shown = ", ".join(sorted(r or "(no role recorded)" for r in seen))
        return f'asserts role="{role}" for "{name}", but what was actually observed for it is: {shown}'
    return None


# ------------------------------------------------------------------ heuristic 2: URL assertion after a non-navigating step

def _action_lambda_source(call: ast.Call, source: str) -> str:
    # action_evidence(page, label, action, verify, evidence_dir) - action is the 3rd positional arg
    return (ast.get_source_segment(source, call.args[2]) or "") if len(call.args) > 2 else ""


def _verify_lambda_source(call: ast.Call, source: str) -> str:
    return (ast.get_source_segment(source, call.args[3]) or "") if len(call.args) > 3 else ""


def url_after_non_navigating_step(source: str) -> str | None:
    """None if no action_evidence step both asserts to_have_url and only selected/filled a field (never
    clicked or pressed) - else the one line explaining why that is a test defect, not an app bug."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "action_evidence"):
            continue
        verify = _verify_lambda_source(node, source)
        action = _action_lambda_source(node, source)
        if "to_have_url" not in verify or not action:
            continue
        if re.search(r"\.(click|press)\s*\(", action):
            continue
        if re.search(r"\.(select_option|fill|check|uncheck)\s*\(", action):
            return (f"asserts the URL changes right after `{action.strip()[:80]}`, which only selects or fills a "
                    "field - nothing in that step clicks or submits, so a URL change here would need a separate step")
    return None


# ------------------------------------------------------------------ heuristic 3: corrupted captured text

# U+FFFD is emitted ONLY when a decoder could not map a byte sequence to a character - a browser, pytest,
# or our own subprocess/file I/O actually failed to decode something. Unlike a mojibake byte-pattern guess
# (which a legitimately-accented word could trigger by coincidence), this one character is unambiguous.
_MOJIBAKE_RE = re.compile("�")


def mojibake_finding(test: str, scope: str, url: str, text: str) -> Finding | None:
    """None unless `text` contains the Unicode replacement character - else a Finding pointing at where."""
    match = _MOJIBAKE_RE.search(text or "")
    if not match:
        return None
    i = match.start()
    sample = re.sub(r"\s+", " ", text[max(0, i - 25):i + 25]).strip()
    return Finding(
        test=test, scope=scope, url=url, severity="P2", kind="capture_corruption",
        summary=f'captured text could not be decoded cleanly (U+FFFD) near: "...{sample}..."',
        repro=f'{test}: search the captured text for "\\ufffd"',
        next_action="check the capture path's text encoding (subprocess/stdout/file I/O); could also be a "
                    "real encoding bug on the source page - open it and compare")


# ------------------------------------------------------------------ heuristic 4: declared direction vs actual script

def direction_mismatch_finding(inv: dict) -> Finding | None:
    """None if the page's declared writing direction agrees with what its captured text actually is -
    else a Finding explaining the mismatch. A missing dir attribute is treated as the HTML default
    ("ltr"), same as a real browser does - nothing here is guessed beyond that spec default. Silent
    whenever the captured text is too thin to judge a script from at all (dominant_script == "unknown"),
    or when dir is something other than ltr/rtl (auto, or simply not a direction value)."""
    from . import heuristics
    script = heuristics.dominant_script(inv.get("accessibility") or "")
    if script == "unknown":
        return None
    declared = (inv.get("dir") or "ltr").strip().lower()
    if declared not in {"ltr", "rtl"} or declared == script:
        return None
    url = inv.get("url", "")
    return Finding(
        test=f"page direction: {url}", scope="page", url=url, severity="P2", kind="localization_mismatch",
        summary=f'the page declares dir="{declared}" but its captured text is predominantly {script}-script',
        repro=f'open {url} and compare <html dir="{declared}"> to the visible text',
        next_action="set dir to match the rendered content, or check whether this is an untranslated/fallback page")


# ------------------------------------------------------------------ building findings

def _spec_source(path: str | None) -> str:
    try:
        return Path(path).read_text(encoding="utf-8") if path else ""
    except OSError:
        return ""


def classify_failure(test_name: str, scope: str, url: str, error: str | None, spec_source: str,
                     inventories: list[dict], flow: dict | None = None, definite: bool = False) -> Finding:
    """One Finding for a failed or errored test/flow. Never raises; an unrecognised failure is 'unclear'."""
    severity = "P1" if scope == "flow" else "P2"
    line = _first_error_line(error)
    if definite:
        return Finding(test_name, scope, url, severity, "flaky_data",
                       "the flow completed every step, but the sentence's promised content never appeared",
                       f"{test_name}: {line}",
                       "data-dependent, not necessarily a defect - re-run, or try the journey with different input")
    why = role_mismatch_finding(error or "", inventories, flow)
    if why:
        return Finding(test_name, scope, url, severity, "test_defect", why, f"{test_name}: {line}",
                       "fix the locator: assert the role that was actually observed, or drop the role assertion")
    why = url_after_non_navigating_step(spec_source)
    if why:
        return Finding(test_name, scope, url, severity, "test_defect", why, f"{test_name}: {line}",
                       "fix the test: assert the URL only after a step that actually navigates")
    return Finding(test_name, scope, url, severity, "unclear", line or "failed with no captured reason",
                   f"{test_name}: {line}", "needs manual triage: open the screenshot/trace for this test")


def _last_definite(entries: list[dict] | None) -> bool:
    """Was the most recent runner evaluation a 'definite' failure (runner.apply_result) - a live,
    content-dependent result, not a locator problem."""
    runs = [e for e in (entries or []) if e.get("source") == "runner"]
    return bool(runs and runs[-1].get("definite"))


def collect_findings(run, tests_dir: Path, inventories: list[dict], flows_by_id: dict[str, dict],
                     ratings: dict[str, list[dict]] | None = None) -> list[Finding]:
    """Every failed/errored outcome in the run, as Findings, plus deterministic signals that are not tied
    to any one failing test - captured text that could not be decoded cleanly, wherever it turns up, and
    a page whose declared writing direction does not match its own captured text. `run` is a
    report.RunReport."""
    ratings = ratings or {}
    findings: list[Finding] = []
    for report in run.url_reports:
        source = _spec_source(report.spec_path)
        for outcome in report.outcomes:
            if outcome.status in {"failed", "error"}:
                findings.append(classify_failure(outcome.title, "page", report.url, outcome.error, source, inventories))
            corrupt = mojibake_finding(outcome.title, "page", report.url, outcome.error or "")
            if corrupt:
                findings.append(corrupt)
    for flow in run.tested_flows:
        flow_dict = flows_by_id.get(flow.flow_id)
        if flow.failed:
            spec_path = tests_dir / _flow_spec_name(flow.flow_id)
            definite = _last_definite(ratings.get(flow.flow_id))
            findings.append(classify_failure(flow.title, "flow", flow.start_url, flow.outcome.error,
                                             _spec_source(str(spec_path)), inventories, flow_dict, definite))
        corrupt = mojibake_finding(flow.title, "flow", flow.start_url, (flow.outcome.error or "") if flow.outcome else "")
        if corrupt:
            findings.append(corrupt)
    for inv in inventories:
        corrupt = mojibake_finding(f"page capture: {inv.get('url', '')}", "page", inv.get("url", ""),
                                   inv.get("accessibility") or "")
        if corrupt:
            findings.append(corrupt)
        mismatch = direction_mismatch_finding(inv)
        if mismatch:
            findings.append(mismatch)
    order = {"P1": 0, "P2": 1}
    return sorted(findings, key=lambda f: (order.get(f.severity, 9), f.test))


def _flow_spec_name(flow_id: str) -> str:
    from .flowgen import _slug
    return "flow_" + _slug(flow_id, 80).replace("-", "_") + "_test.py"


# ------------------------------------------------------------------ suite stability (flapping)

def detect_flapping(ratings: dict[str, list[dict]], names: dict[str, str] | None = None,
                    window: int = FLAP_WINDOW) -> list[FlapRecord]:
    """Flows/tests whose recent REAL executions (runner/pytest, not model opinions) mix passes and
    failures - a green checkmark next to one of these is provisional, not settled."""
    names = names or {}
    out = []
    for test_id, entries in ratings.items():
        runs = [e for e in entries if e.get("source") in {"runner", "pytest"}][-window:]
        seq = ["P" if e.get("passed") else "F" for e in runs]
        if len(set(seq)) > 1:
            reasons = {e.get("error") for e in runs if not e.get("passed") and e.get("error")}
            note = ("content-dependent: " + next(iter(reasons))[:100]) if reasons else "cause not recorded on the failing runs"
            out.append(FlapRecord(names.get(test_id, test_id), "flow", " ".join(seq), runs[-1].get("at", ""), note))
    return sorted(out, key=lambda f: f.test)


# ------------------------------------------------------------------ environment

def environment_block(run, settings=None) -> dict[str, str]:
    return {
        "Browser": "Chromium (Playwright)",
        "Headless": str(getattr(settings, "headless", True)),
        "OS": platform.platform(),
        "Python": sys.version.split()[0],
        "Site": run.base_url or (getattr(settings, "seed_url", "") or ""),
        "Model": run.model or "(none used this run)",
        "Run window": f"{run.started_at or '?'} - {run.finished_at or '?'}",
    }
