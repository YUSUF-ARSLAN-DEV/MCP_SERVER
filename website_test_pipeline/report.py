"""Build human-verifiable Word reports from a test run.

The report exists so a person can confirm the pass/fail numbers instead of
trusting the generator + runner blindly: every test is shown with the
assertions it made and the screenshot evidence captured at each step.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from . import report_policy as policy
from .coverage import Coverage, compute_coverage
from .findings import (
    Finding, FlapRecord, auth_failure_findings, auth_summary_lines, collect_findings, detect_flapping,
    environment_block, human_input_pages, load_auth_history, plain_error, plain_skip_reason, untested_auth,
)
from .flowgen import file_name as flow_spec_name
from .envinfo import capture_browser, load_browser_info
from .flowreport import FlowReport, build_flow_report, disambiguate_titles
from .flows import FlowsFileError, load_flows
from .ratings import RatingsFileError, load_ratings
from .report_model import canonical_counts, validate_report_data, write_report_data
from .sitemap import load_inventories

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
ATTACH_EXTS = {".webm", ".zip"}

# Full-page screenshots are ~1280px wide and often 4-5k tall; embedded raw they
# bloat a full-run .docx to hundreds of MB. Downscale + JPEG-encode so the
# document stays shareable while text on the page is still readable.
_MAX_IMG_WIDTH = 1000
_MAX_IMG_HEIGHT = 3600
_JPEG_QUALITY = 55


def _embeddable(path: Path):
    """Return a JPEG BytesIO for `path`, or the original path if PIL can't read it."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            scale = min(_MAX_IMG_WIDTH / im.width, _MAX_IMG_HEIGHT / im.height, 1.0)
            if scale < 1.0:
                im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
            buffer = io.BytesIO()
            im.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            buffer.seek(0)
            return buffer
    except Exception:
        return str(path)


# --------------------------------------------------------------------------- model

