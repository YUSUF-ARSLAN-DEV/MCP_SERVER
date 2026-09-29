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
        "Defect Report (Bugs Found)", "User flows", "Test Coverage / Requirements Traceability",
        "Test Logs & Evidence", "Risks & Issues", "Conclusions & Recommendations", "Appendix: full evidence",
    )
    for entry in entries:
        if lean and entry in {"Test Logs & Evidence"}:
            continue                                   # the lean report has no such section; its pointer is under the Appendix heading
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


def _findings_section(document, run: RunReport) -> None:
    """The headline of the report: what to trust, what to fix, and what to look at first - ahead of the
    raw counts and the page-by-page detail. Environment, then which recent results are unsettled (a
    tested flow whose own history flips between pass and fail is not a fact to rely on yet), then every
    failure this run classified by kind (a wrong assertion vs. a live, content-dependent result vs.
    genuinely unclear) with a one-line reproduction and what to do about it."""
    document.add_heading("Findings", 2)
    env = environment_block(run)
    document.add_paragraph(" | ".join(f"{k}: {v}" for k, v in env.items()))
    if run.auth_lines:
        para = document.add_paragraph()
        para.add_run("Authentication: ").bold = True
        para.add_run(" ".join(run.auth_lines))
    if run.coverage and run.coverage.pages:
        cov = run.coverage
        para = document.add_paragraph()
        para.add_run(f"Coverage: {cov.pages_visited}/{cov.pages_total} pages visited by a tested flow "
                     f"({cov.percent_pages}%), {cov.controls_touched}/{cov.controls_total} content controls acted "
                     f"on ({cov.percent_controls}%). Detail below in “Flow coverage”.").bold = True

    if run.flapping:
        document.add_paragraph(f"Risk: {len(run.flapping)} flow(s) had inconsistent recent results and should be retested before release.")

    if run.untested_auth:
        para = document.add_paragraph()
        para.add_run(f"Not tested - {len(run.untested_auth)} login or sign-up form(s) found. "
                     "Anything behind them was not exercised:").bold = True
        table = _grid(document, ("Page", "Form", "Fields it asks for"))
        for wall in run.untested_auth:
            cells = table.add_row().cells
            cells[0].text = wall["url"]
            cells[1].text = "sign-up" if wall["kind"] == "signup" else "login"
            cells[2].text = ", ".join(wall["fields"])
        document.add_paragraph()

    if run.human_input_pages:
        para = document.add_paragraph()
        para.add_run(f"Needs a human - {len(run.human_input_pages)} page(s) have a step nothing but a person can "
                     "complete. Everything up to that step was still tested; the step itself was not:").bold = True
        table = _grid(document, ("Page", "Blocked on", "Why"))
        for page in run.human_input_pages:
            cells = table.add_row().cells
            cells[0].text = page["url"]
            cells[1].text = page["reason"]
            cells[2].text = page["detail"]
        document.add_paragraph()

    if not run.findings:
        para = document.add_paragraph("No failures to triage this run.")
        para.runs[0].italic = True
        return
    document.add_paragraph(f"{len(run.findings)} finding(s), classified by severity, failure class, and scope. "
                           "The severity describes release impact; the failure class describes what kind of issue "
                           "was observed, and the scope identifies whether it affects a page or user journey. "
                           "test_defect means the assertion itself is provably wrong "
                           "against what was actually observed; flaky_data means the run completed but the site "
                           "did not return the content the sentence promised (may be content-dependent, not a "
                           "defect); capture_corruption means some captured text could not be decoded cleanly "
                           "(a real data-integrity bug, in the pipeline or the source page - not a guess); "
                           "localization_mismatch means a page's declared writing direction does not match "
                           "what its own captured text actually is, or a flow silently changed script "
                           "partway through; auth_failure means the login never succeeded this run - the details "
                           "in .env may be stale; unclear means it could not be classified automatically - read "
                           "the trace.")
    table = _grid(document, ("ID", "Severity", "Title", "Steps to reproduce", "Expected", "Actual", "Status", "Owner"))
    for number, finding in enumerate(run.findings, 1):
        cells = table.add_row().cells
        cells[0].text = f"BUG-{number:03d}"
        cells[1].text = finding.severity
        # links to the page's or flow's own heading in the evidence Appendix, where the full run (screenshots,
        # every assertion, review history) for this exact title/url lives - a reader is one click from proof.
        _add_internal_hyperlink(cells[2].paragraphs[0], _shorten(finding.summary, 120),
                               _heading_anchor(finding.url if finding.scope == "page" else finding.test))
        cells[3].text = f'Open {finding.url} and run "{finding.test}".'
        cells[4].text = "The flow/page check completes as specified"
        cells[5].text = _shorten(plain_error(finding.repro) if finding.repro else finding.summary, 140)
        cells[6].text = "Open"
        cells[7].text = "Unassigned"


