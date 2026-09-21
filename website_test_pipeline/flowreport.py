"""What the Word report says about a user flow, without any document code.

A flow's generated test is one pytest test, so on its own it reads like a page check. Here it
is put back together with everything known about the journey: the plain sentence it came from,
the steps and the page each one landed on, what `verify` expected and observed, the recorded
history (model, runner, pytest, human), and - when the test failed - which step and page broke.

The report layer (report.py) turns a FlowReport into paragraphs; keeping this part pure makes the
attribution testable without opening a browser or a .docx.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any

from .flows import describe_step
from .review import _rating_line
from .runner import _path

_STEP_STEM = re.compile(r"^(\d{2})-")
_OUTCOME_STEM = 99


@dataclass
class FlowReport:
    flow_id: str
    goal: str
    status: str                        # the flow's status: verified | approved | stale | candidate | rejected
    start_url: str
    intent_id: str | None = None
    steps: list[str] = field(default_factory=list)          # described steps
    lands: list[str] = field(default_factory=list)          # page each step ended on (may be empty for old runs)
    pages: list[str] = field(default_factory=list)          # distinct pages touched, in order
    expected: str = ""
    observed: str = ""
    new_headings: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    outcome: Any = None                # the TestOutcome of its generated test, or None if it was not run
    navigation_only: bool = False      # the outcome proves the URL changed and nothing about what the page shows
    failure: str = ""                  # which step / page broke, when the test failed
    warnings: list[str] = field(default_factory=list)
    vision_broken: bool = False        # the latest vision rating says the final screenshot looks broken (an opinion)
    vision_reason: str = ""

    @property
    def tested(self) -> bool:
        return self.outcome is not None

    @property
    def passed(self) -> bool:
        return bool(self.outcome and self.outcome.passed)

    @property
    def failed(self) -> bool:
        return bool(self.outcome and self.outcome.status in {"failed", "error"})

    @property
    def title(self) -> str:
        return self.goal or self.flow_id


def pages_touched(flow: dict) -> list[str]:
    """Distinct pages the journey visited, in order: the start page, then where each step ended."""
    observed = flow.get("observed") or {}
    urls = [observed.get("landed_url") or flow.get("start_url") or ""] + list(observed.get("step_urls") or [])
    pages: list[str] = []
    for url in urls:
        page = _path(url) if url else ""
        if page and (not pages or pages[-1] != page):
            pages.append(page)
    return pages


def completed_steps(stems: list[str]) -> int:
    """How many step screenshots exist. action_evidence photographs a step only after its action AND its
    check succeeded, so the first missing number is the step that broke."""
    numbers = {int(m.group(1)) for s in stems if (m := _STEP_STEM.match(s))}
    numbers.discard(_OUTCOME_STEM)
    done = 0
    while done + 1 in numbers:
        done += 1
    return done


def _reason(error: str | None) -> str:
    found = re.search(r"^E\s+(.+)$", error or "", re.M)
    return re.sub(r"\s+", " ", found.group(1)).strip()[:220] if found else ""


def attribute_failure(flow: dict, stems: list[str], error: str | None, status: str = "failed") -> str:
    """One sentence saying which step or page broke, from the evidence that exists and the error text."""
    steps = flow.get("steps") or []
    if status == "error":
        return "The test did not start (setup error); no step ran."
    done = completed_steps(stems)
    if done >= len(steps):
        text = f"All {len(steps)} steps ran, but the final outcome check failed."
    else:
        text = f"Step {done + 1} of {len(steps)} did not complete: {describe_step(steps[done])}."
        urls = (flow.get("observed") or {}).get("step_urls") or []
        if done < len(urls):
            text += f" It should have ended on {_path(urls[done])}."
    actual = re.search(r"Actual value:\s*(\S+)", error or "")
    if actual:
        text += f" The browser was on {_path(actual.group(1))}."
    reason = _reason(error)
    return f"{text} Reason: {reason}" if reason else text


def flow_warnings(fr: FlowReport) -> list[str]:
    """Things a reader cannot see from the numbers alone."""
    out = []
    if fr.tested and fr.passed and not fr.outcome.evidence:
        out.append(f"flow \"{fr.title}\": PASSED with no screenshot evidence")
    if fr.failed and not fr.outcome.evidence and not fr.outcome.attachments:
        out.append(f"flow \"{fr.title}\": FAILED with no evidence or trace recorded")
    if fr.failed and fr.status == "verified":
        out.append(f"flow \"{fr.title}\" failed this run but is still verified (one failure is tolerated; "
                   "a second in a row makes it stale)")
    if fr.tested and fr.passed and fr.navigation_only:
        out.append(f"flow \"{fr.title}\": passed, but its outcome only proves the URL changed - no heading, results or "
                   "new controls were observed to check what the page shows")
    if fr.vision_broken:
        out.append(f'flow "{fr.title}": the vision reviewer thinks the final screenshot looks broken ({fr.vision_reason}) - '
                   "a reason to look at it, not a test failure")
    if fr.status == "stale":
        out.append(f"flow \"{fr.title}\" is stale: it failed repeatedly - re-run verify or rebuild it")
    return out


def build_flow_report(flow: dict, entries: list[dict], outcome=None) -> FlowReport:
    observed = flow.get("observed") or {}
    predicted = flow.get("outcome") or {}
    urls = list(observed.get("step_urls") or [])
    steps = flow.get("steps") or []
    fr = FlowReport(
        flow_id=flow["id"], goal=flow.get("goal", ""), status=flow.get("status", "?"), start_url=flow.get("start_url", ""),
        intent_id=flow.get("intent_id"), steps=[describe_step(s) for s in steps],
        lands=[_path(u) for u in urls] if len(urls) == len(steps) else [],
        pages=pages_touched(flow),
        expected=(predicted.get("effect") or "") + (f' -> {predicted["to"]}' if predicted.get("to") else ""),
        observed=(observed.get("effect") or "") + (f' at {_path(observed["url"])}' if observed.get("url") else ""),
        new_headings=list(observed.get("new_headings") or []),
        history=[f"{(e.get('at') or '')[:19]}  {_rating_line(e)}" for e in entries],
        outcome=outcome,
        navigation_only=(observed.get("effect") == "navigates" and not observed.get("new_headings")
                         and not observed.get("results") and not observed.get("new_controls")),
    )
    latest = next((e for e in reversed(entries) if e.get("source") == "vision"), None)
    if latest and latest.get("verdict") == "looks_broken":
        fr.vision_broken, fr.vision_reason = True, str(latest.get("reason") or "")[:160]
    if outcome is not None and fr.failed:
        stems = [_stem(p) for p in outcome.evidence]
        fr.failure = attribute_failure(flow, stems, outcome.error, outcome.status)
    fr.warnings = flow_warnings(fr)
    return fr


def _stem(path: str) -> str:
    return re.split(r"[\\/]", path)[-1].rsplit(".", 1)[0]
