"""Which parts of the explored site the tested flows actually touch.

Flows are journeys, so "coverage" here is not lines of code: it is how much of what the explorer found
on the site a verified or approved flow actually visits and acts on. That answers the question a QA
lead asks after the first run - "what is still untested?" - and gives `intents` / `propose` a list of
places worth writing the next journeys for.

  page visited     a tested flow starts on it, lands on it, or acts on it
  control touched  a tested flow's step targets it (by selector, or by name)

Only what a page offers in its content counts towards the totals: links, buttons, selects and text
fields that are visible and enabled. Site chrome (header, nav, footer) is left out, because the same
links repeat on every page and are already checked by the page tests. Nothing here is specific to one
site: it reads the inventories and flows.json the pipeline already writes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from urllib.parse import urlsplit

TESTED = {"verified", "approved"}            # a flow with a real test behind it
_TAGS = {"a", "button", "select", "input", "textarea"}
_SKIPPED_INPUT_TYPES = {"hidden"}
_NAME_CUT = 40                                # flows store control names cut to 40 characters


@dataclass
class PageCoverage:
    path: str
    url: str
    visited: bool = False
    total: int = 0
    touched: int = 0
    untouched: list[str] = field(default_factory=list)      # names of content controls no tested flow acts on
    flows: list[str] = field(default_factory=list)          # ids of tested flows that visit this page

    @property
    def percent(self) -> int:
        return round(100 * self.touched / self.total) if self.total else 0


@dataclass
class Coverage:
    pages: list[PageCoverage] = field(default_factory=list)
    tested_flows: int = 0
    planned_flows: int = 0          # candidate or stale: written but not (or no longer) backed by a passing run

    @property
    def pages_total(self) -> int:
        return len(self.pages)

    @property
    def pages_visited(self) -> int:
        return sum(p.visited for p in self.pages)

    @property
    def controls_total(self) -> int:
        return sum(p.total for p in self.pages)

    @property
    def controls_touched(self) -> int:
        return sum(p.touched for p in self.pages)

    @property
    def percent_pages(self) -> int:
        return round(100 * self.pages_visited / self.pages_total) if self.pages_total else 0

    @property
    def percent_controls(self) -> int:
        return round(100 * self.controls_touched / self.controls_total) if self.controls_total else 0


def _path(url: str) -> str:
    return (urlsplit(url or "").path or "/").rstrip("/") or "/"


def _key(text: str) -> str:
    """A name compared ignoring case, spacing and punctuation (unicode aware)."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def actionable_controls(inventory: dict) -> list[dict]:
    """The content controls of a page that a journey could act on, each once (pages repeat a control in
    a desktop and a mobile menu)."""
    seen, out = set(), []
    for c in inventory.get("controls") or []:
        if c.get("tag") not in _TAGS or c.get("region") == "chrome" or c.get("hidden") or c.get("disabled"):
            continue
        if c.get("tag") == "input" and (c.get("type") or "").lower() in _SKIPPED_INPUT_TYPES:
            continue
        if c.get("tag") == "a" and not c.get("href"):
            continue
        if not (c.get("name") or c.get("selector")):
            continue
        ident = (c.get("selector") or "", _key(c.get("name")), c.get("tag"))
        if ident not in seen:
            seen.add(ident)
            out.append(c)
    return out


def step_pages(flow: dict) -> list[str]:
    """The page each step of a flow happened on: the page the start URL landed on for the first step, then
    where the previous step ended. Flows written before the runner recorded URLs fall back to their start page."""
    observed = flow.get("observed") or {}
    steps = flow.get("steps") or []
    urls = list(observed.get("step_urls") or [])
    first = _path(observed.get("landed_url") or flow.get("start_url") or "")
    if len(urls) == len(steps):
        return [first] + [_path(u) for u in urls[:-1]] if steps else []
    return [first] * len(steps)