def _shorten(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _display_id(value: str) -> str:
    return value.replace("-", "‑") if value.startswith("BLOCK-") else value


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


def _defect_records(run: RunReport, report_dir: Path | None = None) -> list[dict[str, Any]]:
    """Build one complete, auditable defect/blocker object per failure and blocked flow."""
    records: list[dict[str, Any]] = []
    direct_keys: set[tuple[str, str, str]] = set()
    number = 1
    for scope, key, outcome, flow in _all_outcomes(run):
        if outcome.status not in {"failed", "error"}:
            continue
        finding = _finding_for(run, flow.title if flow else outcome.title, outcome.url, scope)
        test_id = key
        title = _plain_flow_title(flow.title) if flow else outcome.title.replace("_", " ")
        expected = (flow.expected if flow and flow.expected else
                    (_plain_assertion(outcome.assertions[0]) if outcome.assertions else "The check completes as specified."))
        actual = plain_error(outcome.error) if outcome.error else "The test failed without a captured reason."
        evidence = (outcome.evidence or outcome.screenshots or [None])[0]
        trace = next((a for a in outcome.attachments if a.lower().endswith(".zip")), None)
        record = {
            "id": f"BUG-{number:03d}",
            "severity": _severity_label(getattr(finding, "severity", ""), scope),
            "title": _shorten(title, 160),
            "description": actual if not finding else f"{actual} Failure class: {finding.kind}.",
            "steps_to_reproduce": [f"Open {outcome.url}.", f"Run test {test_id}."],
            "expected": expected or "NOT CAPTURED",
            "actual": actual or "NOT CAPTURED",
            "screenshot_url": _relative_artifact(evidence, report_dir),
            "trace_url": _relative_artifact(trace, report_dir) if trace else "NOT CAPTURED",
            "status": "Open",
            "owner": "Unassigned",
            "linked_flow_id": flow.flow_id if flow else "NOT CAPTURED",
            "failure_class": getattr(finding, "kind", "unclear") if finding else "unclear",
            "scope": scope,
            "business_impact": (f'Visitors doing "{_shorten(title, 80)}" from {_page_of(outcome.url)} may not be able to finish it; this run failed to confirm the journey works.'
                                if flow else f'The check "{_shorten(title, 80)}" on {_page_of(outcome.url)} failed, so that part of the page is not confirmed to work.'),
            "blocking": False,
        }
        records.append(record)
        direct_keys.add((scope, outcome.title, outcome.url))
        if flow:
            direct_keys.add((scope, flow.title, outcome.url))
        number += 1

    # Findings produced from authentication history or capture/localization
    # checks do not always have a pytest outcome of their own.  They still need
    # the same complete defect shape as an ordinary failed test.
    for finding in run.findings:
        if (finding.scope, finding.test, finding.url) in direct_keys:
            continue
        records.append({
            "id": f"BUG-{number:03d}",
            "severity": _severity_label(finding.severity, finding.scope),
            "title": _shorten(finding.summary, 160),
            "description": finding.summary or "A recorded validation finding needs review.",
            "steps_to_reproduce": [finding.repro or f"Open {finding.url or 'the affected page'}."] ,
            "expected": "The recorded check should complete without this finding.",
            "actual": finding.summary or "NOT CAPTURED",
            "screenshot_url": "NOT CAPTURED",
            "status": "Open",
            "owner": "Unassigned",
            "linked_flow_id": finding.test if finding.scope == "flow" else "NOT CAPTURED",
            "failure_class": finding.kind or "unclear",
            "scope": finding.scope,
            "business_impact": f'A {finding.kind or "validation"} finding on {_page_of(finding.url)}: {_shorten(finding.summary or "see the finding", 100)}. It needs review before the release decision can be trusted.',
        })
        number += 1

    for flow in run.untested_flows:
        failed_check = flow.verify_failed         # its last real check failed: a failure, not merely "could not run"
        record = {
            "id": f"{'BUG' if failed_check else 'BLOCK'}-{number:03d}",
            "severity": "High",
            "title": _shorten(_plain_flow_title(flow.title), 160),
            "description": f"This user journey has no test result in this run: {flow.not_run_reason}.",
            "steps_to_reproduce": [f"Open {flow.start_url}.", f"Attempt journey {flow.flow_id}."],
            "expected": flow.expected or "The journey should complete and produce a verified result.",
            "actual": flow.observed or "The journey was not completed.",
            "screenshot_url": _relative_artifact(flow.outcome.evidence[0] if flow.outcome and flow.outcome.evidence else None, report_dir),
            "status": "Open",
            "owner": "Unassigned",
            "linked_flow_id": flow.flow_id,
            "failure_class": "failed_last_check" if failed_check else "blocked_flow",
            "scope": "flow",
            "business_impact": f'Visitors cannot be shown to complete "{_shorten(_plain_flow_title(flow.title), 80)}" from {_page_of(flow.start_url)}; until it has a passing test, releasing carries that risk.',
            "blocking": not failed_check,
        }
        records.append(record)
        number += 1

    represented_pages = {d.get("url") for d in records}
    for page in run.human_input_pages:
        if page["url"] in represented_pages:
            continue
        records.append({
            "id": f"BLOCK-{number:03d}",
            "severity": "High",
            "title": f'A person is needed to complete the form at {_page_of(page["url"])}',
            "description": page.get("detail", "A human-only step was detected."),
            "steps_to_reproduce": [f'Open {page["url"]}.', f'Complete the {page["reason"]} step manually.'],
            "expected": "The form should be completed and submitted.",
            "actual": f'The run stopped at {page["reason"]}; the step was not automated.',
            "screenshot_url": "NOT CAPTURED",
            "status": "Open",
            "owner": "QA lead with the site owner (bypass or manual pass)",
            "linked_flow_id": "NOT CAPTURED",
            "failure_class": "human_input_required",
            "mitigation": (f'Not an application defect: an automated test cannot pass a {page["reason"]}. Ask the site owner for a staging '
                           "bypass (test key or disabled check) so the form can be automated, or have a person complete it in each release "
                           "test pass and record the result here."),
            "scope": "page",
            "business_impact": f'The form at {_page_of(page["url"])} needs a {page["reason"]} step that no automated test can pass, so submitting it is unverified.',
            "blocking": True,
        })
        number += 1

    for wall in run.untested_auth:
        records.append({
            "id": f"BLOCK-{number:03d}",
            "severity": "High",
            "title": "Authentication required before protected pages can be tested",
            "description": "A login or sign-up form was found, so content behind it was not exercised.",
            "steps_to_reproduce": [f'Open {wall["url"]}.', "Sign in or create the account required by the form."],
            "expected": "The authenticated area should be available for testing.",
            "actual": "The run could not proceed beyond the authentication form.",
            "screenshot_url": "NOT CAPTURED",
            "status": "Open",
            "owner": "Unassigned",
            "linked_flow_id": "NOT CAPTURED",
            "failure_class": "authentication_required",
            "scope": "page",
            "business_impact": f'Pages behind the sign-in at {_page_of(wall["url"])} were not tested, so anything a signed-in visitor does there is unverified.',
            "blocking": True,
        })
        number += 1

    for record in records:
        record["priority"] = _priority(record)
    return records


def _page_of(url: str | None) -> str:
    """The path part of a URL for prose ("/en/subscribe"); the whole value when it has none."""
    return urlsplit(url or "").path or (url or "the affected page")


def _priority(record: dict[str, Any]) -> str:
    """How urgently to fix it - separate from severity (how bad it is). A blocked or journey-level problem stops
    a real user path, so it is urgent even when the underlying defect looks small."""
    if record.get("blocking"):
        return "P1 - before release"
    high = record.get("severity") in {"Critical", "High"}
    if record.get("scope") == "flow":
        return "P1 - before release" if high else "P2 - next fix cycle"
    if high:
        return "P1 - before release"
    return "P2 - next fix cycle" if record.get("severity") == "Medium" else "P3 - when convenient"


def _coverage_gaps(run: RunReport) -> list[dict[str, str]]:
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
            })
            n += 1
    return gaps


