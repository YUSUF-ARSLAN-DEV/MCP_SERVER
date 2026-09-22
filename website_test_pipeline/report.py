"""Build human-verifiable Word reports from a test run.

The report exists so a person can confirm the pass/fail numbers instead of
trusting the generator + runner blindly: every test is shown with the
assertions it made and the screenshot evidence captured at each step.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from .coverage import Coverage, compute_coverage
from .findings import Finding, FlapRecord, collect_findings, detect_flapping, environment_block
from .flowgen import file_name as flow_spec_name
from .flowreport import FlowReport, build_flow_report
from .flows import FlowsFileError, load_flows
from .ratings import RatingsFileError, load_ratings
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
    flapping: list[FlapRecord] = field(default_factory=list)       # flows/tests whose recent runs mix pass and fail

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
        return sum(u.total for u in self.url_reports) + len(self.tested_flows)

    @property
    def passed(self) -> int:
        return sum(u.passed for u in self.url_reports) + sum(f.passed for f in self.tested_flows)

    @property
    def failed(self) -> int:
        return sum(u.failed for u in self.url_reports) + sum(f.failed for f in self.tested_flows)

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
    return reports, {r.get("nodeid") for rows in rows_by_flow.values() for r in rows}


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


def _find_attachments(pw_dir: Path, slug: str) -> list[str]:
    if not pw_dir.is_dir():
        return []
    out: list[str] = []
    for child in pw_dir.iterdir():
        if child.is_dir() and slug and slug in child.name:
            out.extend(str(p) for p in sorted(child.iterdir()) if p.suffix.lower() in ATTACH_EXTS)
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


def _render_evidence(document, outcome: TestOutcome, *, embed: bool = True, link_base: Path | None = None) -> None:
    document.add_heading("Browser evidence", 3)
    if not outcome.evidence:
        para = document.add_paragraph("No screenshot evidence was captured for this test.")
        para.runs[0].italic = True
    for image in outcome.evidence:
        path = Path(image)
        if not path.is_file():
            continue
        if embed:
            try:
                document.add_picture(_embeddable(path), width=Inches(6.0))
            except Exception as exc:  # unreadable / truncated screenshot
                document.add_paragraph(f"(could not embed {path.name}: {exc})")
                continue
            caption = document.add_paragraph(path.stem.replace("-", " "))
            caption.runs[0].italic = True
            caption.runs[0].font.size = Pt(9)
        else:
            para = document.add_paragraph(f"{path.stem.replace('-', ' ')}  —  ", style="List Bullet")
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
    para.add_run(f"    Flow status: {flow.status}    Duration: {outcome.duration:.2f}s")
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
    try:
        table.style = "Table Grid"
    except KeyError:
        pass
    for cell, label in zip(table.rows[0].cells, labels):
        cell.paragraphs[0].add_run(label).bold = True
    return table


def _findings_section(document, run: RunReport) -> None:
    """The headline of the report: what to trust, what to fix, and what to look at first - ahead of the
    raw counts and the page-by-page detail. Environment, then which recent results are unsettled (a
    tested flow whose own history flips between pass and fail is not a fact to rely on yet), then every
    failure this run classified by kind (a wrong assertion vs. a live, content-dependent result vs.
    genuinely unclear) with a one-line reproduction and what to do about it."""
    document.add_heading("Findings", 1)
    env = environment_block(run)
    document.add_paragraph(" | ".join(f"{k}: {v}" for k, v in env.items()))
    if run.coverage and run.coverage.pages:
        cov = run.coverage
        para = document.add_paragraph()
        para.add_run(f"Coverage: {cov.pages_visited}/{cov.pages_total} pages visited by a tested flow "
                     f"({cov.percent_pages}%), {cov.controls_touched}/{cov.controls_total} content controls acted "
                     f"on ({cov.percent_controls}%). Detail below in “Flow coverage”.").bold = True

    if run.flapping:
        para = document.add_paragraph()
        run_ = para.add_run(f"{len(run.flapping)} flow(s) changed verdict across recent runs - "
                            "their current status is provisional, not settled:")
        run_.bold = True
        run_.font.color.rgb = _RED
        table = _grid(document, ("Flow", "Recent sequence (oldest -> newest)", "Why"))
        for flap in run.flapping:
            cells = table.add_row().cells
            cells[0].text = flap.test
            cells[1].text = flap.sequence
            cells[2].text = flap.note
        document.add_paragraph()

    if not run.findings:
        para = document.add_paragraph("No failures to triage this run.")
        para.runs[0].italic = True
        return
    document.add_paragraph(f"{len(run.findings)} failure(s), classified below. P1 = a user journey (flow); "
                           "P2 = a page-level check. test_defect means the assertion itself is provably wrong "
                           "against what was actually observed; flaky_data means the run completed but the site "
                           "did not return the content the sentence promised (may be content-dependent, not a "
                           "defect); unclear means it could not be classified automatically - read the trace.")
    table = _grid(document, ("Sev", "Kind", "Test", "Summary", "Next action"))
    for finding in run.findings:
        cells = table.add_row().cells
        cells[0].text = finding.severity
        cells[1].text = finding.kind
        cells[2].text = finding.test
        cells[3].text = finding.summary
        cells[4].text = finding.next_action
        if finding.kind != "test_defect":
            for cell in cells:
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.italic = True


def _shorten(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _short_error(error: str | None) -> str:
    match = re.search(r"^E\s+(.+)$", error or "", re.M)
    return _shorten(match.group(1) if match else (error or ""))


def _page_row(outcome: TestOutcome) -> tuple[str, str, str, str, str]:
    """(scope, test, url, expected, observed) for a page-level test - no verdict, added by the caller."""
    if outcome.assertions:
        extra = f" (+{len(outcome.assertions) - 1} more)" if len(outcome.assertions) > 1 else ""
        expected = _shorten(outcome.assertions[0]) + extra
    else:
        expected = "(no assertion captured)"
    observed = "as expected" if outcome.passed else (_short_error(outcome.error) or outcome.status)
    return "page", outcome.title.replace("_", " "), outcome.url, expected, observed


def _flow_row(flow: FlowReport) -> tuple[str, str, str, str, str]:
    return "flow", flow.title, flow.start_url, flow.expected or "(not stated)", flow.observed or "(never ran)"


def _test_summary_section(document, run: RunReport) -> None:
    """One row per test, page or flow, in one place - so a reader can scan what was tested, what it
    expected, and what actually happened without opening the per-page/per-flow sections at all.
    Failing tests are listed first (severity of the finding, if any, in the last column); the rest
    keep the report's normal order. Full detail (screenshots, ARIA snapshots, traces) stays in the
    per-page and per-flow sections below - this table is the index into them, not a replacement."""
    rows = [(*_page_row(o), not o.passed) for u in run.url_reports for o in u.outcomes]
    rows += [(*_flow_row(f), not f.passed) for f in run.tested_flows]
    if not rows:
        return
    severity_of = {f.test: f.severity for f in run.findings}
    document.add_heading("Test summary", 1)
    document.add_paragraph(f"{len(rows)} test(s): {sum(1 for r in rows if not r[-1])} passed, "
                           f"{sum(1 for r in rows if r[-1])} failed. Failing tests are listed first; "
                           "each links to full evidence in its own section below.")
    table = _grid(document, ("Scope", "Test", "URL", "Expected", "Observed", "Verdict"))
    for scope, test, url, expected, observed, failed in sorted(rows, key=lambda r: (not r[-1], r[0], r[1])):
        cells = table.add_row().cells
        cells[0].text = scope
        cells[1].text = test + (f" [{severity_of[test]}]" if failed and test in severity_of else "")
        cells[2].text = url
        cells[3].text = expected
        cells[4].text = observed
        cells[5].text = "FAILED" if failed else "PASSED"
        if failed:
            for p in cells[5].paragraphs:
                for r in p.runs:
                    r.font.color.rgb = _RED
                    r.bold = True


def _flows_table(document, flows: list[FlowReport]) -> None:
    table = _grid(document, ("Flow", "Flow status", "Result", "Pages"))
    for flow in flows:
        cells = table.add_row().cells
        cells[0].text = flow.title
        cells[1].text = flow.status
        cells[2].text = flow.outcome.status.upper() if flow.outcome else "not run"
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


def _coverage_section(document, coverage: Coverage | None) -> None:
    """How much of the explored site the tested flows touch, and where the untested parts are."""
    if coverage is None or not coverage.pages:
        return
    document.add_heading("Flow coverage", 1)
    document.add_paragraph(
        f"Pages visited by a tested flow: {coverage.pages_visited} of {coverage.pages_total} ({coverage.percent_pages}%). "
        f"Content controls a tested flow acts on: {coverage.controls_touched} of {coverage.controls_total} "
        f"({coverage.percent_controls}%). Flows: {coverage.tested_flows} tested, {coverage.planned_flows} not yet backed by a "
        "passing run. Header, navigation and footer links are left out of the totals: they repeat on every page and are "
        "checked by the page tests.")
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


def _untested_flows_section(document, flows: list[FlowReport]) -> None:
    if not flows:
        return
    document.add_heading("Flows without a test result in this run", 1)
    document.add_paragraph("These flows exist but no generated test ran for them (not verified yet, rejected, "
                           "or waiting to be rebuilt from a changed sentence).")
    table = _grid(document, ("Flow", "Flow status"))
    for flow in flows:
        cells = table.add_row().cells
        cells[0].text = flow.title
        cells[1].text = flow.status


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
    document.add_paragraph(f"Model: {run.model or 'unknown'}")
    document.add_paragraph(f"Run started: {run.started_at or 'unknown'}")
    document.add_paragraph(
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    document.add_paragraph(
        f"Totals: {run.total} tests | {run.passed} passed | {run.failed} failed"
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


def _save_document(document, destination: Path, run: RunReport) -> Path:
    """Save the document. If the file is locked (typically open in Word, which holds a ~$ lock file), write
    a '-new' copy next to it and say so, instead of letting one locked file abort the whole report."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        document.save(destination)
        return destination
    except PermissionError:
        alternative = destination.with_name(destination.stem + "-new" + destination.suffix)
        document.save(alternative)
        run.notes.append(f"{destination.name} is open in another program and could not be overwritten; "
                         f"the new report was written to {alternative.name}")
        return alternative