@dataclass
class TestOutcome:
    nodeid: str
    title: str
    url: str
    status: str                       # passed | failed | skipped | error
    duration: float = 0.0
    error: str | None = None
    assertions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)     # step screenshots
    attachments: list[str] = field(default_factory=list)  # video / trace
    screenshots: list[str] = field(default_factory=list)  # Playwright's own screenshots (test-failed-1.png ...)
    invalid_reason: str | None = None  # set when a failure is a fault in the test itself, not in the application

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass
class UrlReport:
    url: str
    spec_path: str | None = None
    generated_status: str | None = None
    outcomes: list[TestOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coverage_ceiling: str | None = None  # set when behavioural coverage is legitimately capped (captcha / embed / thin)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(o.passed for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.status in {"failed", "error"} for o in self.outcomes)


@dataclass
class RunReport:
    base_url: str = ""
    model: str = ""
    started_at: str = ""
    finished_at: str = ""
    url_reports: list[UrlReport] = field(default_factory=list)
    flow_reports: list[FlowReport] = field(default_factory=list)   # every flow; those with tested=False were not run
    notes: list[str] = field(default_factory=list)                 # things that happened while writing the documents
    coverage: Coverage | None = None                               # what the tested flows touch; None when there are no flows
    findings: list[Finding] = field(default_factory=list)          # triaged failures: severity, kind, one-line reason
    untested_auth: list[dict] = field(default_factory=list)        # login / sign-up walls the run could not pass
    human_input_pages: list[dict] = field(default_factory=list)    # a step only a person can complete (CAPTCHA / OTP)
    auth_lines: list[str] = field(default_factory=list)            # one line per account: signed in, and on which attempt
    flapping: list[FlapRecord] = field(default_factory=list)       # flows/tests whose recent runs mix pass and fail
    requirements: list[dict] = field(default_factory=list)         # the plain-sentence intents (intents.json), dropped ones left out
    browser_info: dict = field(default_factory=dict)               # browser name/version/device the tests ran on (envinfo.py)
    artifacts_dir: Path | None = None                              # where evidence lives; links resolve against it
    run_id: str = ""                                               # identifies this run in run-history.json
    app_build: dict = field(default_factory=dict)                  # build id of the SITE UNDER TEST and where it came from
    owners: dict = field(default_factory=dict)                     # owners.json / REPORT_OWNER: who is named on each action
    generation_failures: list[dict] = field(default_factory=list)  # pages whose spec could not be generated (harness failure)

    @property
    def tested_flows(self) -> list[FlowReport]:
        return [f for f in self.flow_reports if f.tested]

    @property
    def untested_flows(self) -> list[FlowReport]:
        return [f for f in self.flow_reports if not f.tested]

    def flows_at(self, url: str) -> list[FlowReport]:
        return [f for f in self.tested_flows if f.start_url == url]

    @property
    def total(self) -> int:
        return int(canonical_counts(self)["executed_tests"])

    @property
    def passed(self) -> int:
        return int(canonical_counts(self)["passed"])

    @property
    def failed(self) -> int:
        return int(canonical_counts(self)["failed"])

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for u in self.url_reports:
            out.extend(f"[{u.url}] {w}" for w in u.warnings)
        for f in self.tested_flows:
            out.extend(f"[flow] {w}" for w in f.warnings)
        return out + self.notes


# ----------------------------------------------------------------------- assertions

def assertions_for(spec_path: Path, test_title: str) -> list[str]:
    """Pull the expect(...) / assert lines of one test function out of its source."""
    try:
        tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == test_title:
            picked: list[str] = []
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _is_expect(child):
                    picked.append(_unparse(child))
                elif isinstance(child, ast.Assert):
                    picked.append("assert " + _unparse(child.test))
            return _dedupe(picked)
    return []


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _is_expect(call: ast.Call) -> bool:
    target = call.func
    while isinstance(target, ast.Attribute):
        target = target.value
    return isinstance(target, ast.Call) and isinstance(target.func, ast.Name) and target.func.id == "expect"


def _dedupe(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------------------- load

def _slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", nodeid).strip("-") or "test"


def _pw_slug(nodeid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", nodeid.lower()).strip("-")


def _row_file(row: dict) -> str:
    return str(row.get("nodeid", "")).split("::")[0].replace(chr(92), "/").rsplit("/", 1)[-1]


def _make_outcome(row: dict, url: str, spec_path: str | None, artifacts_dir: Path) -> TestOutcome:
    spec = Path(spec_path) if spec_path else None
    title = row.get("nodeid", "").split("::")[-1].split("[")[0]
    return TestOutcome(
        nodeid=row.get("nodeid", ""),
        title=row.get("title") or title,
        url=url,
        status=row.get("status", "error"),
        duration=float(row.get("duration") or 0.0),
        error=row.get("error"),
        assertions=assertions_for(spec, title) if spec and spec.exists() else [],
        evidence=_find_images(artifacts_dir / "evidence" / _slug(row.get("nodeid", ""))),
        attachments=_find_attachments(artifacts_dir / "pw", _pw_slug(row.get("nodeid", ""))),
        screenshots=_find_attachments(artifacts_dir / "pw", _pw_slug(row.get("nodeid", "")), IMAGE_EXTS),
    )


def _worst(rows: list[dict]) -> dict:
    """One test per flow; if several rows exist a failure is never hidden by a pass."""
    order = {"failed": 0, "error": 0, "passed": 1}
    return sorted(rows, key=lambda r: order.get(r.get("status", ""), 2))[0]


def _flow_reports(results: dict, tests_dir: Path, artifacts_dir: Path, flows_file: Path | None,
                  ratings_file: Path | None) -> tuple[list[FlowReport], set[str]]:
    """(one FlowReport per flow, nodeids of the tests that belong to a flow). A missing or unreadable
    flows/ratings file means no flow reports at all, so a flow test then stays an ordinary test."""
    try:
        flows = load_flows(flows_file)["flows"] if flows_file and flows_file.exists() else []
        ratings = load_ratings(ratings_file)["ratings"] if ratings_file and ratings_file.exists() else {}
    except (FlowsFileError, RatingsFileError):
        return [], set()
    flows = [f for f in flows if f.get("status") not in {"rejected", "auto_rejected"}]      # a dropped flow (human or auto) is not a gap in the run
    by_file = {flow_spec_name(f): f for f in flows}
    rows_by_flow: dict[str, list[dict]] = {}
    for row in results.get("tests", []):
        flow = by_file.get(_row_file(row))
        if flow is not None and row.get("status") in {"passed", "failed", "error", "skipped"}:
            rows_by_flow.setdefault(flow["id"], []).append(row)
    reports = []
    for flow in sorted(flows, key=lambda f: (f.get("start_url", ""), f["id"])):
        rows = rows_by_flow.get(flow["id"])
        outcome = None
        if rows:
            row = _worst(rows)
            outcome = _make_outcome(row, flow.get("start_url", ""), _guess_spec(row.get("nodeid", ""), tests_dir), artifacts_dir)
        reports.append(build_flow_report(flow, ratings.get(flow["id"], []), outcome))
    disambiguate_titles(reports)
    return reports, {r.get("nodeid") for rows in rows_by_flow.values() for r in rows}


def _requirements(path: Path) -> list[dict]:
    """The written requirements / user stories behind the flows: the plain sentences in intents.json, minus dropped ones.
    A missing or unreadable file simply means no traceability table."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return []
    return [i for i in doc.get("intents", []) if isinstance(i, dict) and i.get("status") != "dropped" and i.get("id")]


def _flows_list(flows_file: Path | None) -> list[dict]:
    try:
        return load_flows(flows_file)["flows"] if flows_file and flows_file.exists() else []
    except FlowsFileError:
        return []


def _ratings_dict(ratings_file: Path | None) -> dict[str, list[dict]]:
    try:
        return load_ratings(ratings_file)["ratings"] if ratings_file and ratings_file.exists() else {}
    except RatingsFileError:
        return {}


def _coverage(artifacts_dir: Path, manifest: dict, flows: list[dict]) -> Coverage | None:
    """Flow coverage of the explored pages of this run; nothing when the site has no flows (older reports stay as they were)."""
    if not flows:
        return None
    inventories = load_inventories(artifacts_dir)
    urls = set((manifest.get("urls") or {}))
    if urls:
        inventories = [i for i in inventories if i.get("url") in urls] or inventories
    return compute_coverage(inventories, flows) if inventories else None


def load_run(artifacts_dir: Path, tests_dir: Path, model: str = "", flows_file: Path | None = None,
             ratings_file: Path | None = None) -> RunReport:
    results = json.loads((artifacts_dir / "test_results.json").read_text(encoding="utf-8"))
    manifest = _maybe_json(artifacts_dir / "run.json")
    workspace = artifacts_dir.parent
    flow_reports, flow_nodeids = _flow_reports(
        results, tests_dir, artifacts_dir, flows_file or workspace / "flows.json", ratings_file or workspace / "flow_ratings.json")

    generated = {url: info.get("status") for url, info in (manifest.get("urls") or {}).items()}
    specs = {url: info.get("spec") for url, info in (manifest.get("urls") or {}).items()}

    run_failures: list[dict] = []
    by_url: dict[str, UrlReport] = {}
    for row in results.get("tests", []):
        if row.get("nodeid") in flow_nodeids:
            continue                                   # a flow's test is reported in the flows section
        url = row.get("url") or "(unknown url)"
        report = by_url.setdefault(url, UrlReport(url=url))
        report.spec_path = _resolve_spec(specs.get(url), row.get("nodeid", ""), tests_dir)
        report.generated_status = generated.get(url)
        report.outcomes.append(_make_outcome(row, url, report.spec_path, artifacts_dir))

    # generated specs that produced no test rows at all
    for url, status in generated.items():
        if status == "generated" and url not in by_url:
            by_url[url] = UrlReport(url=url, spec_path=specs.get(url), generated_status=status)
    # a flow that ran starts on a page: make sure that page gets a document even if it has no page tests
    for flow in flow_reports:
        if flow.tested and flow.start_url not in by_url:
            by_url[flow.start_url] = UrlReport(url=flow.start_url)

    for url, info in (manifest.get("urls") or {}).items():
        if info.get("status") == "failed":
            run_failures.append({"url": url, "error": _full(info.get("error") or "No spec was generated: the model returned no usable test code or every spec failed validation.")})
    flows_list = _flows_list(flows_file or workspace / "flows.json")
    ratings = _ratings_dict(ratings_file or workspace / "flow_ratings.json")
    run = RunReport(
        base_url=_common_prefix([u for u in by_url]),
        model=model or manifest.get("model", ""),
        started_at=manifest.get("started_at", ""),
        finished_at=manifest.get("finished_at", results.get("finished_at", "")),
        url_reports=[by_url[k] for k in sorted(by_url)],
        flow_reports=flow_reports,
        coverage=_coverage(artifacts_dir, manifest, flows_list),
        artifacts_dir=artifacts_dir,
        generation_failures=run_failures,
        owners=policy.load_owner_config(workspace),
        run_id=policy.run_id_for(manifest, results.get("finished_at", "") or manifest.get("finished_at", ""),
                                 manifest.get("started_at", ""), json.dumps(results, sort_keys=True)),
    )
    for report in run.url_reports:
        report.coverage_ceiling = _behaviour_ceiling(_load_inventory(artifacts_dir, report.url))
        validate_url(report)
    flows_by_id = {f["id"]: f for f in flows_list}
    inventories = load_inventories(artifacts_dir)
    run.findings = collect_findings(run, tests_dir, inventories, flows_by_id, ratings)
    names = {f["id"]: (f.get("goal") or f["id"]) for f in flows_list}
    run.flapping = detect_flapping(ratings, names)
    run.untested_auth = untested_auth(inventories)
    run.human_input_pages = human_input_pages(inventories)
    run.requirements = _requirements(workspace / "intents.json")
    run.browser_info = load_browser_info(artifacts_dir) or ({} if os.environ.get("WTP_SKIP_BROWSER_PROBE") else capture_browser(artifacts_dir))
    auth_history = load_auth_history(artifacts_dir)
    run.auth_lines = auth_summary_lines(auth_history)
    run.findings = auth_failure_findings(auth_history) + run.findings
    _triage_outcomes(run)
    run.app_build = policy.app_build_info(run.base_url, probe=not os.environ.get("WTP_SKIP_BROWSER_PROBE"))
    return run


_ACTION_ROLES = {"button", "checkbox", "radio", "combobox", "tab", "switch", "slider", "menuitem", "menuitemcheckbox"}
_CAPTCHA_RE = re.compile(r"captcha|recaptcha|hcaptcha|are you human|prove you|robot", re.I)


def _inv_slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:100]

def _load_inventory(artifacts_dir: Path, url: str) -> dict | None:
    path = artifacts_dir / f"{_inv_slug(url)}.inventory.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def _count_page_actions(data: dict) -> int:
    """How many genuinely interactive things the explorer saw - form controls,
    buttons, revealed panels, form fields."""
    n = 0
    for c in data.get("controls") or []:
        if c.get("region") == "chrome" or c.get("hidden"):
            continue
        tag, role = c.get("tag"), (c.get("role") or "")
        if (tag in {"button", "select", "textarea"}
                or (tag == "input" and (c.get("type") or "text") != "hidden")
                or role in _ACTION_ROLES):
            n += 1
    n += len(data.get("revealed") or [])
    for f in data.get("forms") or []:
        n += min(len(f.get("fields") or []), 3)
    return n

def _behaviour_ceiling(data: dict | None) -> str | None:
    """A reason the page's behavioural coverage is legitimately capped - so a file
    of mostly visibility checks is honest, not lazy. None means no excuse."""
    if data is None:
        return None
    haystack = json.dumps(data.get("controls") or [], ensure_ascii=False) + " " \
        + json.dumps(data.get("forms") or [], ensure_ascii=False) + " " \
        + (data.get("accessibility") or "")
    if _CAPTCHA_RE.search(haystack):
        return "the form is CAPTCHA-protected, so a real end-to-end submit cannot be automated"
    if data.get("embeds") and _count_page_actions(data) <= 3:
        return "the page is built around a third-party embed (map / media) with no driveable DOM"
    actions = _count_page_actions(data)
    if actions <= 2:
        return f"only ~{actions} interactive control(s) observed"
    return None

def _maybe_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _guess_spec(nodeid: str, tests_dir: Path) -> str | None:
    stem = nodeid.split("::")[0]
    candidate = tests_dir / Path(stem).name
    return str(candidate) if candidate.exists() else None


def _resolve_spec(stored: str | None, nodeid: str, tests_dir: Path) -> str | None:
    """Prefer a stored path that still exists, else find the spec by name in tests_dir."""
    if stored and Path(stored).exists():
        return stored
    return _guess_spec(nodeid, tests_dir) or stored


def _find_images(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return [str(p) for p in sorted(directory.iterdir()) if p.suffix.lower() in IMAGE_EXTS]


def _find_attachments(pw_dir: Path, slug: str, exts: set[str] = ATTACH_EXTS) -> list[str]:
    if not pw_dir.is_dir():
        return []
    out: list[str] = []
    for child in pw_dir.iterdir():
        if child.is_dir() and slug and slug in child.name:
            out.extend(str(p) for p in sorted(child.iterdir()) if p.suffix.lower() in exts)
    return out


def _common_prefix(urls: list[str]) -> str:
    if not urls:
        return ""
    first = urls[0]
    for i, ch in enumerate(first):
        if any(len(u) <= i or u[i] != ch for u in urls):
            return first[:i]
    return first


# ------------------------------------------------------------------------ validate

_BEHAVIOURAL_ASSERT = re.compile(
    r"to_have_value|to_have_url|to_contain_text|to_have_text|to_be_enabled|to_be_disabled|"
    r"to_be_checked|to_have_attribute|to_have_class|to_have_count\(\s*[2-9]"
)


def _title_calls_action(spec_path: Path | None, title: str) -> bool:
    """True if the test function performs a real action - it calls
    action_evidence(...) (which does a click/fill/select and then verifies a
    post-state). Such a test is behavioural regardless of which assertion the
    verify callback uses."""
    if not spec_path:
        return False
    try:
        tree = ast.parse(Path(spec_path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == title:
            return any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "action_evidence"
                for c in ast.walk(node)
            )
    return False


def validate_url(report: UrlReport) -> None:
    warnings = report.warnings
    if report.generated_status == "generated" and report.total == 0:
        warnings.append("spec was generated but no tests executed")
    for outcome in report.outcomes:
        if outcome.passed and not outcome.evidence:
            warnings.append(f"{outcome.title}: PASSED with no screenshot evidence")
        if outcome.status in {"failed", "error"} and not outcome.attachments and not outcome.evidence:
            warnings.append(f"{outcome.title}: FAILED with no evidence or trace recorded")
        if outcome.passed and not outcome.assertions:
            warnings.append(f"{outcome.title}: PASSED with no detectable assertion (trivial test)")
    behavioural = sum(
        1 for o in report.outcomes
        if any(_BEHAVIOURAL_ASSERT.search(a) for a in o.assertions)
        or _title_calls_action(report.spec_path, o.title)
    )
    if report.total >= 4 and behavioural <= report.total // 4:
        if report.coverage_ceiling:
            # The page legitimately can't be driven further - a file of visibility
            # checks IS the honest coverage here, not a lazy one.
            warnings.append(
                f"limited coverage is expected here: {report.coverage_ceiling} "
                f"({behavioural}/{report.total} behavioural) - not a coverage gap"
            )
        else:
            warnings.append(
                f"{behavioural}/{report.total} tests exercise behaviour (click/fill/submit/navigate + verify); "
                "the rest only assert an element is visible - shallow coverage for this page"
            )


# --------------------------------------------------------------------------- render

def _heading_color(document, text: str, level: int, rgb: tuple[int, int, int] | None = None) -> None:
    heading = document.add_heading(text, level)
    if rgb:
        for run in heading.runs:
            run.font.color.rgb = RGBColor(*rgb)


def _status_line(document, outcome: TestOutcome) -> None:
    para = document.add_paragraph()
    run = para.add_run(f"Status: {outcome.status.upper()}")
    run.bold = True
    run.font.color.rgb = RGBColor(0x1B, 0x7F, 0x37) if outcome.passed else RGBColor(0xB3, 0x26, 0x1A)
    para.add_run(f"    Duration: {outcome.duration:.2f}s")


def _add_hyperlink(paragraph, target: str, text: str) -> None:
    r_id = paragraph.part.relate_to(target, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color"); color.set(qn("w:val"), "0563C1"); props.append(color)
    underline = OxmlElement("w:u"); underline.set(qn("w:val"), "single"); props.append(underline)
    run.append(props)
    node = OxmlElement("w:t"); node.text = text; run.append(node)
    link.append(run)
    paragraph._p.append(link)


def _rel(target: str, base: Path | None) -> str:
    if base is None:
        return target
    try:
        return os.path.relpath(target, base).replace(os.sep, "/")
    except ValueError:
        return target


def _render_outcome(document, outcome: TestOutcome, *, embed: bool = True, link_base: Path | None = None) -> None:
    document.add_heading(outcome.title.replace("_", " "), 2)
    _status_line(document, outcome)
    _render_assertions(document, outcome)
    _render_evidence(document, outcome, embed=embed, link_base=link_base)
    _render_failure_detail(document, outcome, link_base=link_base)


def _render_assertions(document, outcome: TestOutcome) -> None:
    if outcome.assertions:
        verified = outcome.status == "passed"
        document.add_heading(
            "Assertions verified" if verified else "Assertions in this test (test did not pass - see failure detail)", 3)
        for line in outcome.assertions:
            document.add_paragraph(line, style="List Bullet")


def _evidence_caption(stem: str) -> str:
    """'01-click-flow' -> 'Step 1: click flow'; the runner's '99-outcome' -> 'Final outcome'."""
    m = re.match(r"^(\d+)[-_ ]+(.*)$", stem)
    if not m:
        return stem.replace("-", " ").replace("_", " ")
    num, label = int(m.group(1)), m.group(2).replace("-", " ").replace("_", " ").strip()
    if num == 99 and label.lower() in {"outcome", ""}:
        return "Final outcome"
    return f"Step {num}: {label}" if label else f"Step {num}"


def _render_evidence(document, outcome: TestOutcome, *, embed: bool = True, link_base: Path | None = None) -> None:
    document.add_heading("Browser evidence", 3)
    seen: dict[str, str] = getattr(document, "_evidence_hashes", {})
    setattr(document, "_evidence_hashes", seen)
    if not outcome.evidence:
        para = document.add_paragraph("No screenshot evidence was captured for this test.")
        para.runs[0].italic = True
    for image in outcome.evidence:
        path = Path(image)
        if not path.is_file():
            continue
        digest = _evidence_hash(str(path))
        caption_text = f'{outcome.nodeid or outcome.title} — {_evidence_caption(path.stem)}'
        if digest in seen:
            document.add_paragraph(f"Duplicate screenshot; see {seen[digest]}.")
            continue
        evidence_id = f"E-{len(seen) + 1:04d}"
        seen[digest] = evidence_id
        if embed:
            try:
                document.add_picture(_embeddable(path), width=Inches(6.0))
            except Exception as exc:  # unreadable / truncated screenshot
                document.add_paragraph(f"(could not embed {path.name}: {exc})")
                continue
            caption = document.add_paragraph(f"{evidence_id}: {caption_text}")
            caption.runs[0].italic = True
            caption.runs[0].font.size = Pt(9)
        else:
            para = document.add_paragraph(f"{evidence_id}: {caption_text}  —  ", style="List Bullet")
            _add_hyperlink(para, _rel(str(path), link_base), path.name)


def _render_failure_detail(document, outcome: TestOutcome, *, link_base: Path | None = None) -> None:
    if outcome.status in {"failed", "error"}:
        document.add_heading("Failure detail", 3)
        block = document.add_paragraph(outcome.error or "(no traceback captured)")
        block.runs[0].font.name = "Consolas"
        block.runs[0].font.size = Pt(8)
        for attachment in outcome.attachments:
            para = document.add_paragraph("Attachment: ")
            para.runs[0].font.size = Pt(9)
            _add_hyperlink(para, _rel(attachment, link_base), Path(attachment).name)


_GREEN, _RED, _GREY = RGBColor(0x1B, 0x7F, 0x37), RGBColor(0xB3, 0x26, 0x1A), RGBColor(0x59, 0x59, 0x59)
_ARROW = " → "


def _render_flow(document, flow: FlowReport, *, embed: bool = True, link_base: Path | None = None) -> None:
    """One user journey: the sentence as the title, then what it did, what was expected and seen,
    where it broke if it did, and the recorded history - not just a pytest function name."""
    outcome = flow.outcome
    document.add_heading(flow.title, 2)
    para = document.add_paragraph()
    run = para.add_run(f"Result: {outcome.status.upper()}")
    run.bold = True
    run.font.color.rgb = _GREEN if outcome.passed else _RED
    stored_status = {"verified": "Verified", "approved": "Approved", "stale": "Needs retesting", "candidate": "Not yet verified"}.get(flow.status, "Not recorded")
    current_status = "Passed" if flow.passed else ("Failed" if flow.failed else "Skipped")
    para.add_run(f"    Current result: {current_status}    Stored review status: {stored_status}    Duration: {outcome.duration:.2f}s")
    where = (f" across {len(flow.pages)} pages: " + _ARROW.join(flow.pages)) if len(flow.pages) > 1 else (
        f" on {flow.pages[0]}" if flow.pages else "")
    origin = f" {flow.intent_id} (from the plain-sentence file)" if flow.intent_id else ""
    source = document.add_paragraph("User flow" + origin + where)
    source.runs[0].font.color.rgb = _GREY

    document.add_heading("Journey", 3)
    for n, step in enumerate(flow.steps):
        landing = flow.lands[n] if n < len(flow.lands) else ""
        document.add_paragraph(step + (f"  →  {landing}" if landing else ""), style="List Number")

    document.add_heading("Expected vs observed", 3)
    document.add_paragraph(f"Expected: {flow.expected or 'not stated'}", style="List Bullet")
    document.add_paragraph(f"Observed in a real run: {flow.observed or 'never run'}", style="List Bullet")
    if flow.new_headings:
        document.add_paragraph("New on the page: " + " | ".join(flow.new_headings), style="List Bullet")

    _render_assertions(document, outcome)

    if flow.failure:
        document.add_heading("Where it broke", 3)
        para = document.add_paragraph(flow.failure)
        para.runs[0].font.color.rgb = _RED

    if flow.history:
        document.add_heading("Review history", 3)
        for line in flow.history[-12:]:
            document.add_paragraph(line, style="List Bullet")

    _render_evidence(document, outcome, embed=embed, link_base=link_base)
    _render_failure_detail(document, outcome, link_base=link_base)


def _grid(document, labels: tuple[str, ...]):
    table = document.add_table(rows=1, cols=len(labels))
    table.autofit = True
    try:
        table.style = "Table Grid"
    except KeyError:
        pass
    for cell, label in zip(table.rows[0].cells, labels):
        run = cell.paragraphs[0].add_run(label)
        run.bold = True
        run.font.size = Pt(9)
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), "D9EAF7")
        cell._tc.get_or_add_tcPr().append(shading)
    return table


def _set_table_widths(table, widths: tuple[float, ...]) -> None:
    """Give dense stakeholder tables intentional column proportions."""
    table.autofit = False
    for column, width in zip(table.columns, widths):
        column.width = Inches(width)
        for cell in column.cells:
            cell.width = Inches(width)


def _table_of_contents(document, lean: bool = False) -> None:
    """Insert a populated, clickable TOC with no stale field placeholder."""
    document.add_heading("Table of Contents", 1)
    entries = (
        "Executive Summary", "Test Scope", "Test Environment", "Test Execution Summary",
        "Defect Report (Bugs Found)", "Test Coverage / Requirements Traceability",
        "Test Logs & Evidence", "Risks & Issues", "Conclusions & Recommendations",
    )
    for entry in entries:
        paragraph = document.add_paragraph(style="List Bullet")
        _add_internal_hyperlink(paragraph, entry, _heading_anchor(entry))
    document.add_page_break()


def _add_page_reference(paragraph, anchor: str) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = f" PAGEREF {anchor} \\h "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "-"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instruction, separate, text, end])