def _release_decision(run: RunReport, defects: list[dict[str, Any]], gaps: list[dict[str, str]]) -> tuple[str, list[str]]:
    if run.failed or any(d["status"] == "Open" for d in defects):
        return "NO-GO", [f"Resolve {d['id']} ({d['title']}) and rerun the affected check." for d in defects if d["status"] == "Open"]
    conditions = [f"Complete {d['id']} ({d['title']}) or formally accept the remaining test risk." for d in defects if d.get("blocking")]
    conditions.extend(f"Review {gap['id']} ({gap['page']}) before release." for gap in gaps[:5])
    if conditions:
        return "CONDITIONAL GO", conditions
    return "GO", []


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


def _report_data(run: RunReport, report_dir: Path) -> dict[str, Any]:
    counts = canonical_counts(run)
    defects = _defect_records(run, report_dir)
    gaps = _coverage_gaps(run)
    recommendation, conditions = _release_decision(run, defects, gaps)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema_version": "1.0",
        "generated_at": now,
        "site": run.base_url or "NOT CAPTURED",
        "run_started_at": _utc_timestamp(run.started_at),
        "run_finished_at": _utc_timestamp(run.finished_at),
        "run_duration_seconds": _duration_seconds(run.started_at, run.finished_at),
        "counts": counts,
        "recommendation": recommendation,
        "conditions": conditions,
        "defects": defects,
        "coverage": {
            "pages_total": run.coverage.pages_total if run.coverage else 0,
            "pages_visited": run.coverage.pages_visited if run.coverage else 0,
            "controls_total": run.coverage.controls_total if run.coverage else 0,
            "controls_touched": run.coverage.controls_touched if run.coverage else 0,
            "tested_flows": run.coverage.tested_flows if run.coverage else 0,
            "planned_flows": run.coverage.planned_flows if run.coverage else 0,
            "gaps": gaps,
        },
        "environment": environment_block(run),
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


