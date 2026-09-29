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

from .flows import describe_step, is_blocked
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
    title_suffix: str = ""             # set when several flows share one sentence, so a reader can tell them apart
    not_run_reason: str = ""           # why an untested flow has no result in this run (empty when it ran)
    verify_failed: bool = False        # untested, and the last real check of it (runner or pytest) failed
    inconclusive: bool = False         # untested; its steps all ran but nothing visible changed, so the outcome could not be confirmed

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
        return (self.goal or self.flow_id) + self.title_suffix

    @property
    def run_label(self) -> str:
        """One plain word for this run's result. "Blocked" is not used: a flow with no result either failed its
        last check or was simply not run, and those are different things to a reader."""
        if self.outcome is not None:
            return {"passed": "Passed", "skipped": "Skipped"}.get(self.outcome.status, "Failed")
        if self.inconclusive:
            return "Inconclusive (steps ran, outcome not observable)"
        return "Failed its last check" if self.verify_failed else "Not run in this run"


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
    if outcome is None:
        fr.not_run_reason, fr.verify_failed = explain_not_run(flow, entries)
        executions = [e for e in entries if e.get("source") in {"runner", "pytest"}]
        newest = executions[-1] if executions else None
        fr.inconclusive = (ran_without_visible_effect(newest) or promised_content_missing(newest)) and not is_blocked(flow)
    fr.warnings = flow_warnings(fr)
    return fr


def promised_content_missing(entry: dict | None) -> bool:
    """The last real check did everything the sentence says and the site simply showed no content for that input
    (recorded as definite: "promises content but ... only saw the URL change"). Data-dependent, not a broken page."""
    return bool(entry and not entry.get("passed") and entry.get("definite") and "promises content" in str(entry.get("error") or ""))


def ran_without_visible_effect(entry: dict | None) -> bool:
    """The last real check ran every step without an error, and the page showed no change at all. That says the
    tool could not observe the promised outcome (a tab-style button that only switches something already on the
    page, say) - it does not say the site failed."""
    if not entry or entry.get("passed") or entry.get("error"):
        return False
    checks = entry.get("checks") or {}
    done, _, total = str(checks.get("steps_completed", "")).partition("/")
    return bool(done) and done == total and checks.get("observed_effect") == "no-visible-change"


def explain_not_run(flow: dict, entries: list[dict]) -> tuple[str, bool]:
    """(why a flow has no test result in this run, whether its last real check failed). A stored "verified" next to
    "not run" is not a contradiction: verified is what was true at the last check, and this run did not repeat it."""
    executions = [e for e in entries if e.get("source") in {"runner", "pytest"}]
    last = executions[-1] if executions else None
    if is_blocked(flow):
        return "its plain sentence was edited or dropped since it was built; rebuild it before it can be tested", False
    if promised_content_missing(last):
        return ("every step ran, but the site showed no content for this input (for example a country and channel with no "
                "frequency data); try another input - the site itself did not fail"), False
    if ran_without_visible_effect(last):
        return ("every step ran, but nothing on the page visibly changed, so the promised outcome could not be confirmed; "
                "the site did not fail, the check could not observe an effect"), False
    if last is not None and not last.get("passed"):
        return f'its last check ({last.get("source")}, {(last.get("at") or "")[:10]}) failed', True
    if flow.get("status") in {"verified", "approved"}:
        return "it was verified earlier, but no test of it ran in this run (no generated test, or it was skipped)", False
    return "it has not been verified yet", False


def disambiguate_titles(reports: list[FlowReport]) -> None:
    """Flows recorded from different pages can carry the same sentence ("Search: select ..."). Say where each starts
    (and its number, if that is still not enough) so the report never lists what looks like one flow several times."""
    groups: dict[str, list[FlowReport]] = {}
    for fr in reports:
        groups.setdefault(fr.goal or fr.flow_id, []).append(fr)
    for group in groups.values():
        if len(group) < 2:
            continue
        for fr in group:
            fr.title_suffix = f" (starts at {_path(fr.start_url) or fr.start_url})"
        seen: dict[str, int] = {}
        for fr in group:
            seen[fr.title] = seen.get(fr.title, 0) + 1
        counter: dict[str, int] = {}
        for fr in group:
            if seen[fr.title] > 1:
                base = fr.title
                counter[base] = counter.get(base, 0) + 1
                fr.title_suffix += f" #{counter[base]}"


def _stem(path: str) -> str:
    return re.split(r"[\\/]", path)[-1].rsplit(".", 1)[0]