def _heading_anchor(text: str) -> str:
    return "toc_" + re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _add_internal_hyperlink(paragraph, text: str, anchor: str) -> None:
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("w:anchor"), anchor)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    properties.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.append(underline)
    run.append(properties)
    node = OxmlElement("w:t")
    node.text = text
    run.append(node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _bookmark_headings(document) -> None:
    """Give report headings stable anchors so the generated TOC works without Word field updates."""
    used: dict[str, int] = {}
    for paragraph in document.paragraphs:
        if not paragraph.style.name.startswith("Heading"):
            continue
        base = _heading_anchor(paragraph.text)
        count = used.get(base, 0)
        used[base] = count + 1
        anchor = base if count == 0 else f"{base}_{count + 1}"
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), str(1000 + len(used)))
        start.set(qn("w:name"), anchor)
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), str(1000 + len(used)))
        paragraph._p.insert(0, start)
        paragraph._p.append(end)


def _shorten(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _display_id(value: str) -> str:
    return policy.display_id(value)


def _all_outcomes(run: RunReport) -> list[tuple[str, str, TestOutcome, FlowReport | None]]:
    rows: list[tuple[str, str, TestOutcome, FlowReport | None]] = []
    for report in run.url_reports:
        for outcome in report.outcomes:
            rows.append(("page", outcome.nodeid or outcome.title, outcome, None))
    for flow in run.tested_flows:
        if flow.outcome is not None:
            rows.append(("flow", flow.flow_id, flow.outcome, flow))
    return rows


def _severity_label(value: str, scope: str = "page") -> str:
    # Compatibility for old run data while keeping the rendered taxonomy clean.
    if value == "P1":
        return "High"
    if value == "P2":
        return "Medium"
    return value if value in {"Critical", "High", "Medium", "Low"} else ("High" if scope == "flow" else "Medium")


def _plain_flow_title(title: str) -> str:
    """Keep selectors out of the stakeholder defect title; raw steps remain in the appendix."""
    text = re.sub(r"\b(?:input|select|textarea|button)\[[^\]]+\]", "the form control", title or "")
    text = re.sub(r"#[A-Za-z0-9_-]+", "the control", text)
    return re.sub(r"\s+", " ", text).strip() or "User journey"


def _finding_for(run: RunReport, test: str, url: str, scope: str):
    candidates = [f for f in run.findings if f.test == test and f.url == url and f.scope == scope]
    return candidates[0] if candidates else None


def _relative_artifact(path: str | None, base: Path | None) -> str:
    if not path:
        return "NOT CAPTURED"
    try:
        return os.path.relpath(path, base) if base else path
    except (OSError, ValueError):
        return path


def _full(text: Any) -> str:
    """Whitespace-normalised and never cut: titles are shown in full wherever they appear."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _evidence_target(path: str, report_dir: Path | None, run: "RunReport") -> str:
    """A link that resolves: under ARTIFACT_BASE_URL when one is configured, otherwise relative to the report folder."""
    base = os.environ.get("ARTIFACT_BASE_URL", "").strip()
    if base and run.artifacts_dir:
        try:
            rel = os.path.relpath(path, run.artifacts_dir).replace(os.sep, "/")
            if not rel.startswith(".."):
                return base.rstrip("/") + "/" + rel
        except ValueError:
            pass
    return _relative_artifact(path, report_dir).replace(os.sep, "/")


def _run_date(run: "RunReport"):
    try:
        return datetime.fromisoformat(_utc_timestamp(run.finished_at or run.started_at)).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


def _make_record(run: "RunReport", report_dir: Path | None, *, category: str, failure_class: str, scope: str, title: str,
                 description: str, steps: list[str], expected: str, actual: str, url: str, impact: str,
                 flow_id: str = "NOT CAPTURED", evidence: str | None = None, trace: str | None = None,
                 video: str | None = None, failure_text: str = "", assertion_text: str = "",
                 mitigation: str | None = None, reason_detail: str = "") -> dict[str, Any]:
    """One auditable record. Severity comes from user impact (report_policy.impact_severity), never from how the
    failure was classified; priority follows severity; a limitation is never P1 and nothing but a High/Critical
    application defect is ever a release blocker."""
    severity = policy.impact_severity(category, scope=scope, title=title, failure_text=failure_text or actual,
                                      assertion_text=assertion_text)
    shot = _evidence_target(evidence, report_dir, run) if evidence else "NOT CAPTURED"
    trace_url = _evidence_target(trace, report_dir, run) if trace else "NOT CAPTURED"
    video_url = _evidence_target(video, report_dir, run) if video else "NOT CAPTURED"
    has_link = any(v != "NOT CAPTURED" for v in (shot, trace_url, video_url))
    record = {
        "category": category,
        "severity": severity,
        "priority": policy.priority_for(category, severity),
        "title": _full(title),
        "description": description,
        "steps_to_reproduce": steps,
        "expected": expected or "NOT CAPTURED",
        "actual": actual or "NOT CAPTURED",
        "screenshot_url": shot,
        "trace_url": trace_url,
        "video_url": video_url,
        "evidence_reason": "" if has_link else policy.evidence_reason(category, failure_class, reason_detail),
        "status": "Open",
        "owner": "",
        "due": "",
        "linked_flow_id": flow_id,
        "failure_class": failure_class,
        "scope": scope,
        "url": url or "",
        "business_impact": impact,
        "blocking": category == "defect" and severity in {"Critical", "High"},
    }
    if mitigation:
        record["mitigation"] = mitigation
    return record


def _triage_outcomes(run: "RunReport") -> None:
    """Mark a failing test whose own expectation is wrong (same expected/observed URL, or a stated expectation that is
    not the assertion that ran) so it is reported as a test defect and kept out of the failure count and the verdict."""
    for scope, _key, outcome, flow in _all_outcomes(run):
        if outcome.status not in {"failed", "error"}:
            continue
        finding = _finding_for(run, flow.title if flow else outcome.title, outcome.url, scope)
        if finding is not None and finding.kind == "test_defect":
            outcome.invalid_reason = f"{finding.summary}. Next step: {finding.next_action}."
            continue
        reason = policy.triage_test_defect(outcome.error, flow.expected if flow else "", outcome.assertions)
        if reason:
            outcome.invalid_reason = reason


def _defect_records(run: "RunReport", report_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every item of the Defect Report, each in exactly one of five types:
    Defect (DEF) - something ran and was broken;  Unverified (UNV) - the journey could not be confirmed either way;
    Limitation (LIM) - the tooling cannot test it (CAPTCHA, sign-in, no test account);
    Test defect (TST) - the test itself is wrong, so it says nothing about the application;
    Tooling issue (TOOL) - the harness failed (no spec, spec rejected, test skipped)."""
    raw: list[dict[str, Any]] = []
    direct_keys: set[tuple[str, str, str]] = set()
    for scope, key, outcome, flow in _all_outcomes(run):
        title = _plain_flow_title(flow.title) if flow else outcome.title.replace("_", " ")
        evidence = (outcome.evidence or outcome.screenshots or [None])[0]
        trace = next((a for a in outcome.attachments if a.lower().endswith(".zip")), None)
        video = next((a for a in outcome.attachments if a.lower().endswith(".webm")), None)
        flow_id = flow.flow_id if flow else "NOT CAPTURED"
        steps = [f"Open {outcome.url}.", f"Run test {key}."]
        if outcome.status == "skipped":
            reason = plain_skip_reason(outcome.error)
            raw.append(_make_record(
                run, report_dir, category="tooling", failure_class="harness_skip", scope=scope,
                title=f"Test skipped before it ran: {title}", description=reason, steps=steps,
                expected="The check should run and produce a result.", actual=reason, url=outcome.url, flow_id=flow_id,
                evidence=evidence, trace=trace, video=video,
                impact=f'"{title}" on {_page_of(outcome.url)} has no result from this check, so that part of the site is not covered.'))
            continue
        if outcome.status not in {"failed", "error"}:
            continue
        finding = _finding_for(run, flow.title if flow else outcome.title, outcome.url, scope)
        # outcome.assertions is every expect()/assert in the spec's source, in source order - not an execution
        # trace, so it does not know which one actually failed. For a multi-step test the earlier ones are the
        # per-step checks that ran and passed (the test only gets this far if they did); the LAST one in source
        # order is the closest available guess at the check "expected" should actually describe.
        expected = (flow.expected if flow and flow.expected else
                    (_plain_assertion(outcome.assertions[-1]) if outcome.assertions else "The check completes as specified."))
        actual = plain_error(outcome.error) if outcome.error else "The test failed without a captured reason."
        kind = getattr(finding, "kind", "unclear") if finding else "unclear"
        if outcome.invalid_reason:
            category = "test_defect"
            description = f"{outcome.invalid_reason} This is a defect in the automated test, not in the application."
            impact = f'The test "{title}" on {_page_of(outcome.url)} is wrong, so it says nothing about the application; it is left out of the failure count and the release decision.'
        else:
            category = policy.category_for(kind)
            description = f"{actual} Failure class: {kind}." if finding else actual
            impact = (f'Visitors doing "{title}" from {_page_of(outcome.url)} may not be able to finish it; this run failed to confirm the journey works.'
                      if flow else f'The check "{title}" on {_page_of(outcome.url)} failed, so that part of the page is not confirmed to work.')
        raw.append(_make_record(
            run, report_dir, category=category, failure_class="test_defect" if category == "test_defect" else kind, scope=scope,
            title=title, description=description, steps=steps, expected=expected, actual=actual, url=outcome.url,
            flow_id=flow_id, evidence=evidence, trace=trace, video=video, impact=impact,
            failure_text=f"{outcome.error or ''}\n{actual}", assertion_text=outcome.assertions[-1] if outcome.assertions else ""))
        direct_keys.add((scope, outcome.title, outcome.url))
        if flow:
            direct_keys.add((scope, flow.title, outcome.url))

    for failure in run.generation_failures:
        raw.append(_make_record(
            run, report_dir, category="tooling", failure_class="generation_failed", scope="page",
            title=f"No test was generated for {failure['url']}", description=failure["error"],
            steps=[f"Open {failure['url']}.", "Run the generate stage for this URL."],
            expected="A validated spec for the page.", actual=failure["error"], url=failure["url"],
            impact=f"{_page_of(failure['url'])} has no tests at all this run, so it is uncovered."))

    # Findings from authentication history or capture/localization checks do not always have a pytest outcome of
    # their own; they still get the same complete record shape.
    for finding in run.findings:
        if (finding.scope, finding.test, finding.url) in direct_keys:
            continue
        category = policy.category_for(finding.kind)
        raw.append(_make_record(
            run, report_dir, category=category, failure_class=finding.kind or "unclear", scope=finding.scope,
            title=finding.summary or "A recorded validation finding needs review.",
            description=finding.summary or "A recorded validation finding needs review.",
            steps=[finding.repro or f"Open {finding.url or 'the affected page'}."],
            expected="The recorded check should complete without this finding.", actual=finding.summary or "NOT CAPTURED",
            url=finding.url, flow_id=finding.test if finding.scope == "flow" else "NOT CAPTURED",
            impact=f'A {finding.kind or "validation"} finding on {_page_of(finding.url)}: {_full(finding.summary or "see the finding")}. It needs review before the release decision can be trusted.'))

    for flow in run.untested_flows:
        failed_check = flow.verify_failed         # its last real check failed: a failure, not merely "could not run"
        failure_class = ("failed_last_check" if failed_check else "human_input_required" if flow.needs_person else
                         "inconclusive" if flow.inconclusive else "blocked_flow")
        category = policy.category_for(failure_class)
        title = _plain_flow_title(flow.title)
        raw.append(_make_record(
            run, report_dir, category=category, failure_class=failure_class, scope="flow", title=title,
            description=f"This user journey has no test result in this run: {flow.not_run_reason}.",
            steps=[f"Open {flow.start_url}.", f"Attempt journey {flow.flow_id}."],
            expected=flow.expected or "The journey should complete and produce a verified result.",
            actual=flow.observed or "The journey was not completed.", url=flow.start_url, flow_id=flow.flow_id,
            evidence=flow.outcome.evidence[0] if flow.outcome and flow.outcome.evidence else None,
            failure_text=f"{flow.not_run_reason}\n{flow.observed}",
            impact=f'Visitors cannot be shown to complete "{title}" from {_page_of(flow.start_url)}; until it has a passing test, releasing carries that risk.',
            reason_detail="CAPTCHA" if flow.needs_person else ""))

    for page in run.human_input_pages:
        raw.append(_make_record(
            run, report_dir, category="limitation", failure_class="human_input_required", scope="page",
            title=f'A person is needed to complete the form at {page["url"]}',
            description=page.get("detail", "A human-only step was detected."),
            steps=[f'Open {page["url"]}.', f'Complete the {page["reason"]} step manually.'],
            expected="The form should be completed and submitted.",
            actual=f'The run stopped at {page["reason"]}; the step was not automated.', url=page["url"],
            mitigation=(f'Not an application defect: an automated test cannot pass a {page["reason"]}. Ask the site owner for a staging '
                        "bypass (test key or disabled check) so the form can be automated, or run it with someone at the terminal: a window then shows the form and "
                        "the CAPTCHA and a person types the code during the run."),
            impact=f'The form at {_page_of(page["url"])} needs a {page["reason"]} step that no automated test can pass, so submitting it is unverified.',
            reason_detail=page["reason"]))

    for wall in run.untested_auth:
        raw.append(_make_record(
            run, report_dir, category="limitation", failure_class="authentication_required", scope="page",
            title=f'Sign-in wall at {wall["url"]}: pages behind it were not tested',
            description="A login or sign-up form was found, so content behind it was not exercised.",
            steps=[f'Open {wall["url"]}.', "Sign in or create the account required by the form."],
            expected="The authenticated area should be available for testing.",
            actual="The run could not proceed beyond the authentication form.", url=wall["url"],
            mitigation="Provide a dedicated test account for the areas behind a sign-in, so those pages are tested.",
            impact=f'Pages behind the sign-in at {_page_of(wall["url"])} were not tested, so anything a signed-in visitor does there is unverified.'))

    counters = {c: 0 for c in policy.CATEGORIES}
    records: list[dict[str, Any]] = []
    for category in policy.CATEGORIES:
        for record in raw:
            if record["category"] == category:
                counters[category] += 1
                record["id"] = f"{policy.PREFIX[category]}-{counters[category]:03d}"
                records.append(record)
    policy.assign_owners(records, run.owners, _run_date(run))
    policy.assert_priorities_vary(records)
    return records


def _page_of(url: str | None) -> str:
    """The path part of a URL for prose ("/en/subscribe"); the whole value when it has none."""
    return urlsplit(url or "").path or (url or "the affected page")


def _coverage_gaps(run: "RunReport") -> list[dict[str, str]]:
    if not run.coverage:
        return []
    gaps: list[dict[str, str]] = []
    n = 1
    for page in run.coverage.pages:
        if not page.visited or page.untouched:
            gaps.append({
                "id": f"COV-{n:03d}",
                "page": page.path,
                "url": page.url,
                "impact": "Some page behavior was not exercised by a passing user journey.",
                "untouched_controls": "; ".join(page.untouched) or "The page was not visited by a tested flow.",
                "what_to_review": ("Open the page and use each listed control once, or write a journey that does."
                                   if page.untouched else "Visit the page and check its main content; no journey reached it."),
            })
            n += 1
    return gaps


def _requirement_stats(run: "RunReport") -> dict[str, Any]:
    """Written requirements and what this run says about each; one source for section 1 and the section 6 table."""
    by_id = {f.flow_id: f for f in run.flow_reports}
    rows = []
    for req in run.requirements:
        flow = by_id.get(req.get("flow_id") or "")
        result = flow.run_label if flow else "No flow, so nothing was tested"
        status = str(req.get("status", "new"))
        reason = ""
        if status == "unbuildable":
            reason = str(req.get("reason") or "").strip() or "the expand step recorded no reason"
        rows.append({"id": req["id"], "sentence": _full(req.get("sentence", "")), "source": str(req.get("source", "?")),
                     "flow_built": bool(flow), "status": status, "reason": reason, "result": result,
                     "passed": result == "Passed"})
    total = len(rows)
    not_backed = sum(not r["passed"] for r in rows)
    return {"total": total, "not_backed": not_backed, "percent": round(100 * not_backed / total) if total else 0, "rows": rows}


def _warning_items(run: "RunReport") -> list[tuple[str, str]]:
    """(subject, raw warning) - the subject says WHICH page or journey the warning is about."""
    items: list[tuple[str, str]] = []
    for report in run.url_reports:
        items.extend((f"page {report.url}", w) for w in report.warnings)
    for flow in run.tested_flows:
        items.extend((f'journey "{_plain_flow_title(flow.title)}" ({flow.flow_id})', w) for w in flow.warnings)
    items.extend(("report generation", note) for note in run.notes)
    return items


def _risk_items(run: "RunReport", records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Every risk or coverage note, each with an ID and the subject it is about."""
    found: list[tuple[str, str, str]] = []
    for flap in run.flapping:
        found.append(("Stability risk", flap.test,
                      f"Recent results alternate between pass and fail ({flap.sequence}, oldest to newest); retest before relying on it."))
    for subject, warning in _warning_items(run):
        kind, text = _plain_warning(warning)
        found.append((kind, subject, text))
    device = (run.browser_info or {}).get("device")
    found.append(("Coverage risk", "Device and browser coverage",
                  (f"Only the {device} profile ran; desktop and other browsers were not executed." if device else
                   "Mobile viewport coverage was not executed; responsive and mobile release risk remains unverified.")))
    human = [r for r in records if r["failure_class"] == "human_input_required" and r["category"] == "limitation"]
    if human:
        found.append(("Needs a human", "CAPTCHA and verification-code forms",
                      f"{len(human)} form step(s) require manual completion; those steps were not automated ({policy.id_range([r['id'] for r in human])}, section 5)."))
    walls = [r for r in records if r["failure_class"] == "authentication_required"]
    if walls:
        found.append(("Not tested", "Sign-in walls",
                      f"{len(walls)} login or sign-up form(s) found; content behind them was not exercised ({policy.id_range([r['id'] for r in walls])}, section 5)."))
    unique = list(dict.fromkeys(found))
    return [{"id": f"RSK-{i:03d}", "kind": kind, "subject": subject, "text": text} for i, (kind, subject, text) in enumerate(unique, 1)]


def _run_log_lines(run: "RunReport") -> list[str]:
    """Operational log lines (sign-in attempts, observed form fields): appendix material, not a risk."""
    lines = [f"Authentication: {line}" for line in run.auth_lines]
    for wall in run.untested_auth:
        lines.append(f'Authentication form fields observed at {wall["url"]}: {", ".join(wall["fields"]) or "NOT CAPTURED"}.')
    return lines


def _plain_warning(warning: str) -> tuple[str, str]:
    text = warning
    if "limited coverage is expected" in text:
        return "Coverage note", "Some page behavior could not be driven automatically; this is informational, not a confirmed defect."
    if "shallow coverage" in text:
        return "Coverage risk", "The page has more controls than the passing journeys exercised."
    if "only proves the URL changed" in text:
        return "Coverage risk", "The journey reached a new address, but the resulting content was not confirmed."
    if "inconsistent" in text or "flapping" in text:
        return "Stability risk", "The same journey has produced different results and should be retested."
    # Unknown warnings can contain selectors, raw URLs, or model-generated text.  Keep
    # that detail in the appendix/run log; the stakeholder section should remain plain
    # language and should never accidentally expose an implementation detail.
    return "Review item", "A validation item needs manual review; see the appendix for details." if text else "NOT CAPTURED"


def _run_snapshot(run: "RunReport", counts: dict[str, Any], records: list[dict[str, Any]], env: dict[str, str]) -> dict[str, Any]:
    tests: dict[str, dict[str, str]] = {}
    for scope, key, outcome, flow in _all_outcomes(run):
        tests[policy.stable_test_key(outcome.nodeid or key)] = {
            "title": flow.title if flow else outcome.title.replace("_", " "), "url": outcome.url, "status": outcome.status,
            "scope": scope, "file": _row_file({"nodeid": outcome.nodeid})}
    pages = [r.url for r in run.url_reports] + [f.start_url for f in run.flow_reports]
    pages += [p.url for p in (run.coverage.pages if run.coverage else [])]
    tooling_urls = [r["url"] for r in records if r["category"] == "tooling" and r["url"]]
    untested_files = [flow_spec_name({"id": f.flow_id}) for f in run.untested_flows]
    kept = {k: env.get(k, "") for k in ("Browser", "Browser version", "Playwright")}
    return policy.snapshot(run_id=run.run_id, finished_at=_utc_timestamp(run.finished_at), counts=counts, tests=tests,
                           pages=pages, tooling_urls=tooling_urls, untested_files=untested_files, env=kept)


def _report_data(run: "RunReport", report_dir: Path) -> dict[str, Any]:
    counts = canonical_counts(run)
    records = _defect_records(run, report_dir)
    gaps = _coverage_gaps(run)
    tax = policy.taxonomy_counts(records)
    recommendation = policy.release_decision(records)
    conditions = policy.build_conditions(records, [g["id"] for g in gaps], recommendation)
    env = environment_block(run)
    snap = _run_snapshot(run, counts, records, env)
    delta = policy.compute_delta(policy.previous_run(policy.load_history(Path(report_dir) / policy.HISTORY_FILE), run.run_id), snap)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema_version": "2.0",
        "generated_at": now,
        "site": run.base_url or "NOT CAPTURED",
        "run_id": run.run_id,
        "run_started_at": _utc_timestamp(run.started_at),
        "run_finished_at": _utc_timestamp(run.finished_at),
        "run_duration_seconds": _duration_seconds(run.started_at, run.finished_at),
        "counts": counts,
        "taxonomy": tax,
        "recommendation": recommendation,
        "conditions": conditions,
        "exit_criteria": policy.exit_criteria(records, [g["id"] for g in gaps], recommendation),
        "defects": records,
        "delta": delta,
        "requirements": _requirement_stats(run),
        "risks": _risk_items(run, records),
        "run_log": _run_log_lines(run),
        "coverage": {
            "pages_total": run.coverage.pages_total if run.coverage else 0,
            "pages_visited": run.coverage.pages_visited if run.coverage else 0,
            "controls_total": run.coverage.controls_total if run.coverage else 0,
            "controls_touched": run.coverage.controls_touched if run.coverage else 0,
            "tested_flows": run.coverage.tested_flows if run.coverage else 0,
            "planned_flows": run.coverage.planned_flows if run.coverage else 0,
            "gaps": gaps,
        },
        "environment": env,
        "application_build": run.app_build,
        "snapshot": snap,
    }


def _utc_timestamp(value: str | None) -> str:
    if not value:
        return "NOT CAPTURED"
    try:
        raw = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return "NOT CAPTURED"


def _duration_seconds(start: str | None, finish: str | None) -> float | None:
    if not start or not finish:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(finish.replace("Z", "+00:00"))
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return round((b - a).total_seconds(), 2)
    except ValueError:
        return None


def _evidence_cell(cell, record: dict[str, Any]) -> None:
    """Resolvable links, one per artifact, or the literal reason there is none."""
    cell.text = ""
    links = [(label, record.get(key)) for label, key in (("Screenshot", "screenshot_url"), ("Trace", "trace_url"), ("Video", "video_url"))
             if record.get(key) not in (None, "", "NOT CAPTURED")]
    if not links:
        cell.paragraphs[0].add_run(record.get("evidence_reason") or "NOT CAPTURED")
        return
    for index, (label, url) in enumerate(links):
        paragraph = cell.paragraphs[0] if index == 0 else cell.add_paragraph()
        paragraph.add_run(f"{label}: ")
        _add_hyperlink(paragraph, url, url)


def _record_table(document, rows: list[dict[str, Any]], spec: list[tuple[str, Any, float]]) -> None:
    """spec = (header, getter or "evidence", width in inches). A narrow ID column and a wide title column, so a
    title is never cut."""
    table = _grid(document, tuple(header for header, _, _ in spec))
    _set_table_widths(table, tuple(width for _, _, width in spec))
    for record in rows:
        for cell, (_, getter, _) in zip(table.add_row().cells, spec):
            if getter == "evidence":
                _evidence_cell(cell, record)
            else:
                cell.text = str(getter(record))


def _compact_defects(document, defects: list[dict[str, Any]]) -> None:
    """Kept for callers that only want one summary row per record."""
    _record_table(document, defects, [("ID", lambda d: _display_id(d["id"]), 0.8), ("What went wrong", lambda d: d["actual"], 2.6),
                                      ("Impact", lambda d: d.get("business_impact", ""), 2.2), ("Evidence", "evidence", 1.4)])


def _findings_section(document, run: RunReport, report_data: dict[str, Any] | None = None, *, compact: bool = False) -> None:
    """The Defect Report: three tables that decide the verdict (Defects, Unverified, Limitations), then the Tooling
    issues that say nothing about the application. Only the Defects table can produce NO-GO."""
    data = report_data or _report_data(run, Path.cwd())
    records = data["defects"]
    tax = data["taxonomy"]
    by_cat = {c: [r for r in records if r["category"] == c] for c in policy.CATEGORIES}
    document.add_paragraph(policy.taxonomy_sentence(tax))
    document.add_paragraph(
        "Release rule: any open High or Critical Defect is NO-GO. With no such Defect, any other open Defect or any open Unverified journey "
        "is CONDITIONAL GO. Limitations, test defects and tooling issues never change the verdict, and none of them is a release blocker.")
    if not records:
        document.add_paragraph("No open defects, unverified journeys or limitations were recorded in this run. No failures to triage this run.")
        return

    document.add_heading("Defects", 2)
    document.add_paragraph("Something ran and was broken. Severity reflects user impact: a core page element that does not render is "
                           "Critical, landing on the wrong address is High, a cosmetic or secondary control is Medium.")
    if by_cat["defect"]:
        _record_table(document, by_cat["defect"], [
            ("ID", lambda d: _display_id(d["id"]), 0.75), ("Severity", lambda d: d["severity"], 0.7),
            ("Priority", lambda d: d["priority"].split(" - ")[0], 0.5), ("Title", lambda d: d["title"], 1.9),
            ("Page", lambda d: d["url"], 1.0), ("Evidence", "evidence", 1.55)])
    else:
        document.add_paragraph("0 application defects: no test that ran found the application broken.")

    document.add_heading("Unverified journeys", 2)
    document.add_paragraph("The journey could not be confirmed either way. Severity is the business value of the journey (default Medium). "
                           "These are not defects and not release blockers; they need a manual pass.")
    if by_cat["unverified"]:
        _record_table(document, by_cat["unverified"], [
            ("ID", lambda d: _display_id(d["id"]), 0.75), ("Severity", lambda d: d["severity"], 0.7),
            ("Priority", lambda d: d["priority"].split(" - ")[0], 0.5), ("Journey", lambda d: d["title"], 1.9),
            ("Why unverified", lambda d: d["actual"], 1.0), ("Evidence", "evidence", 1.55)])
    else:
        document.add_paragraph("No journey is unverified.")

    document.add_heading("Limitations", 2)
    document.add_paragraph("The tooling cannot test these (CAPTCHA, sign-in, no test account). Always Low severity and never P1; "
                           "each has its own ID and URL so identical wording can be told apart.")
    if by_cat["limitation"]:
        _record_table(document, by_cat["limitation"], [
            ("ID", lambda d: _display_id(d["id"]), 0.75), ("Severity", lambda d: d["severity"], 0.55),
            ("Title", lambda d: d["title"], 1.5), ("URL", lambda d: d["url"], 1.4),
            ("How to unblock", lambda d: d.get("mitigation", d["actual"]), 1.3), ("Evidence", "evidence", 1.2)])
    else:
        document.add_paragraph("No limitation was recorded.")

    document.add_heading("Tooling issues", 2)
    document.add_paragraph("Failures of the test harness and tests that are themselves wrong. They are not test results: they are left out "
                           "of the failure count and the release decision, and each affected page or journey is uncovered until it is repaired.")
    harness = by_cat["test_defect"] + by_cat["tooling"]
    if harness:
        _record_table(document, harness, [
            ("ID", lambda d: _display_id(d["id"]), 0.75), ("Kind", lambda d: policy.CATEGORY_LABEL[d["category"]], 0.8),
            ("Title", lambda d: d["title"], 1.6), ("URL", lambda d: d["url"], 1.2),
            ("What happened", lambda d: d["description"], 1.5), ("Evidence", "evidence", 1.2)])
    else:
        document.add_paragraph("No tooling issue was recorded.")
    if not compact:
        _defect_detail_tables(document, records)


def _defect_detail_tables(document, defects: list[dict[str, Any]]) -> None:
    for defect in defects:
        document.add_heading(f'{defect["id"]} — {defect["title"]}', 3)
        table = _grid(document, ("Field", "Details"))
        fields = (
            ("Type", policy.CATEGORY_LABEL[defect["category"]]),
            ("Severity (how bad)", defect["severity"]),
            ("Priority (how urgent)", defect.get("priority", "NOT CAPTURED")),
            ("Description", defect["description"]),
            ("Steps to reproduce", "\n".join(f"{i}. {s}" for i, s in enumerate(defect["steps_to_reproduce"], 1))),
            ("Expected", defect["expected"]),
            ("Actual", defect["actual"]),
            ("Status", defect["status"]),
            ("Owner", defect["owner"]),
            ("Due", defect.get("due", "NOT CAPTURED")),
            ("Linked flow", defect["linked_flow_id"]),
            ("Evidence", None),
        ) + ((("Mitigation", defect["mitigation"]),) if defect.get("mitigation") else ())
        for label, value in fields:
            cells = table.add_row().cells
            cells[0].text = label
            if label == "Evidence":
                _evidence_cell(cells[1], defect)
                if defect.get("trace_url", "NOT CAPTURED") != "NOT CAPTURED":
                    cells[1].add_paragraph("Open a trace with: python -m playwright show-trace <file>; it holds the page snapshots, console and network log.")
            else:
                cells[1].text = str(value)
        document.add_paragraph(f'Business impact: {defect.get("business_impact", "NOT CAPTURED")}')


def _write_execution_chart(report_dir: Path, counts: dict[str, Any]) -> Path | None:
    """Create a dependency-light PNG so export cannot corrupt the summary chart."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        width, height = 760, 360
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        values = [("Passed", int(counts["passed"]), (35, 132, 67)),
                  ("Failed", int(counts["failed"]), (179, 38, 26)),
                  ("Skipped", int(counts["skipped"]), (138, 109, 0)),
                  ("Journeys with no result", int(counts["blocked_flows"]), (90, 90, 90))]
        maximum = max(1, max(v for _, v, _ in values))
        draw.text((30, 22), "Run result breakdown", fill="black", font=font)
        bar_left, bar_top, bar_width, bar_height = 170, 55, 480, 42
        for index, (label, value, color) in enumerate(values):
            y = bar_top + index * 68
            draw.text((10, y + 14), label, fill="black", font=font)
            draw.rectangle((bar_left, y, bar_left + bar_width, y + bar_height), outline=(210, 210, 210))
            draw.rectangle((bar_left, y, bar_left + round(bar_width * value / maximum), y + bar_height), fill=color)
            draw.text((bar_left + bar_width + 15, y + 14), str(value), fill="black", font=font)
        path = report_dir / "execution-summary.png"
        image.save(path, format="PNG")
        return path
    except Exception:
        return None


def _risks_section(document, run: RunReport, report_data: dict[str, Any], *, lean: bool = False) -> None:
    document.add_heading("Risks & Issues", 1)
    defects = report_data["defects"]
    document.add_paragraph(policy.taxonomy_sentence(report_data["taxonomy"]))
    # A release blocker is only an open High or Critical application defect (the same "blocking" flag the Defect
    # Report carries). Unverified journeys and limitations are never blockers.
    blockers = [d for d in defects if d.get("blocking")]
    document.add_heading("Blockers", 2)
    if blockers and lean:
        document.add_paragraph(f"{len(blockers)} release blocker(s) ({policy.id_range([d['id'] for d in blockers])}); each is listed with its impact in section 5.")
    elif blockers:
        for defect in blockers:
            document.add_paragraph(f'{defect["id"]}: {defect["title"]} — {defect["business_impact"]}', style="List Bullet")
    else:
        document.add_paragraph("No release blockers were recorded. Only an open High or Critical application defect blocks a release; "
                               "unverified journeys and limitations never do.")
    document.add_heading("Risks and coverage notes", 2)
    risks = report_data["risks"]
    for risk in risks:
        document.add_paragraph(f'{risk["id"]} | {risk["kind"]} | {risk["subject"]}: {risk["text"]}', style="List Bullet")
    if not risks:
        document.add_paragraph("No risks or coverage notes were recorded.")
    document.add_paragraph("Sign-in attempts and the form fields observed are operational log lines; they are in the appendix (Run log).")


def _test_index_section(document, run: RunReport) -> None:
    """Append a complete, uniquely identified test index with plain expected/actual text."""
    document.add_heading("Complete test index", 2)
    table = _grid(document, ("Test ID", "Scope", "Test name", "URL", "Expected", "Observed", "Result"))
    _set_table_widths(table, (0.65, 0.5, 1.25, 1.0, 1.15, 1.25, 0.55))
    for number, (scope, key, outcome, flow) in enumerate(_all_outcomes(run), 1):
        cells = table.add_row().cells
        title = flow.title if flow else outcome.title.replace("_", " ")
        expected = flow.expected if flow and flow.expected else (_plain_assertion(outcome.assertions[0]) if outcome.assertions else "(no assertion captured)")
        observed = flow.observed if flow and flow.observed else (plain_skip_reason(outcome.error) if outcome.status == "skipped" else ("As expected." if outcome.passed else plain_error(outcome.error)))
        values = (_display_id(f"T-{number:03d}"), scope, title, outcome.url, expected, observed or "NOT CAPTURED", outcome.status.upper())
        for cell, value in zip(cells, values):
            cell.text = value


def _evidence_hash(path: str) -> str:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "NOT CAPTURED"


# ------------------------------------------------------------------ plain-language translation for the main
# body (Test Execution Summary, Defect Report). The generator/flowgen templates only ever emit a small, known
# set of expect(...) methods and pytest failure shapes (see tests_python for the full list this was built
# against) - translated by pattern, never guessed. The raw Playwright code and pytest trace are never lost:
# they stay exactly as captured in the Appendix (assertions_for / _render_failure_detail).

_LOCATOR_ROLE = re.compile(r"get_by_role\(\s*['\"]([a-z]+)['\"][^)]*?name\s*=\s*['\"]([^'\"]*)['\"]", re.I)
_LOCATOR_TEXT = re.compile(r"get_by_text\(\s*['\"]([^'\"]*)['\"]")
_LOCATOR_LABEL = re.compile(r"get_by_label\(\s*['\"]([^'\"]*)['\"]")
_LOCATOR_CSS = re.compile(r"\.locator\(\s*['\"]([^'\"]+)['\"]")


def _plain_locator(code: str) -> str:
    """What the assertion is about, in words - from the same source line, no separate lookup needed."""
    if m := _LOCATOR_ROLE.search(code):
        role, name = m.group(1), m.group(2)
        return f'the "{name}" {role}' if name else f"the {role}"
    if m := _LOCATOR_TEXT.search(code):
        return f'the text "{m.group(1)}"' if m.group(1) else "the text"
    if m := _LOCATOR_LABEL.search(code):
        return f'the "{m.group(1)}" field'
    if m := _LOCATOR_CSS.search(code):
        return f"the element matching {m.group(1)}"
    return "it"


_ASSERT_METHOD = re.compile(r"\.(not_)?to_(be_visible|be_checked|be_in_viewport|be_enabled|be_disabled|"
                            r"be_empty|have_url|have_count|have_value|have_attribute)\(([^)]*)\)")


def _plain_assertion(code: str) -> str:
    """One sentence for an Expected/Actual column, built from the source line itself - never the raw Python."""
    match = _ASSERT_METHOD.search(code or "")
    if not match:
        return _shorten(code, 140)
    negated, method, args = bool(match.group(1)), match.group(2), match.group(3).strip()
    subject = _plain_locator(code)
    subject_cap = subject[0].upper() + subject[1:]      # capitalize() would also lowercase a real name like "Passcode"
    verb = "should not" if negated else "should"
    if method == "be_visible":
        return f"{subject_cap} {verb} be visible on the page."
    if method == "be_checked":
        return f"{subject_cap} {verb} be checked."
    if method == "be_in_viewport":
        return f"{subject_cap} {verb} be scrolled into view."
    if method == "be_enabled":
        return f"{subject_cap} {verb} be enabled."
    if method == "be_disabled":
        return f"{subject_cap} {verb} be disabled."
    if method == "be_empty":
        return f"{subject_cap} {verb} be empty."
    if method == "have_url":
        # args may be truncated (a nested re.compile(...) has its own closing paren), so read the literal
        # straight out of the full source line instead of the captured args.
        literal = re.search(r"to_have_url\([^'\"]*['\"]([^'\"]+)['\"]", code)
        pattern = (literal.group(1) if literal else args).replace("\\.", ".").replace("\\-", "-")
        path = re.sub(r"/\?\(\?:\[\?#\]\.\*\)\?\$?$", "", pattern).lstrip("^") or pattern
        return f"The page {verb} end up at {path}."
    if method == "have_count":
        n = args.strip() or "0"
        return f"{n} of {subject} {verb} be present."
    if method == "have_value":
        value = args.strip("'\" ") or "(empty)"
        return f'{subject_cap} {verb} show "{value}".' if value != "(empty)" else f"{subject_cap} {verb} be left empty."
    if method == "have_attribute":
        parts = [a.strip().strip("'\"") for a in args.split(",", 1)]
        attr = parts[0] if parts else "attribute"
        return f'{subject_cap} {verb} have a "{attr}" attribute.'
    return _shorten(code, 140)


def _page_row(outcome: TestOutcome) -> tuple[str, str, str, str, str, str]:
    """(scope, test, url, expected, observed, verdict) for a page-level test. A pytest 'skipped' status (the
    validator refused to emit an unsafe spec) is its own verdict - it is not a failure, and its raw skip reason
    is a (file, line, message) tuple, not an error trace, so it needs its own plain-language reading too."""
    if outcome.status == "skipped":
        return ("page", outcome.title.replace("_", " "), outcome.url, "(no test could be safely generated)",
                plain_skip_reason(outcome.error), "SKIPPED")
    if outcome.assertions:
        extra = f" (+{len(outcome.assertions) - 1} more)" if len(outcome.assertions) > 1 else ""
        expected = _plain_assertion(outcome.assertions[0]) + extra
    else:
        expected = "(no assertion captured)"
    observed = "As expected." if outcome.passed else (plain_error(outcome.error) or outcome.status)
    verdict = "PASSED" if outcome.passed else "FAILED"
    return "page", outcome.title.replace("_", " "), outcome.url, expected, observed, verdict


def _flow_row(flow: FlowReport) -> tuple[str, str, str, str, str, str]:
    verdict = "SKIPPED" if flow.outcome and flow.outcome.status == "skipped" else ("PASSED" if flow.passed else "FAILED")
    return "flow", flow.title, flow.start_url, flow.expected or "(not stated)", flow.observed or "(never ran)", verdict


def _defect_id_for(defects: list[dict[str, Any]], key: str, flow: FlowReport | None) -> str:
    """The BUG id recorded for this failing test, so a row in "Tests requiring attention" can be found in the defect report."""
    for defect in defects:
        if flow and defect.get("linked_flow_id") == flow.flow_id and defect.get("scope") == "flow":
            return defect["id"]
        if any(step == f"Run test {key}." for step in defect.get("steps_to_reproduce", [])):
            return defect["id"]
    return "-"


def _delta_block(document, delta: dict[str, Any], counts: dict[str, Any]) -> None:
    """What changed since the previous run. A pass-rate movement without this block cannot be explained, so it is
    always printed: either the comparison, or the statement that this run is the first."""
    document.add_heading("Change since the previous run", 2)
    if not delta.get("baseline"):
        document.add_paragraph(delta["note"])
        return
    document.add_paragraph(
        f'Compared with {delta["previous_run_id"]} (finished {delta.get("previous_finished_at") or "at an unrecorded time"}): '
        f'pass rate {delta["previous_pass_rate_percent"]}% -> {counts["pass_rate_percent"]}%, '
        f'tests that ran {delta["previous_tests_ran"]} -> {counts["executed_tests"]}.')
    table = _grid(document, ("Change", "Tests"))
    for label, key in (("Tests added", "added"), ("Tests removed", "removed"), ("Failures now passing", "fixed"),
                       ("Failures still open", "still_failing"), ("New failures", "new_failures")):
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, str(len(delta[key]))
    if delta.get("renamed_count"):
        document.add_paragraph(f'{delta["renamed_count"]} removed test(s) were renamed, not dropped; they are listed under "Tests removed" with the new name and are not counted as added.')
    for heading, key in (("Tests added", "added"), ("Tests removed", "removed"), ("Failures now passing", "fixed"),
                         ("Failures still open", "still_failing"), ("New failures", "new_failures")):
        rows = delta[key]
        if not rows:
            continue
        document.add_paragraph(f"{heading}:")
        withreason = key == "removed"
        grid = _grid(document, ("Test ID", "Title", "Reason removed") if withreason else ("Test ID", "Title"))
        _set_table_widths(grid, (2.2, 2.4, 1.9) if withreason else (2.6, 3.9))
        for row in rows:
            values = (row["id"], row["title"], row["reason"]) if withreason else (row["id"], row["title"])
            for cell, value in zip(grid.add_row().cells, values):
                cell.text = value


def _test_summary_section(document, run: RunReport, report_data: dict[str, Any] | None = None) -> None:
    """Render the quantitative summary and only the tests needing attention.

    The complete test index is kept in the appendix, where it can be searched
    without making the release decision pages unreadable.
    """
    data = report_data or _report_data(run, Path.cwd())
    if not _all_outcomes(run) and not run.untested_flows:
        return
    counts = data["counts"]
    document.add_heading("Test Execution Summary", 1)
    document.add_heading("Run totals", 2)
    document.add_paragraph(
        f'{counts["executed_tests"]} tests ran ({counts["page_tests"]} page + {counts["flow_tests"]} flow): '
        f'{counts["passed"]} passed and {counts["failed"]} failed. '
        f'{counts["skipped"]} were skipped by the harness and {counts["test_defects"]} were invalid because the test itself was wrong '
        f'(both are Tooling issues in section 5, not failures); {counts["blocked_flows"]} user journeys had no result. '
        f'Pass rate among tests that ran: {counts["pass_rate_percent"]}%. That rate ignores journeys with no result, so read it with the '
        f'journey figure: {counts["journeys_passed"]} of {counts["journeys_total"]} user journeys passed '
        f'({counts["journeys_passed_percent"]}%).'
    )
    document.add_paragraph(
        f'What is counted: {counts["page_tests"]} page test(s) plus {counts["flow_tests"]} flow test(s) ran. Each flow test is one user journey; '
        f'{counts["flows_with_result"]} of the {counts["journeys_total"]} journeys have a result in this run (the same figure section 6 uses), '
        "and a journey with no flow test has no result.")
    metrics = _grid(document, ("Metric", "Value"))
    for label, value in (("Tests that ran", counts["executed_tests"]), ("Page tests that ran", counts["page_tests"]),
                         ("Flow tests that ran", counts["flow_tests"]), ("Passed", counts["passed"]),
                         ("Failed (application)", counts["failed"]), ("Skipped by the harness", counts["skipped"]),
                         ("Invalid (the test itself is wrong)", counts["test_defects"]),
                         ("User journeys with no result this run", counts["blocked_flows"]),
                         ("Pass rate (tests that ran only)", f'{counts["pass_rate_percent"]}%'),
                         ("User journeys passed (of all journeys)", f'{counts["journeys_passed"]} of {counts["journeys_total"]} ({counts["journeys_passed_percent"]}%)'),
                         ("...of which only proved the URL changed", sum(f.passed and f.navigation_only for f in run.flow_reports))):
        cells = metrics.add_row().cells
        cells[0].text, cells[1].text = label, str(value)
    nav_only = sum(f.passed and f.navigation_only for f in run.flow_reports)
    if nav_only:
        document.add_paragraph(
            f"{nav_only} of the passed journeys only prove that the address changed; nothing on the resulting page was checked "
            "(no heading, results or new controls were recorded). They show the click worked, not that the page is right.")
    _delta_block(document, data["delta"], counts)
    attention = []
    for test_number, (scope, key, outcome, flow) in enumerate(_all_outcomes(run), 1):
        if outcome.status not in {"failed", "error"} or outcome.invalid_reason:
            continue
        title = flow.title if flow else outcome.title.replace("_", " ")
        expected = flow.expected if flow and flow.expected else (_plain_assertion(outcome.assertions[0]) if outcome.assertions else "NOT CAPTURED")
        observed = flow.observed if flow and flow.observed else plain_error(outcome.error)
        attention.append((_display_id(f"T-{test_number:03d}"), _display_id(_defect_id_for(data["defects"], key, flow)), scope, title, outcome.url, expected, observed or "NOT CAPTURED", outcome.status.upper()))
    if attention:
        document.add_heading("Tests requiring attention", 2)
        table = _grid(document, ("Test ID", "Defect", "Scope", "Test name", "URL", "Expected", "Observed", "Result"))
        _set_table_widths(table, (0.6, 0.6, 0.45, 1.15, 0.95, 1.05, 1.15, 0.5))
        for row in attention:
            cells = table.add_row().cells
            for cell, value in zip(cells, row):
                cell.text = value
    else:
        document.add_paragraph("No failed application tests require attention in this run.")


def _flows_table(document, flows: list[FlowReport]) -> None:
    table = _grid(document, ("Flow", "Stored status", "This run", "Pages"))
    for flow in flows:
        cells = table.add_row().cells
        cells[0].text = flow.title
        cells[1].text = {"verified": "Verified", "approved": "Approved", "stale": "Needs retesting", "candidate": "Not yet verified"}.get(flow.status, "Not recorded")
        cells[2].text = flow.run_label
        cells[3].text = _ARROW.join(flow.pages) or "-"


def _flows_section(document, flows: list[FlowReport], *, embed: bool, link_base: Path | None) -> None:
    if not flows:
        return
    document.add_heading("User flows", 1)
    document.add_paragraph(
        "Each flow is one user journey, tested by a generated spec whose steps and assertions come from a real "
        "verified browser run. The title is the flow's plain-language description.")
    _flows_table(document, flows)
    for flow in flows:
        document.add_page_break()
        _render_flow(document, flow, embed=embed, link_base=link_base)


_CRITICAL_CONTROL = re.compile(r"submit|subscribe|search|send|register|sign|log ?in|pay|buy|confirm|request|save|checkout|download|install|tune", re.I)


def _critical_untouched(coverage: Coverage) -> list[tuple[str, str]]:
    """(page, control) for every untouched control whose name suggests the page's main action - the untested parts
    a release decision actually needs to know about, rather than a bare percentage."""
    return [(page.path, control) for page in coverage.pages for control in page.untouched if _CRITICAL_CONTROL.search(control)]


def _coverage_section(document, run: RunReport, report_data: dict[str, Any]) -> None:
    """How much of the explored site the tested flows touch, which controls nothing exercised (COV rows), and which
    pages the harness left uncovered."""
    coverage = run.coverage
    counts = report_data["counts"]
    tooling = [r for r in report_data["defects"] if r["category"] in {"tooling", "test_defect"} and r["url"]]
    if (coverage is None or not coverage.pages) and not tooling:
        return
    document.add_heading("Test Coverage / Requirements Traceability", 1)
    if coverage is not None and coverage.pages:
        document.add_heading("Flow coverage", 2)
        document.add_paragraph(
            f"Pages visited by a tested flow: {coverage.pages_visited} of {coverage.pages_total} ({coverage.percent_pages}%). "
            f"Content controls a tested flow acts on: {coverage.controls_touched} of {coverage.controls_total} "
            f"({coverage.percent_controls}%). Journeys with a test result in this run: {counts['flows_with_result']} of {counts['journeys_total']} "
            f"(the flow figure of section 4). Stored status: {coverage.tested_flows} verified or approved, "
            f"{coverage.planned_flows} written but not backed by a passing run.")
        document.add_paragraph(
            "Method: the page denominator is the number of unique explored URL paths after redirect aliases are removed. "
            "The control denominator is the number of visible, enabled content links, buttons, selects, and fields recorded "
            "by the explorer; repeated header, navigation, and footer controls are excluded. The raw URL and control lists "
            "are included in the machine-readable report-data.json and the coverage appendix.")
        document.add_paragraph("Requirements and user stories are represented by the plain-language flow goals. A goal is traceable only when its flow is verified or approved; unverified goals remain coverage gaps.")
        critical = _critical_untouched(coverage)
        document.add_paragraph("The percentages are a rough guide. What matters for a release decision is which specific controls nothing tested:")
        gaps = report_data["coverage"]["gaps"]
        if gaps:
            document.add_paragraph(
                f"{len(critical)} untested control(s) look like a page's main action (submit, search, subscribe and similar)." if critical else
                "No untested control looks like a page's main action (submit, search, subscribe and similar).")
            _record_table(document, gaps, [("ID", lambda g: _display_id(g["id"]), 0.75), ("Page", lambda g: g["page"], 1.3),
                                           ("Controls not exercised by any journey", lambda g: g["untouched_controls"], 2.6),
                                           ("What to review", lambda g: g["what_to_review"], 1.85)])
        else:
            document.add_paragraph("No page has controls that a tested journey left unexercised.")
        if coverage.aliases:
            document.add_paragraph("Counted once: " + ", ".join(f"{a} redirects to {b}" for a, b in sorted(coverage.aliases.items())))
    if tooling:
        document.add_heading("Pages left uncovered by tooling issues", 2)
        document.add_paragraph("These pages and journeys have no valid test result because the harness failed, not because the application did.")
        _record_table(document, tooling, [("ID", lambda d: _display_id(d["id"]), 0.75), ("URL", lambda d: d["url"], 2.6),
                                          ("Why it is uncovered", lambda d: d["actual"], 3.15)])


def _traceability_section(document, run: RunReport, report_data: dict[str, Any], *, gaps_only: bool = False) -> None:
    """Each written requirement -> the flow built from it -> what this run said. A requirement with no flow, or
    whose flow did not pass, is a coverage gap and is listed as one; an unbuildable one says why."""
    stats = report_data["requirements"]
    if not stats["total"]:
        return
    document.add_heading("Requirements traceability", 2)
    table = _grid(document, ("Requirement", "Source", "Flow built", "This run"))
    _set_table_widths(table, (2.6, 0.8, 1.9, 1.2))
    for row in stats["rows"]:
        if gaps_only and row["passed"]:
            continue
        cells = table.add_row().cells
        cells[0].text = f'{row["id"]}: {row["sentence"]}'
        cells[1].text = row["source"]
        built = "yes" if row["flow_built"] else f'no ({row["status"]})'
        if row["status"] == "unbuildable" and not row["flow_built"]:
            built = f'no (unbuildable): {row["reason"]}'
        cells[2].text = built
        cells[3].text = row["result"]
    document.add_paragraph(f'{stats["not_backed"]} of {stats["total"]} requirement(s) ({stats["percent"]}%) are not backed by a passing result in this run; these are the coverage gaps.'
                           + (" Only the gaps are listed here; the full table is in the appendix." if gaps_only else ""))


def _untested_flows_section(document, flows: list[FlowReport], *, detail: bool = True) -> None:
    if not flows:
        return
    document.add_heading("Flows without a test result in this run", 1)
    document.add_paragraph(
        "These journeys have no test result in this run. \"Stored status\" is what was true at the last check; "
        "\"Latest result\" says why this run has nothing for the flow, so a flow can be Verified earlier and still not run now.")
    table = _grid(document, ("Flow", "Stored status", "Latest result"))
    for flow in flows:
        cells = table.add_row().cells
        cells[0].text = flow.title
        cells[1].text = {"verified": "Verified", "approved": "Approved", "stale": "Needs retesting", "candidate": "Not yet verified"}.get(flow.status, "Not recorded")
        latest = flow.history[-1] if flow.history else "never run"
        cells[2].text = flow.not_run_reason.capitalize() if flow.verify_failed else f"{flow.run_label}: {flow.not_run_reason}"
        if detail:
            document.add_paragraph(f'{flow.title} — Most recent attempt: {latest}')


def _summary_table(document, reports: list[UrlReport]) -> None:
    table = document.add_table(rows=1, cols=4)
    try:
        table.style = "Table Grid"
    except KeyError:
        pass
    for cell, label in zip(table.rows[0].cells, ("URL", "Tests", "Passed", "Failed")):
        cell.paragraphs[0].add_run(label).bold = True
    for report in reports:
        cells = table.add_row().cells
        cells[0].text = report.url
        cells[1].text = str(report.total)
        cells[2].text = str(report.passed)
        cells[3].text = str(report.failed)


def _metadata(document, run: RunReport, scope: str) -> None:
    document.add_paragraph(f"Scope: {scope}")
    counts = canonical_counts(run)
    document.add_paragraph(f"Run started: {_utc_timestamp(run.started_at)}")
    document.add_paragraph(f"Run finished: {_utc_timestamp(run.finished_at)}")
    duration = _duration_seconds(run.started_at, run.finished_at)
    document.add_paragraph(f"Run duration: {duration:.2f}s" if duration is not None else "Run duration: NOT CAPTURED")
    document.add_paragraph(
        f"Totals: {counts['executed_tests']} tests ran | {counts['passed']} passed | {counts['failed']} failed | "
        f"{counts['skipped']} skipped | {counts['blocked_flows']} blocked journeys"
    )


def _warnings_section(document, warnings: list[str]) -> None:
    document.add_heading("Validation warnings", 1)
    if not warnings:
        document.add_paragraph("None. Every test has evidence and at least one assertion.")
        return
    document.add_paragraph(
        "These items mean the numbers above cannot be fully trusted from the "
        "document alone and need a manual look:"
    )
    for warning in warnings:
        para = document.add_paragraph(warning, style="List Bullet")
        para.runs[0].font.color.rgb = RGBColor(0xB3, 0x26, 0x1A)


def _executive_summary_section(document, run: RunReport, report_data: dict[str, Any] | None = None, *, lean: bool = False) -> None:
    document.add_heading("Executive Summary", 1)
    data = report_data or _report_data(run, Path.cwd())
    counts = data["counts"]
    tax = data["taxonomy"]
    recommendation = data["recommendation"]
    status = "FAIL" if recommendation == "NO-GO" else ("PASS WITH ISSUES" if recommendation == "CONDITIONAL GO" else "PASS")
    document.add_paragraph(policy.defect_headline(tax))
    document.add_paragraph("Objective: check the discovered pages and the user journeys generated from them, then provide a release recommendation.")
    document.add_paragraph(
        f'Release decision: {recommendation}, derived from the Defects table. {counts["passed"]} of {counts["executed_tests"]} tests passed '
        f'({counts["pass_rate_percent"]}%); {counts["failed"]} failed, {counts["blocked_flows"]} journeys had no result, and {counts["skipped"]} were skipped. '
        f'Journeys passed: {counts["journeys_passed"]} of {counts["journeys_total"]} ({counts["journeys_passed_percent"]}%). '
        + policy.taxonomy_sentence(tax))
    reqs = data["requirements"]
    document.add_paragraph(
        f'Requirements coverage: {reqs["not_backed"]} of {reqs["total"]} requirements ({reqs["percent"]}%) are not backed by a passing result '
        f'(section 6), next to a {counts["pass_rate_percent"]}% pass rate among the tests that ran.' if reqs["total"] else
        "Requirements coverage: no written requirements were supplied, so it cannot be computed; the pass rate covers only the tests that ran.")
    table = _grid(document, ("Overall status", "Tests run", "Passed", "Failed", "Skipped", "Journeys with no result", "Recommendation"))
    values = (status, str(counts["executed_tests"]), str(counts["passed"]), str(counts["failed"]),
              str(counts["skipped"]), str(counts["blocked_flows"]), recommendation)
    for cell, value in zip(table.add_row().cells, values):
        cell.text = value
    document.add_heading("Conditions and top risks", 2)
    if data["conditions"]:
        for condition in data["conditions"]:
            document.add_paragraph(condition, style="List Bullet")
    else:
        document.add_paragraph("No release conditions were recorded.")


_EDGE_WORDS = re.compile(r"wrong|invalid|incorrect|empty|blank|error|unknown|missing", re.I)


def _test_data_paragraphs(document, run: RunReport) -> None:
    """What data the tests used, stated from the run itself (nothing here is a claim the run cannot back up)."""
    accounts = len(run.auth_lines)
    document.add_paragraph(
        "Test data: " + (f"{accounts} test account(s) were used to sign in (details are kept out of the report). " if accounts
                         else "no login was used, so only public pages were exercised. ")
        + "Form values are stored per step in each flow, so a run repeats the same inputs; a step with no stored value "
          "picks the first real option in the list.")
    edge = [f for f in run.flow_reports if _EDGE_WORDS.search(f.goal or "")]
    document.add_paragraph(
        f"Edge cases: {len(edge)} flow(s) are worded as invalid, empty or error inputs (matched by their wording). "
        + ("Beyond those, edge cases such as empty search results or invalid values are not tried unless someone writes them as a plain-sentence requirement."
           if edge else "Empty search results, invalid values and similar cases are not tried unless someone writes them as a plain-sentence requirement."))


def _scope_section(document, run: RunReport) -> None:
    document.add_heading("Test Scope", 1)
    document.add_paragraph(f"In scope: {len(run.url_reports)} discovered URL(s), page-level checks, and generated user flows.")
    document.add_paragraph("Out of scope: performance benchmarking, penetration testing, full browser/device compatibility, and business-rule correctness unless explicitly represented by a verified flow.")
    _test_data_paragraphs(document, run)
    document.add_paragraph("Test types performed: functional smoke testing, navigation, flow verification, evidence capture, and coverage analysis.")
    info = run.browser_info or {}
    browser, device = (info.get("browser") or "chromium"), info.get("device") or ""
    ran = f"{browser.capitalize()}{' emulating ' + device if device else ' on a desktop-size window'}"
    document.add_paragraph(
        f"Device and browser coverage: this run used {ran}. Other browsers and viewports were not run in it and remain release risks for "
        "responsive or browser-specific behaviour. Re-run with --device \"iPhone 14\" for a phone-size check and --browser firefox (or webkit) for another engine."
        if not device and browser == "chromium" else
        f"Device and browser coverage: this run used {ran}. Desktop Chromium and any other browser or viewport are only covered by their own runs.")


def _environment_section(document, run: RunReport, report_data: dict[str, Any] | None = None) -> None:
    document.add_heading("Test Environment", 1)
    env = environment_block(run)
    build = run.app_build or {"id": "NOT CAPTURED", "source": "not looked up"}
    table = _grid(document, ("Item", "Value"))
    values = [("URL scope", run.base_url or "All discovered URLs"),
              ("Application build (site under test)", f'{build["id"]} ({build["source"]})'),
              ("Run ID", run.run_id or "NOT CAPTURED"),
              ("CI / job link", policy.ci_link()),
              ("Browser", f'{env.get("Browser", "Chromium")} {env.get("Browser version", "NOT CAPTURED")}'),
              ("Viewport", policy.viewport_text(run.browser_info)),
              ("Retry policy", policy.RETRY_POLICY),
              ("Operating system", env.get("OS", "unknown")), ("Python", env.get("Python", "unknown")),
              ("Playwright", env.get("Playwright", "NOT CAPTURED")), ("pytest", env.get("pytest", "NOT CAPTURED")),
              ("Pipeline version", f'{env.get("Pipeline version", "NOT CAPTURED")} ({env.get("Pipeline git SHA", "NOT CAPTURED")})'),
              ("Desktop/device", env.get("Device", "Desktop (no device emulation)")),
              ("Test data/accounts", "Configured local test account; secrets excluded from the report")]
    for key, value in values:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = key, value
    delta = (report_data or {}).get("delta") or {}
    if delta.get("baseline"):
        changes = delta.get("environment_changes") or []
        for change in changes:
            document.add_paragraph(f"Tool change since the previous run: {change}")
        if not changes:
            document.add_paragraph("The browser and Playwright versions are the same as in the previous run.")


def _action_text(record: dict[str, Any]) -> str:
    kind = record["category"]
    if kind == "defect":
        return f'Fix and retest: {record["title"]}'
    if kind == "unverified":
        return f'Manual pass, or add an outcome check so a run can verify it: {record["title"]}'
    if kind == "limitation":
        return record.get("mitigation") or f'Unblock automation or accept in writing: {record["title"]}'
    if kind == "test_defect":
        return f'Correct the test: {record["title"]}'
    return f'Repair the harness so a real result exists: {record["title"]}'


def _conclusion_section(document, run: RunReport, report_data: dict[str, Any] | None = None, *, lean: bool = False) -> None:
    document.add_heading("Conclusions & Recommendations", 1)
    data = report_data or _report_data(run, Path.cwd())
    recommendation = data["recommendation"]
    if recommendation == "GO":
        document.add_paragraph("Final verdict: GO. The tested scope is suitable for release. Review the documented out-of-scope items before signing off.")
    elif recommendation == "CONDITIONAL GO":
        document.add_paragraph("Final verdict: CONDITIONAL GO. No application defect blocks the release, but it is not unconditional until the exit criteria below are met.")
    else:
        document.add_paragraph("Final verdict: NO-GO. Do not release based on this run.")
    document.add_paragraph(policy.taxonomy_sentence(data["taxonomy"]))
    document.add_paragraph("Exit criteria: the conditions that move the current verdict to GO.")
    for criterion in data["exit_criteria"]:
        document.add_paragraph(criterion, style="List Bullet")
    records = data["defects"]
    if records:
        document.add_paragraph("Actions, each with a named owner and a due date (ISO):")
        table = _grid(document, ("Item", "Action", "Owner", "Due"))
        _set_table_widths(table, (0.8, 3.4, 1.4, 0.9))
        for d in records:
            cells = table.add_row().cells
            cells[0].text = _display_id(d["id"])
            cells[1].text = _action_text(d)
            cells[2].text = d["owner"]
            cells[3].text = d["due"]


def _verify_document(document, data: dict[str, Any]) -> None:
    """Fail rather than emit a document whose IDs or counts do not reconcile.

    Walks the finished document in order: every ID cited in section 1, 8 or 9 must have a row in section 5 or 6; the
    rows of section 5 must match the per-type counts; sections 1, 8 and 9 must carry the same defect figures."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    section = None
    cited: dict[str, set[str]] = {}
    defined: set[str] = set()
    texts: dict[str, list[str]] = {}
    rows_by_prefix: dict[str, int] = {}
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            if paragraph.style is not None and paragraph.style.name == "Heading 1":
                section = paragraph.text.strip()
                continue
            text = paragraph.text
            first_cells = []
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            text = " ".join(cell.text for row in table.rows for cell in row.cells)
            first_cells = [row.cells[0].text.strip() for row in table.rows[1:]]
        else:
            continue
        if section is None:
            continue
        texts.setdefault(section, []).append(text)
        ids = {m.group(0) for m in policy.ID_RE.finditer(text)}
        if section in {"Defect Report (Bugs Found)", "Test Coverage / Requirements Traceability"}:
            for cell_text in first_cells:
                if policy.ID_RE.fullmatch(cell_text):
                    defined.add(cell_text)
                    prefix = policy.plain_id(cell_text).split("-")[0]
                    if (prefix == "COV") == (section == "Test Coverage / Requirements Traceability"):
                        rows_by_prefix[prefix] = rows_by_prefix.get(prefix, 0) + 1
        elif section in {"Executive Summary", "Risks & Issues", "Conclusions & Recommendations"}:
            cited.setdefault(section, set()).update(ids)
    policy.check_ids(cited, defined)
    for category, n in data["taxonomy"].items():
        if rows_by_prefix.get(policy.PREFIX[category], 0) != n:
            raise policy.ReportConsistencyError(
                f"section 5 lists {rows_by_prefix.get(policy.PREFIX[category], 0)} {policy.CATEGORY_LABEL[category]} row(s) but the report counts {n}")
    if rows_by_prefix.get("COV", 0) != len(data["coverage"]["gaps"]):
        raise policy.ReportConsistencyError("section 6 COV rows do not match the coverage gaps counted")
    sentence = policy.taxonomy_sentence(data["taxonomy"])
    for name in ("Executive Summary", "Risks & Issues", "Conclusions & Recommendations", "Defect Report (Bugs Found)"):
        if sentence not in " ".join(texts.get(name, [])):
            raise policy.ReportConsistencyError(f"section '{name}' does not carry the same defect counts as the Defect Report")
    if not " ".join(texts.get("Executive Summary", [])).lstrip().startswith(policy.defect_headline(data["taxonomy"])):
        raise policy.ReportConsistencyError("the executive summary must lead with the application defect count")
    if data["delta"].get("baseline") and "Test Execution Summary" in texts and not any(
            "Compared with" in t for t in texts["Test Execution Summary"]):
        raise policy.ReportConsistencyError("a previous run exists but the run-over-run comparison is missing from section 4")


def _validate_docx_export(destination: Path) -> None:
    """Reject the known catastrophic export-corruption shapes before delivery.

    This is deliberately a small integrity gate, not a substitute for rendering
    visual QA.  The old exporter could emit pages containing repeated ``1`` or
    year values and, in a separate failure mode, explode the table cell count.
    Catching those signatures in the OOXML makes the regression test deterministic
    and prevents a broken artifact from being presented as a finished report.
    """
    with zipfile.ZipFile(destination) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", xml)
    if re.search(r"(?:\b1\b[\s,;]*){30,}", text):
        raise ValueError(f"DOCX export contains a repeated-value corruption sequence: {destination}")
    if re.search(r"(?:(?:19|20)\d{2}[\s,;]*){20,}", text):
        raise ValueError(f"DOCX export contains a repeated-year corruption sequence: {destination}")
    if xml.count("<w:tc") > 20000:
        raise ValueError(f"DOCX export contains an implausibly large table-cell count: {destination}")


def _save_document(document, destination: Path, run: RunReport) -> Path:
    """Save the document. If the file is locked (typically open in Word, which holds a ~$ lock file), write
    a '-new' copy next to it and say so, instead of letting one locked file abort the whole report."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        document.save(destination)
        if destination.is_file():
            _validate_docx_export(destination)
        return destination
    except PermissionError:
        alternative = destination.with_name(destination.stem + "-new" + destination.suffix)
        document.save(alternative)
        if alternative.is_file():
            _validate_docx_export(alternative)
        run.notes.append(f"{destination.name} is open in another program and could not be overwritten; "
                         f"the new report was written to {alternative.name}")
        return alternative


def build_url_docx(run: RunReport, report: UrlReport, destination: Path) -> None:
    document = Document()
    document.add_heading("Website Test Evidence Report", 0)
    document.add_heading(report.url, 1)
    _metadata(document, run, scope=f"single URL ({report.url})")
    _table_of_contents(document)
    document.add_heading("Test Execution Summary", 1)
    _summary_table(document, [report])
    flows = run.flows_at(report.url)
    page_cov = next((p for p in (run.coverage.pages if run.coverage else []) if p.url == report.url), None)
    if page_cov and page_cov.total:
        document.add_paragraph(f"Flow coverage of this page: {page_cov.touched} of {page_cov.total} content controls are acted on "
                               f"by a tested flow ({'visited' if page_cov.visited else 'not visited'} by any).")
    _warnings_section(document, report.warnings + [w for f in flows for w in f.warnings])
    if flows:
        document.add_page_break()
        _flows_section(document, flows, embed=True, link_base=None)
    for outcome in report.outcomes:
        document.add_page_break()
        _render_outcome(document, outcome)
    _bookmark_headings(document)
    _save_document(document, destination, run)


APPENDIX_FILE = "full-report-appendix.docx"


def build_combined_docx(run: RunReport, destination: Path, report_data: dict[str, Any] | None = None,
                        appendix: str = "inline") -> None:
    """The decision report. appendix="inline" keeps the evidence appendix in the same file (the long form);
    "separate" writes it to full-report-appendix.docx and leaves this file short enough to read in one sitting."""
    lean = appendix == "separate"
    report_data = report_data or _report_data(run, destination.parent)
    document = Document()
    document.add_heading("Website Test Report Full Run", 0)
    _metadata(document, run, scope="all URLs")
    _table_of_contents(document, lean=lean)
    _executive_summary_section(document, run, report_data, lean=lean)
    _scope_section(document, run)
    _environment_section(document, run, report_data)
    _test_summary_section(document, run, report_data)
    chart = _write_execution_chart(destination.parent, report_data["counts"])
    if chart:
        document.add_paragraph("Result breakdown")
        document.add_picture(str(chart), width=Inches(5.7))
    document.add_page_break()
    document.add_heading("Defect Report (Bugs Found)", 1)
    _findings_section(document, run, report_data, compact=lean)
    link_base = destination.parent
    if lean and run.untested_flows:
        document.add_paragraph(f"{len(run.untested_flows)} flow(s) have no test result in this run; each is an Unverified, Limitation or Defect entry in section 5, and the table with their history is in the appendix.")
    else:
        _untested_flows_section(document, run.untested_flows, detail=not lean)
    _coverage_section(document, run, report_data)
    _traceability_section(document, run, report_data, gaps_only=lean)
    document.add_heading("Test Logs & Evidence", 1)
    _evidence_store_section(document, run)
    has_evidence = bool(run.tested_flows or any(r.outcomes for r in run.url_reports))
    if not lean:
        _appendix_section(document, run, link_base=link_base, heading=False)
    elif has_evidence:
        para = document.add_paragraph(
            "The searchable test index, evidence index, per-journey detail (steps, history, screenshots) and raw failure output are in ")
        _add_hyperlink(para, APPENDIX_FILE, APPENDIX_FILE)
        para.add_run(" in the same folder. The machine-readable source is report-data.json.")
    else:
        document.add_paragraph("No test evidence was captured in this run.")
    _risks_section(document, run, report_data, lean=lean)
    _conclusion_section(document, run, report_data, lean=lean)
    _verify_document(document, report_data)
    _bookmark_headings(document)
    _save_document(document, destination, run)
    if lean and has_evidence:
        _build_appendix_docx(run, destination.with_name(APPENDIX_FILE), link_base, report_data)


def _build_appendix_docx(run: RunReport, destination: Path, link_base: Path, report_data: dict[str, Any]) -> None:
    """The evidence that would otherwise make the decision report hundreds of pages long."""
    document = Document()
    document.add_heading("Website Test Report - Appendix", 0)
    _metadata(document, run, scope="all URLs")
    if report_data["defects"]:
        document.add_heading("Defect details", 1)
        _defect_detail_tables(document, report_data["defects"])
    if run.tested_flows:
        document.add_heading("All tested flows", 1)
        _flows_table(document, run.tested_flows)
    _untested_flows_section(document, run.untested_flows)
    if run.requirements:
        _traceability_section(document, run, report_data)
    _appendix_section(document, run, link_base=link_base)
    _bookmark_headings(document)
    _save_document(document, destination, run)


def _evidence_store_section(document, run: RunReport) -> None:
    """Where the evidence lives, for how long, and which CI run produced it."""
    base = os.environ.get("ARTIFACT_BASE_URL", "").strip()
    retention = os.environ.get("ARTIFACT_RETENTION_DAYS", "").strip()
    table = _grid(document, ("Evidence store", "Value"))
    rows = (("Artifact base URL", base or "NOT CAPTURED: ARTIFACT_BASE_URL is not set, so every evidence link in this report is relative to the report folder"),
            ("Retention window", f"{retention} days" if retention else "NOT CAPTURED: ARTIFACT_RETENTION_DAYS is not set; the files are kept until the run folder is deleted"),
            ("CI run link", policy.ci_link()),
            ("Run ID", run.run_id or "NOT CAPTURED"))
    for key, value in rows:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = key, value


def _appendix_section(document, run: RunReport, *, link_base: Path | None, heading: bool = True) -> None:
    """Indexed raw evidence, deduplicated screenshots, and machine-readable source data."""
    if not run.tested_flows and not any(r.outcomes for r in run.url_reports):
        return
    document.add_page_break()
    if heading:
        document.add_heading("Test Logs & Evidence", 1)
    document.add_paragraph(
        "This appendix contains the searchable test index, screenshots, attachments, assertions, and failure details. "
        "Repeated screenshots are stored once and referenced by evidence ID.")
    para = document.add_paragraph("Machine-readable source: ")
    _add_hyperlink(para, "report-data.json", "report-data.json")

    log_lines = _run_log_lines(run)
    if log_lines:
        document.add_heading("Run log", 2)
        for line in log_lines:
            document.add_paragraph(line, style="List Bullet")

    document.add_heading("Appendix index", 2)
    index = _grid(document, ("Entry", "Contents"))
    for entry, contents in (("Complete test index", "Every executed test with a unique ID and expected/observed result."),
                            ("Coverage data", "Page URLs, denominator rules, and untouched controls."),
                            ("Evidence", "Deduplicated screenshots, traces, and videos by test and step."),
                            ("Report data", "Validated JSON source used to render this document.")):
        cells = index.add_row().cells
        cells[0].text, cells[1].text = entry, contents

    _test_index_section(document, run)
    if run.coverage and run.coverage.pages:
        document.add_heading("Coverage data", 2)
        table = _grid(document, ("Page", "URL", "Visited", "Controls", "Untouched controls"))
        for page in run.coverage.pages:
            cells = table.add_row().cells
            values = (page.path, page.url, "Yes" if page.visited else "No", f"{page.touched}/{page.total}", "; ".join(page.untouched) or "None")
            for cell, value in zip(cells, values):
                cell.text = value

    document.add_heading("Evidence index", 2)
    evidence_index = _grid(document, ("Evidence ID", "Test", "Type", "File"))
    seen_files: set[str] = set()
    evidence_number = 1
    for _, key, outcome, flow in _all_outcomes(run):
        test_label = flow.title if flow else (outcome.title or key)
        for path in outcome.evidence + outcome.attachments:
            if path in seen_files:
                continue
            seen_files.add(path)
            cells = evidence_index.add_row().cells
            evidence_id = f"E-{evidence_number:04d}"
            evidence_number += 1
            values = (evidence_id, test_label, "Screenshot" if path in outcome.evidence else "Trace/video")
            for cell, value in zip(cells[:3], values):
                cell.text = value
            file_cell = cells[3]
            file_cell.text = ""
            _add_hyperlink(file_cell.paragraphs[0], _relative_artifact(path, link_base), Path(path).name)

    if run.tested_flows:
        document.add_heading("Flow evidence", 2)
        for flow in run.tested_flows:
            _render_flow(document, flow, embed=True, link_base=link_base)
    pages_with_tests = [r for r in run.url_reports if r.outcomes]
    if pages_with_tests:
        document.add_heading("Page evidence", 2)
        for report in pages_with_tests:
            document.add_heading(report.url, 2)
            for outcome in report.outcomes:
                _render_outcome(document, outcome, embed=True, link_base=link_base)


def name_for(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:100] or "url"


def create_report(
    artifacts_dir: Path,
    tests_dir: Path,
    out_dir: Path,
    *,
    model: str = "",
    combined: bool = False,
    flows_file: Path | None = None,
    ratings_file: Path | None = None,
    appendix: str = "inline",
) -> RunReport:
    run = load_run(artifacts_dir, tests_dir, model=model, flows_file=flows_file, ratings_file=ratings_file)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_data = _report_data(run, out_dir)
    validate_report_data(report_data)
    write_report_data(out_dir / "report-data.json", report_data)
    # Secondary (per-page) reports go in their own subfolder so out_dir's top level stays just the
    # deliverables that matter for a release decision: full-report.docx, its appendix, and report-data.json.
    pages_dir = out_dir / "pages"
    if run.url_reports:
        pages_dir.mkdir(parents=True, exist_ok=True)
    for report in run.url_reports:
        build_url_docx(run, report, pages_dir / f"{name_for(report.url)}.docx")
    if combined:
        build_combined_docx(run, out_dir / "full-report.docx", report_data, appendix=appendix)
    # Only after every document was built: a failed build must not become the baseline of the next comparison.
    policy.save_history(out_dir / policy.HISTORY_FILE, policy.load_history(out_dir / policy.HISTORY_FILE), report_data["snapshot"])
    return run