def _compact_defects(document, defects: list[dict[str, Any]]) -> None:
    """One row per defect: what went wrong and what it means. Steps, expected result and trace links are in the appendix."""
    table = _grid(document, ("ID", "What went wrong", "Impact", "Evidence"))
    _set_table_widths(table, (0.8, 2.2, 2.6, 0.8))
    for defect in defects:
        cells = table.add_row().cells
        cells[0].text = _display_id(defect["id"])
        cells[1].text = _shorten(str(defect["actual"]), 130)
        cells[2].text = _shorten(str(defect.get("business_impact", "")), 150)
        cells[3].text = "none" if defect["screenshot_url"] == "NOT CAPTURED" else "yes"
    document.add_paragraph("Steps to reproduce, the expected result, owner and trace link for each defect are in the appendix.")


def _findings_section(document, run: RunReport, report_data: dict[str, Any] | None = None, *, compact: bool = False) -> None:
    """Render complete defects and blockers in a readable, auditable format."""
    document.add_heading("Defects and blockers", 2)
    data = report_data or _report_data(run, Path.cwd())
    defects = data["defects"]
    if not defects:
        document.add_paragraph("No open defects or blocked journeys were recorded in this run. No failures to triage this run.")
        return
    document.add_paragraph(f"{len(defects)} recorded defect or blocker(s), classified by severity, priority, failure class, and scope.")
    document.add_paragraph(
        "BUG means something ran and went wrong, or a journey whose last real check failed (it counts as a failure, even when this "
        "run had no fresh result for it). BLOCK means a journey could not be run or completed for a reason outside the check itself: "
        "no test exists for it, its sentence was edited, a login is required, or a step only a person can do (such as a CAPTCHA).")
    index = _grid(document, ("ID", "Severity", "Priority", "Type", "Status", "Title"))
    _set_table_widths(index, (0.9, 0.65, 0.9, 1.0, 0.65, 2.1))
    for defect in defects:
        cells = index.add_row().cells
        cells[0].text = _display_id(defect["id"])
        cells[1].text = defect["severity"]
        cells[2].text = defect.get("priority", "NOT CAPTURED").split(" - ")[0]
        cells[3].text = f'{defect["scope"]} / {defect.get("failure_class", "NOT CAPTURED")}'
        cells[4].text = defect["status"]
        cells[5].text = _shorten(defect["title"], 90)
    if compact:
        _compact_defects(document, defects)
        return
    _defect_detail_tables(document, defects)