def build_url_docx(run: RunReport, report: UrlReport, destination: Path) -> None:
    document = Document()
    document.add_heading("Website Test Evidence Report", 0)
    document.add_heading(report.url, 1)
    _metadata(document, run, scope=f"single URL ({report.url})")
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
    _save_document(document, destination, run)


def build_combined_docx(run: RunReport, destination: Path) -> None:
    document = Document()
    document.add_heading("Website Test Evidence Report — Full Run", 0)
    _metadata(document, run, scope="all URLs")
    document.add_paragraph(
        "Screenshots are embedded beneath each test. Traces and videos remain "
        "linked; open this report from beside the artifacts/ folder so those links resolve."
    )
    _findings_section(document, run)
    document.add_page_break()
    _test_summary_section(document, run)
    document.add_page_break()
    _summary_table(document, run.url_reports)
    _warnings_section(document, run.warnings)
    link_base = destination.parent
    if run.tested_flows:
        document.add_page_break()
        _flows_section(document, run.tested_flows, embed=True, link_base=link_base)
    _untested_flows_section(document, run.untested_flows)
    _coverage_section(document, run.coverage)
    for report in run.url_reports:
        document.add_page_break()
        document.add_heading(report.url, 1)
        _summary_table(document, [report])
        if report.warnings:
            _warnings_section(document, report.warnings)
        for outcome in report.outcomes:
            document.add_page_break()
            _render_outcome(document, outcome, embed=True, link_base=link_base)
    _save_document(document, destination, run)


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
) -> RunReport:
    run = load_run(artifacts_dir, tests_dir, model=model, flows_file=flows_file, ratings_file=ratings_file)
    out_dir.mkdir(parents=True, exist_ok=True)
    for report in run.url_reports:
        build_url_docx(run, report, out_dir / f"{name_for(report.url)}.docx")
    if combined:
        build_combined_docx(run, out_dir / "full-report.docx")
    return run