def _touches(step: dict, control: dict) -> bool:
    if step.get("selector") and step["selector"] == control.get("selector"):
        return True
    want, have = _key(step.get("name")), _key(control.get("name"))
    if not want or not have:
        return False
    if have == want:
        return True
    return len(step.get("name") or "") >= _NAME_CUT and have.startswith(want)     # a name stored at the 40-char cut


def compute_coverage(inventories: list[dict], flows: list[dict]) -> Coverage:
    tested = [f for f in flows if f.get("status") in TESTED]
    coverage = Coverage(tested_flows=len(tested),
                        planned_flows=sum(1 for f in flows if f.get("status") in {"candidate", "stale"}))
    by_page = {}
    for inv in inventories:
        path = _path(inv.get("url", ""))
        if path not in by_page:                                   # the same page explored twice counts once
            by_page[path] = PageCoverage(path=path, url=inv.get("url", ""))
    inv_by_path = {_path(i.get("url", "")): i for i in inventories}
    acted: dict[str, list[dict]] = {p: [] for p in by_page}
    for flow in tested:
        pages_of_steps = step_pages(flow)
        landed = [_path(u) for u in (flow.get("observed") or {}).get("step_urls") or []]
        for path in {_path(flow.get("start_url", "")), *pages_of_steps, *landed}:
            if path in by_page:
                by_page[path].visited = True
                if flow["id"] not in by_page[path].flows:
                    by_page[path].flows.append(flow["id"])
        for step, path in zip(flow.get("steps") or [], pages_of_steps):
            if path in acted:
                acted[path].append(step)
    for path, page in by_page.items():
        for control in actionable_controls(inv_by_path[path]):
            page.total += 1
            if any(_touches(step, control) for step in acted[path]):
                page.touched += 1
            else:
                label = (control.get("name") or control.get("selector") or "").strip()
                page.untouched.append(label[:40] or "(unnamed)")
        page.untouched = list(dict.fromkeys(page.untouched))     # unique, in page order
        if page.touched:
            page.visited = True                                    # acting on a page means visiting it
    coverage.pages = sorted(by_page.values(), key=lambda p: (not p.visited, p.path))
    return coverage


def render_coverage(coverage: Coverage, limit: int = 4) -> str:
    """A terminal table: the totals, then one line per page with a few of its untouched controls."""
    if not coverage.pages:
        return "no explored pages - run `explore` first"
    lines = [f"COVERAGE  pages visited {coverage.pages_visited}/{coverage.pages_total} ({coverage.percent_pages}%)  |  "
             f"controls touched {coverage.controls_touched}/{coverage.controls_total} ({coverage.percent_controls}%)  |  "
             f"flows: {coverage.tested_flows} tested, {coverage.planned_flows} not yet backed by a passing run", ""]
    width = max(len(p.path) for p in coverage.pages)
    lines.append(f'{"PAGE":<{width}}  VISITED  TOUCHED  NOT TOUCHED')
    for p in coverage.pages:
        rest = len(p.untouched) - limit
        shown = "; ".join(p.untouched[:limit]) + (f"; +{rest} more" if rest > 0 else "")
        lines.append(f'{p.path:<{width}}  {"yes" if p.visited else "no ":<7}  {f"{p.touched}/{p.total}":<7}  {shown}')
    return "\n".join(lines)


def render_uncovered(coverage: Coverage, max_pages: int = 8, per_page: int = 6) -> str:
    """The same gaps as prompt text, for `intents` and `propose`: pages no tested flow visits, and the content
    controls on visited pages nothing acts on. Empty when there is nothing left to cover."""
    never = [p.path for p in coverage.pages if not p.visited and p.total]
    partial = [p for p in coverage.pages if p.visited and p.untouched]
    if not never and not partial:
        return ""
    lines = ["NOT YET COVERED by any tested flow (prefer journeys that reach these):"]
    if never:
        lines.append("  pages no flow visits: " + ", ".join(never[:max_pages]))
    for p in partial[:max_pages]:
        lines.append(f'  {p.path}: not acted on: ' + "; ".join(p.untouched[:per_page]))
    return "\n".join(lines)