def _defect_detail_tables(document, defects: list[dict[str, Any]]) -> None:
    for defect in defects:
        document.add_heading(f'{defect["id"]} — {defect["title"]}', 3)
        table = _grid(document, ("Field", "Details"))
        fields = (
            ("Severity (how bad)", defect["severity"]),
            ("Priority (how urgent)", defect.get("priority", "NOT CAPTURED")),
            ("Description", defect["description"]),
            ("Steps to reproduce", "\n".join(f"{i}. {s}" for i, s in enumerate(defect["steps_to_reproduce"], 1))),
            ("Expected", defect["expected"]),
            ("Actual", defect["actual"]),
            ("Status", defect["status"]),
            ("Owner", defect["owner"]),
            ("Linked flow", defect["linked_flow_id"]),
            ("Evidence", defect["screenshot_url"]),
            ("Trace", defect.get("trace_url", "NOT CAPTURED")),
        ) + ((("Mitigation", defect["mitigation"]),) if defect.get("mitigation") else ())
        for label, value in fields:
            cells = table.add_row().cells
            cells[0].text = label
            if label == "Trace" and value != "NOT CAPTURED":
                cells[1].text = f"{value}  (open with: python -m playwright show-trace <file>; it holds the page snapshots, console and network log)"
            elif label == "Evidence" and value != "NOT CAPTURED":
                _add_hyperlink(cells[1].paragraphs[0], value, value)
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
    blockers = [d for d in defects if d.get("blocking") or d["severity"] in {"Critical", "High"}]
    document.add_heading("Blockers", 2)
    if lean:
        document.add_paragraph(f"{len(blockers)} release blocker(s) are listed with their impact in the Defect Report above; they are not repeated here.")
    elif blockers:
        for defect in blockers:
            document.add_paragraph(f'{defect["id"]}: {defect["title"]} — {defect["business_impact"]}', style="List Bullet")
    else:
        document.add_paragraph("No release blockers were recorded.")

    document.add_heading("Risks", 2)
    risks = []
    if run.flapping:
        risks.append(f"{len(run.flapping)} journey(s) produced inconsistent recent results and should be retested.")
    if run.failed:
        risks.append("The failed checks reduce confidence in the affected journeys until they pass consistently.")
    risks.append("Mobile viewport coverage was not executed; responsive and mobile release risk remains unverified.")
    for risk in risks:
        document.add_paragraph(risk, style="List Bullet")

    document.add_heading("Coverage notes", 2)
    notes = [_plain_warning(w)[1] for w in run.warnings]
    if run.auth_lines:
        notes.extend(f"Authentication: {line}" for line in run.auth_lines)
    if run.human_input_pages:
        notes.append(f"Needs a human - {len(run.human_input_pages)} form step(s) require manual completion; those steps were not automated.")
    if run.untested_auth:
        notes.append(f"Not tested - {len(run.untested_auth)} login or sign-up form(s) found; content behind them was not exercised.")
        for wall in run.untested_auth:
            notes.append(f'Authentication form fields observed: {", ".join(wall["fields"]) or "NOT CAPTURED"}.')
    notes = list(dict.fromkeys(notes))
    if notes:
        for note in notes:
            document.add_paragraph(note, style="List Bullet")
    else:
        document.add_paragraph("No additional coverage notes were recorded.")


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
        f'{counts["executed_tests"]} tests ran: {counts["passed"]} passed and {counts["failed"]} failed. '
        f'{counts["skipped"]} were skipped and {counts["blocked_flows"]} user journeys were blocked. '
        f'Pass rate among tests that ran: {counts["pass_rate_percent"]}%. That rate ignores journeys with no result, so read it with the '
        f'journey figure: {counts["journeys_passed"]} of {counts["journeys_total"]} user journeys passed '
        f'({counts["journeys_passed_percent"]}%).'
    )
    page_tests = sum(len(u.outcomes) for u in run.url_reports)
    flow_tests = sum(f.outcome is not None for f in run.flow_reports)
    document.add_paragraph(
        f"What is counted: the test totals above are {page_tests} page test(s) plus {flow_tests} flow test(s). Each flow test "
        f"is one user journey, so the {len(run.flow_reports)} journey(s) counted separately are the same flows, not extra tests; "
        "a journey with no flow test has no result in this run.")
    metrics = _grid(document, ("Metric", "Value"))
    for label, value in (("Tests that ran", counts["executed_tests"]), ("Passed", counts["passed"]),
                         ("Failed", counts["failed"]), ("Skipped", counts["skipped"]),
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
    attention = []
    for test_number, (scope, key, outcome, flow) in enumerate(_all_outcomes(run), 1):
        if outcome.status not in {"failed", "error", "skipped"}:
            continue
        title = flow.title if flow else outcome.title.replace("_", " ")
        expected = flow.expected if flow and flow.expected else (_plain_assertion(outcome.assertions[0]) if outcome.assertions else "NOT CAPTURED")
        observed = flow.observed if flow and flow.observed else (plain_skip_reason(outcome.error) if outcome.status == "skipped" else plain_error(outcome.error))
        attention.append((_display_id(f"T-{test_number:03d}"), scope, title, outcome.url, expected, observed or "NOT CAPTURED", outcome.status.upper()))
    if attention:
        document.add_heading("Tests requiring attention", 2)
        table = _grid(document, ("Test ID", "Scope", "Test name", "URL", "Expected", "Observed", "Result"))
        _set_table_widths(table, (0.65, 0.5, 1.25, 1.0, 1.15, 1.25, 0.55))
        for row in attention:
            cells = table.add_row().cells
            for cell, value in zip(cells, row):
                cell.text = value
    else:
        document.add_paragraph("No failed or skipped tests require attention in this run.")


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


def _coverage_section(document, coverage: Coverage | None, *, per_page: bool = True) -> None:
    """How much of the explored site the tested flows touch, and where the untested parts are."""
    if coverage is None or not coverage.pages:
        return
    document.add_heading("Test Coverage / Requirements Traceability", 1)
    document.add_heading("Flow coverage", 2)
    document.add_paragraph(
        f"Pages visited by a tested flow: {coverage.pages_visited} of {coverage.pages_total} ({coverage.percent_pages}%). "
        f"Content controls a tested flow acts on: {coverage.controls_touched} of {coverage.controls_total} "
        f"({coverage.percent_controls}%). Flows: {coverage.tested_flows} tested, {coverage.planned_flows} not yet backed by a "
        "passing run.")
    document.add_paragraph(
        "Method: the page denominator is the number of unique explored URL paths after redirect aliases are removed. "
        "The control denominator is the number of visible, enabled content links, buttons, selects, and fields recorded "
        "by the explorer; repeated header, navigation, and footer controls are excluded. The raw URL and control lists "
        "are included in the machine-readable report-data.json and the coverage appendix.")
    document.add_paragraph("Header, navigation and footer links are left out of the control denominator; they are shared chrome rather than page-specific requirements.")
    document.add_paragraph("Requirements and user stories are represented by the plain-language flow goals. A goal is traceable only when its flow is verified or approved; unverified goals remain coverage gaps.")
    critical = _critical_untouched(coverage)
    document.add_paragraph("The percentages are a rough guide. What matters for a release decision is which specific controls nothing tested:")
    if critical:
        document.add_paragraph(f"Untested controls that look like a page's main action ({len(critical)}):")
        for page_path, control in critical[:25]:
            document.add_paragraph(f"{control} on {page_path}", style="List Bullet")
        if len(critical) > 25:
            document.add_paragraph(f"...and {len(critical) - 25} more; the table below and the coverage appendix list every untouched control.")
    else:
        document.add_paragraph("No untested control looks like a page's main action (submit, search, subscribe and similar).")
    if not per_page:
        document.add_paragraph("The per-page table of untouched controls is in the appendix (Coverage data).")
        return
    table = _grid(document, ("Page", "Visited", "Touched", "Not touched by any flow"))
    for page in coverage.pages:
        cells = table.add_row().cells
        cells[0].text = page.path
        cells[1].text = "yes" if page.visited else "no"
        cells[2].text = f"{page.touched}/{page.total}"
        shown = "; ".join(page.untouched[:5]) + (f"; +{len(page.untouched) - 5} more" if len(page.untouched) > 5 else "")
        cells[3].text = shown
    if coverage.aliases:
        document.add_paragraph("Counted once: " + ", ".join(f"{a} redirects to {b}" for a, b in sorted(coverage.aliases.items())))


def _traceability_section(document, run: RunReport, *, gaps_only: bool = False) -> None:
    """Each written requirement -> the flow built from it -> what this run said. A requirement with no flow, or
    whose flow did not pass, is a coverage gap and is listed as one."""
    if not run.requirements:
        return
    document.add_heading("Requirements traceability", 2)
    by_id = {f.flow_id: f for f in run.flow_reports}
    table = _grid(document, ("Requirement", "Source", "Flow built", "This run"))
    gaps = 0
    for req in run.requirements:
        flow = by_id.get(req.get("flow_id") or "")
        result = flow.run_label if flow else "No flow, so nothing was tested"
        gaps += result != "Passed"
        if gaps_only and result == "Passed":
            continue
        cells = table.add_row().cells
        cells[0].text = f'{req["id"]}: {_shorten(str(req.get("sentence", "")), 110)}'
        cells[1].text = str(req.get("source", "?"))
        cells[2].text = "yes" if flow else f'no ({req.get("status", "new")})'
        cells[3].text = result
    document.add_paragraph(f"{gaps} of {len(run.requirements)} requirement(s) are not backed by a passing result in this run; these are the coverage gaps."
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


def _lean_conditions(conditions: list[str], lean: bool) -> list[str]:
    """The lean report shows the many "Resolve BUG-xxx ... and rerun" lines as one; every other condition is kept.
    The full list stays in report-data.json and the Defect Report."""
    if not lean:
        return conditions
    resolve = [c for c in conditions if c.startswith("Resolve ")]
    rest = [c for c in conditions if not c.startswith("Resolve ")]
    if len(resolve) > 3:
        rest.insert(0, f"Resolve the {len(resolve)} open items in the Defect Report and rerun the affected checks.")
        return rest
    return conditions


def _executive_summary_section(document, run: RunReport, report_data: dict[str, Any] | None = None, *, lean: bool = False) -> None:
    document.add_heading("Executive Summary", 1)
    data = report_data or _report_data(run, Path.cwd())
    counts = data["counts"]
    recommendation = data["recommendation"]
    status = "FAIL" if recommendation == "NO-GO" else ("PASS WITH ISSUES" if recommendation == "CONDITIONAL GO" else "PASS")
    document.add_paragraph("Objective: check the discovered pages and the user journeys generated from them, then provide a release recommendation.")
    document.add_paragraph(
        f'Release decision: {recommendation}. {counts["passed"]} of {counts["executed_tests"]} tests passed; '
        f'{counts["failed"]} failed, {counts["blocked_flows"]} journeys had no result, and {counts["skipped"]} were skipped. '
        f'Journeys passed: {counts["journeys_passed"]} of {counts["journeys_total"]} ({counts["journeys_passed_percent"]}%).'
    )
    table = _grid(document, ("Overall status", "Tests run", "Passed", "Failed", "Skipped", "Journeys with no result", "Recommendation"))
    values = (status, str(counts["executed_tests"]), str(counts["passed"]), str(counts["failed"]),
              str(counts["skipped"]), str(counts["blocked_flows"]), recommendation)
    for cell, value in zip(table.add_row().cells, values):
        cell.text = value
    document.add_heading("Conditions and top risks", 2)
    conditions = _lean_conditions(data["conditions"], lean)
    if conditions:
        for condition in conditions:
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


def _environment_section(document, run: RunReport) -> None:
    document.add_heading("Test Environment", 1)
    env = environment_block(run)
    table = _grid(document, ("Item", "Value"))
    values = [("URL scope", run.base_url or "All discovered URLs"), ("Browser", f'{env.get("Browser", "Chromium")} {env.get("Browser version", "NOT CAPTURED")}'),
              ("Operating system", env.get("OS", "unknown")), ("Python", env.get("Python", "unknown")),
              ("Playwright", env.get("Playwright", "NOT CAPTURED")), ("pytest", env.get("pytest", "NOT CAPTURED")),
              ("Pipeline version", f'{env.get("Pipeline version", "NOT CAPTURED")} ({env.get("Pipeline git SHA", "NOT CAPTURED")})'),
              ("Desktop/device", env.get("Device", "Desktop (no device emulation)")),
              ("Test data/accounts", "Configured local test account; secrets excluded from the report")]
    for key, value in values:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = key, value


def _conclusion_section(document, run: RunReport, report_data: dict[str, Any] | None = None, *, lean: bool = False) -> None:
    document.add_heading("Conclusions & Recommendations", 1)
    data = report_data or _report_data(run, Path.cwd())
    recommendation = data["recommendation"]
    if recommendation == "GO":
        document.add_paragraph("The tested scope is suitable for release. Review the documented out-of-scope items before signing off.")
    elif recommendation == "CONDITIONAL GO":
        document.add_paragraph("Release is acceptable only if every condition listed in the Executive Summary is reviewed and accepted.")
    else:
        document.add_paragraph("Do not release based on this run. The open defects must be resolved and the affected journeys rerun.")
    for condition in _lean_conditions(data["conditions"], lean) or ["Review the out-of-scope mobile and compatibility coverage before sign-off."]:
        document.add_paragraph(condition, style="List Bullet")
    _recommendation_plan(document, run, data)


def _recommendation_plan(document, run: RunReport, data: dict[str, Any]) -> None:
    """What to do about it, built from this run: manual checks for what could not be automated, process fixes, and
    what to add to the next test run. Every line is tied to something the run actually found."""
    blocked = [d for d in data["defects"] if d.get("blocking") or d.get("failure_class") in {"failed_last_check", "blocked_flow"}]
    document.add_heading("Immediate mitigation", 2)
    if blocked:
        document.add_paragraph("Until these are automated or fixed, have a person check them by hand before release:")
        for d in blocked[:12]:
            document.add_paragraph(f'{d["id"]}: {_shorten(d["title"], 110)}', style="List Bullet")
        if len(blocked) > 12:
            document.add_paragraph(f"...and {len(blocked) - 12} more listed in the defect report.")
    else:
        document.add_paragraph("No journey needs a manual check beyond the defects already listed.")
    document.add_heading("Process improvements", 2)
    steps = []
    if run.human_input_pages:
        steps.append("Ask the site owner for a staging bypass (test key or disabled check) for the CAPTCHA forms so they can be automated.")
    if run.untested_auth:
        steps.append("Provide a dedicated test account for the areas behind a sign-in, so those pages are tested.")
    if sum(f.passed and f.navigation_only for f in run.flow_reports):
        steps.append("Give navigation-only journeys an outcome check (a heading or result that must appear) so passing means the page is right.")
    if not (run.browser_info or {}).get("device"):
        steps.append("Add a phone-size run (--device \"iPhone 14\") and a second engine (--browser firefox) to the release routine.")
    for step in steps or ["No process change is suggested by this run."]:
        document.add_paragraph(step, style="List Bullet")
    document.add_heading("Revised test plan for the next run", 2)
    plan = ["Re-run the failed and blocked journeys above and update their status."]
    critical = _critical_untouched(run.coverage) if run.coverage else []
    if critical:
        plan.append(f"Write plain-sentence requirements for the {len(critical)} untested main-action control(s) listed under coverage, then rebuild flows from them.")
    plan.append("Repeat the critical path (search, tune, subscribe) on the phone-size and second-engine runs.")
    for step in plan:
        document.add_paragraph(step, style="List Bullet")


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
    document.add_paragraph("This report states whether the tested scope is ready for release, identifies the checks that need attention, and records the remaining coverage risks. Technical evidence is indexed in the appendix.")
    _executive_summary_section(document, run, report_data, lean=lean)
    _scope_section(document, run)
    _environment_section(document, run)
    _test_summary_section(document, run, report_data)
    chart = _write_execution_chart(destination.parent, report_data["counts"])
    if chart:
        document.add_paragraph("Result breakdown")
        document.add_picture(str(chart), width=Inches(5.7))
    document.add_page_break()
    document.add_heading("Defect Report (Bugs Found)", 1)
    _findings_section(document, run, report_data, compact=lean)
    link_base = destination.parent
    if run.tested_flows:
        document.add_heading("User flows", 1)
        document.add_paragraph(
            "Each flow is one user journey, tested by a generated spec whose steps and assertions come from a "
            "real verified browser run. The title is the flow's plain-language description. Full detail for "
            "each flow (journey, evidence, review history) is in the Appendix.")
        if lean:
            attention = [f for f in run.tested_flows if not f.passed]
            document.add_paragraph(f"{len(run.tested_flows) - len(attention)} of {len(run.tested_flows)} flows passed; the full list is in the appendix.")
            if attention:
                _flows_table(document, attention)
        else:
            _flows_table(document, run.tested_flows)
    if lean and run.untested_flows:
        document.add_paragraph(f"{len(run.untested_flows)} more flow(s) have no test result in this run; each is a BUG or BLOCK entry in the Defect Report, and the table with their history is in the appendix.")
    else:
        _untested_flows_section(document, run.untested_flows, detail=not lean)
    _coverage_section(document, run.coverage, per_page=not lean)
    _traceability_section(document, run, gaps_only=lean)
    if not lean:
        document.add_heading("Test Logs & Evidence", 1)
        document.add_paragraph("Screenshots, videos, traces, assertions, and failure details are indexed in the appendix. The machine-readable source is included as report-data.json.")
    _risks_section(document, run, report_data, lean=lean)
    _conclusion_section(document, run, report_data, lean=lean)
    has_evidence = bool(run.tested_flows or any(r.outcomes for r in run.url_reports))
    if not lean:
        _appendix_section(document, run, link_base=link_base)
    elif has_evidence:
        document.add_heading("Appendix: full evidence", 1)
        para = document.add_paragraph(
            "The searchable test index, evidence index, per-journey detail (steps, history, screenshots) and raw failure output are in ")
        _add_hyperlink(para, APPENDIX_FILE, APPENDIX_FILE)
        para.add_run(" in the same folder. The machine-readable source is report-data.json.")
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
        _traceability_section(document, run)
    _appendix_section(document, run, link_base=link_base)
    _bookmark_headings(document)
    _save_document(document, destination, run)


def _appendix_section(document, run: RunReport, *, link_base: Path | None) -> None:
    """Indexed raw evidence, deduplicated screenshots, and machine-readable source data."""
    if not run.tested_flows and not any(r.outcomes for r in run.url_reports):
        return
    document.add_page_break()
    document.add_heading("Appendix: full evidence", 1)
    document.add_paragraph(
        "This appendix contains the searchable test index, screenshots, attachments, assertions, and failure details. "
        "Repeated screenshots are stored once and referenced by evidence ID.")
    para = document.add_paragraph("Machine-readable source: ")
    _add_hyperlink(para, "report-data.json", "report-data.json")

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
    for report in run.url_reports:
        build_url_docx(run, report, out_dir / f"{name_for(report.url)}.docx")
    if combined:
        build_combined_docx(run, out_dir / "full-report.docx", report_data, appendix=appendix)
    return run
