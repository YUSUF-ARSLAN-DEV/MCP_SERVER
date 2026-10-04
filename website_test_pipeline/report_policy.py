"""Rules the stakeholder report must obey, kept apart from the Word rendering so they can be tested directly.

Everything here is pure data in / data out (plus two best-effort lookups: the site's build id and the CI link).
A rule that is broken raises ReportConsistencyError: the report is not emitted rather than emitted wrong.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

NBH = "‑"            # non-breaking hyphen: keeps "DEF-001" on one line in a narrow ID column


class ReportConsistencyError(ValueError):
    """The report data breaks a rule (a count that does not reconcile, a dangling ID, a missing owner ...)."""


# ----------------------------------------------------------------------------------------------- taxonomy

CATEGORIES = ("defect", "unverified", "limitation", "test_defect", "tooling")
PREFIX = {"defect": "DEF", "unverified": "UNV", "limitation": "LIM", "test_defect": "TST", "tooling": "TOOL"}
CATEGORY_LABEL = {"defect": "Defect", "unverified": "Unverified", "limitation": "Limitation",
                  "test_defect": "Test defect", "tooling": "Tooling issue"}
ID_RE = re.compile(r"\b(?:DEF|UNV|LIM|TST|TOOL|COV)[-" + NBH + r"]\d{3}\b")

_LIMITATION_KINDS = {"human_input_required", "authentication_required"}
_UNVERIFIED_KINDS = {"inconclusive", "blocked_flow", "auth_failure"}

# A skipped test is the harness declining to run, never a result: the validator rejected the spec, the model
# returned no code, the spec never named its target page.
TOOLING_SKIP = re.compile(r"no spec passed validation|did not return a python code block|target url missing|not testable", re.I)


def category_for(kind: str | None) -> str:
    if kind == "test_defect":
        return "test_defect"
    if kind in _LIMITATION_KINDS:
        return "limitation"
    if kind in _UNVERIFIED_KINDS:
        return "unverified"
    return "defect"


def display_id(value: str) -> str:
    return re.sub(r"^(DEF|UNV|LIM|TST|TOOL|COV)-", lambda m: m.group(1) + NBH, value or "")


def plain_id(value: str) -> str:
    return (value or "").replace(NBH, "-")


def id_range(ids: list[str]) -> str:
    """"UNV-001 to UNV-013" for a contiguous run, otherwise the IDs listed."""
    ids = sorted(ids)
    if not ids:
        return ""
    if len(ids) == 1:
        return ids[0]
    nums = [int(i.rsplit("-", 1)[-1]) for i in ids]
    if nums == list(range(nums[0], nums[0] + len(nums))) and len(ids) > 2:
        return f"{ids[0]} to {ids[-1]}"
    return ", ".join(ids)


# ----------------------------------------------------------------------------------- triage: product vs test

_PAGE_URL_EXPECTED = re.compile(r"Page URL expected[^'\"\n]*['\"]([^'\"\n]+)['\"]")
_ACTUAL_VALUE = re.compile(r"Actual value:\s*(\S+)")
_STATED_TARGET = re.compile(r"->\s*(\S+)")
_URL_LITERAL = re.compile(r"to_have_url\([^'\"]*['\"]([^'\"]+)['\"]")


def _url_path(value: str) -> str:
    value = re.sub(r"^https?://[^/]+", "", value or "")
    value = value.split("#", 1)[0]
    return value.rstrip("/") or "/"


def _regex_path(pattern: str) -> str:
    text = pattern.replace("\\.", ".").replace("\\-", "-").replace("\\/", "/")
    text = re.sub(r"/\?\(\?:\[\?#\]\.\*\)\?\$?$", "", text)
    text = re.sub(r"[\^$]|\.\*", "", text)
    return _url_path(text)


def triage_test_defect(error: str | None, stated_expectation: str = "", assertions: list[str] | None = None) -> str | None:
    """A reason when a failure is a fault in the test, not the application; None when it is a real failure.

    Two shapes are recognised: (1) the URL the test expected and the URL it observed resolve to the same address
    (the assertion cannot be right and wrong at once); (2) the sentence stating the expectation names a different
    address from the one the assertion that ran checks."""
    raw = error or ""
    expected = _PAGE_URL_EXPECTED.search(raw)
    actual = _ACTUAL_VALUE.search(raw)
    if expected and actual:
        want, got = expected.group(1), actual.group(1)
        same = _url_path(want) == _url_path(got)
        if not same:
            try:
                same = bool(re.search(want, got))
            except re.error:
                same = False
        if same:
            return f"The expected and observed address resolve to the same URL ({_url_path(got)}); the assertion is wrong, not the page."
    stated = _STATED_TARGET.search(stated_expectation or "")
    if stated and assertions:
        for code in reversed(assertions):
            literal = _URL_LITERAL.search(code)
            if literal:
                target, ran = _url_path(stated.group(1)), _regex_path(literal.group(1))
                if target not in ran and ran not in target:
                    return (f"The stated expectation ({target}) does not match the assertion that ran ({ran}); "
                            "the test checks something other than what it says.")
                break
    return None


# --------------------------------------------------------------------------- severity from user impact

_CORE_ELEMENT = re.compile(r"\bheading\b|\bh1\b|\bmain\b|banner|navigation|\bheader\b|logo|hero|page title|content area|\bmenu\b|\bnav\b", re.I)
_URL_SIGNAL = re.compile(r"to_have_url|page url expected|ended up at|wrong (?:page|address)|navigat", re.I)
_RENDER_SIGNAL = re.compile(r"to_be_visible|never appeared|never became visible|not found|could not be found|never found", re.I)
_HIGH_VALUE = re.compile(r"check ?out|\bpay|purchase|\bbuy\b|\border\b|subscribe|sign ?up|register|log ?in|sign ?in|account|password|search|contact|submit|download|donat|book|apply|cart", re.I)
_LOW_VALUE = re.compile(r"language|footer|theme|cookie|social|share|about|terms|privacy|setting|newsletter|scroll|back to top|copyright", re.I)


def business_value(title: str) -> str:
    if _HIGH_VALUE.search(title or ""):
        return "High"
    if _LOW_VALUE.search(title or ""):
        return "Low"
    return "Medium"


def impact_severity(category: str, *, scope: str, title: str, failure_text: str = "", assertion_text: str = "") -> str:
    """Severity from what a visitor loses, not from how the failure was classified.

    Defect: a core page element that does not render is Critical; landing on the wrong address is High; a broken
    journey is High unless it only concerns a secondary control; a cosmetic or secondary control is Medium.
    Unverified: the business value of the journey (default Medium). Limitation, test defect, tooling: Low."""
    if category in {"limitation", "test_defect", "tooling"}:
        return "Low"
    if category == "unverified":
        return business_value(title)
    text = f"{failure_text}\n{assertion_text}"
    secondary = bool(_LOW_VALUE.search(title or "")) and not _HIGH_VALUE.search(title or "")
    if _URL_SIGNAL.search(text):
        return "Medium" if secondary else "High"
    if _RENDER_SIGNAL.search(text):
        return "Critical" if _CORE_ELEMENT.search(f"{title}\n{assertion_text}") else "Medium"
    if scope == "flow":
        return "Medium" if secondary else "High"
    return "Medium"


def priority_for(category: str, severity: str) -> str:
    """Urgency follows severity; a limitation is never P1."""
    if category in {"limitation", "test_defect", "tooling"}:
        return "P3 - when convenient"
    if severity in {"Critical", "High"}:
        return "P1 - before release"
    return "P2 - next fix cycle" if severity == "Medium" else "P3 - when convenient"


def assert_priorities_vary(records: list[dict[str, Any]]) -> None:
    """Severity must come from user impact, so a run in which every scored item (Defects and Unverified journeys) has the
    same priority was not scored. Limitations, test defects and tooling issues have a priority fixed by the rules
    (always P3), so they cannot show scoring variety and are not part of this check."""
    scored = [r for r in records if r["category"] in {"defect", "unverified"}]
    if len(scored) >= 2 and len({r["priority"] for r in scored}) == 1:
        raise ReportConsistencyError(
            f"all {len(scored)} scored items (defects and unverified journeys) have the same priority ({scored[0]['priority']}); "
            "severity was not computed from user impact. The report is not emitted.")


# -------------------------------------------------------------------------------------- release decision

def release_decision(records: list[dict[str, Any]]) -> str:
    """Derived from the Defects table first. Any open High/Critical Defect is NO-GO; with none, any other open
    Defect or any open Unverified journey is CONDITIONAL GO. Limitations, test defects and tooling issues never
    change the verdict, and none of them is ever a release blocker."""
    open_defects = [r for r in records if r["category"] == "defect" and r["status"] == "Open"]
    if any(r["severity"] in {"Critical", "High"} for r in open_defects):
        return "NO-GO"
    if open_defects or any(r["category"] == "unverified" and r["status"] == "Open" for r in records):
        return "CONDITIONAL GO"
    return "GO"


def taxonomy_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {c: sum(r["category"] == c for r in records) for c in CATEGORIES}


def taxonomy_sentence(tax: dict[str, int]) -> str:
    return (f"{tax['defect']} application defect(s), {tax['unverified']} unverified journey(s), "
            f"{tax['limitation']} tooling limitation(s), {tax['test_defect']} test defect(s) and "
            f"{tax['tooling']} tooling issue(s) were recorded.")


def defect_headline(tax: dict[str, int]) -> str:
    n = tax["defect"]
    if n == 0:
        return "0 application defects found this run. No application defect was confirmed by any test that ran."
    return f"{n} application defect{'s' if n != 1 else ''} found this run."


def build_conditions(records: list[dict[str, Any]], cov_ids: list[str], verdict: str) -> list[str]:
    """At most five bullets: repeated items collapse into a count with an ID range."""
    def ids(cat: str, cls: set[str] | None = None) -> list[str]:
        return [plain_id(r["id"]) for r in records
                if r["category"] == cat and (cls is None or r.get("failure_class") in cls)]

    fixed: list[str] = []
    defects = ids("defect")
    if defects:
        fixed.append((f"{len(defects)} application defect(s) must be fixed and retested before release ({id_range(defects)}, section 5)."
                      if verdict == "NO-GO" else
                      f"{len(defects)} open defect(s) must be fixed or accepted by the owner ({id_range(defects)}, section 5)."))
    unverified = ids("unverified")
    if unverified:
        fixed.append(f"{len(unverified)} journeys need a manual pass before sign-off ({id_range(unverified)}, section 5).")
    if cov_ids:
        fixed.append(f"{len(cov_ids)} page(s) have controls that no journey exercised ({id_range(cov_ids)}, section 6).")
    harness = ids("tooling") + ids("test_defect")
    if harness:
        fixed.append(f"{len(harness)} tooling issue(s) left tests unrun or invalid; they hide real results ({id_range(harness)}, section 5).")
    limits = [r for r in records if r["category"] == "limitation"]
    lim_bullets: list[str] = []
    if limits and len(fixed) + len(limits) <= 5:
        for r in limits:
            lim_bullets.append(f"{plain_id(r['id'])} ({r.get('url') or 'no URL'}): {r.get('mitigation') or r['title']}")
    elif limits:
        by_class: dict[str, list[dict]] = {}
        for r in limits:
            by_class.setdefault(r.get("failure_class", "limitation"), []).append(r)
        if len(fixed) + len(by_class) <= 5:
            label = {"human_input_required": "form(s) need a CAPTCHA or code that no automated test can pass",
                     "authentication_required": "sign-in wall(s) need a test account"}
            for cls, rows in by_class.items():
                lim_bullets.append(f"{len(rows)} {label.get(cls, 'item(s) cannot be tested automatically')} "
                                   f"({id_range([plain_id(r['id']) for r in rows])}, section 5; each URL is in the table).")
        else:
            lim_bullets.append(f"{len(limits)} item(s) cannot be tested automatically "
                               f"({id_range([plain_id(r['id']) for r in limits])}, section 5; each URL is in the table).")
    bullets = fixed + lim_bullets
    if len(bullets) > 5:
        raise ReportConsistencyError(f"executive summary has {len(bullets)} bullets; the maximum is five")
    return bullets


def exit_criteria(records: list[dict[str, Any]], cov_ids: list[str], verdict: str) -> list[str]:
    """What flips the CURRENT verdict to GO - conditions, not a restatement of the executive summary."""
    def ids(cat: str) -> list[str]:
        return [plain_id(r["id"]) for r in records if r["category"] == cat]

    out = []
    if ids("defect"):
        out.append(f"Every application defect ({id_range(ids('defect'))}) is fixed, retested and closed.")
    if ids("unverified"):
        out.append(f"Every unverified journey ({id_range(ids('unverified'))}) is manually signed off, with an owner named on each.")
    if ids("limitation"):
        out.append(f"Each limitation ({id_range(ids('limitation'))}) is either automated (staging bypass or test account) or accepted in writing by the site owner.")
    if ids("tooling") or ids("test_defect"):
        out.append(f"The tooling issues ({id_range(ids('tooling') + ids('test_defect'))}) are repaired and the affected pages and journeys rerun with a real result.")
    if cov_ids:
        out.append(f"The coverage gaps ({id_range(cov_ids)}) are reviewed by a person or covered by a new journey.")
    if not out:
        out.append("No further condition applies; rerun on the next build to keep this verdict.")
    return out


# -------------------------------------------------------------------------------- owners and due dates

_PLACEHOLDER_OWNER = {"", "unassigned", "tbd", "tba", "n/a", "na", "none", "unknown", "?", "-"}
OWNERS_FILE = "owners.json"
_SLA_DAYS = {"P1": 3, "P2": 7, "P3": 14}


def load_owner_config(workspace: Path | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if workspace:
        try:
            data = json.loads((Path(workspace) / OWNERS_FILE).read_text(encoding="utf-8"))
            cfg = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            cfg = {}
    if os.environ.get("REPORT_OWNER"):
        cfg = {**cfg, "default": os.environ["REPORT_OWNER"]}
    if os.environ.get("REPORT_RELEASE_DATE"):
        cfg = {**cfg, "release_date": os.environ["REPORT_RELEASE_DATE"]}
    return cfg


def _real_name(value: Any) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in _PLACEHOLDER_OWNER else text


def resolve_owner(cfg: dict[str, Any], record: dict[str, Any]) -> str | None:
    for source in ((cfg.get("by_url") or {}).get(record.get("url") or ""),
                   (cfg.get("by_category") or {}).get(record["category"]), cfg.get("default")):
        if name := _real_name(source):
            return name
    return None


def due_date(priority: str, run_date: date, cfg: dict[str, Any]) -> str:
    """An ISO date. A release date in the config caps it; otherwise run date plus the SLA for the priority."""
    sla = {**_SLA_DAYS, **{k: int(v) for k, v in (cfg.get("sla_days") or {}).items() if str(v).isdigit()}}
    due = run_date + timedelta(days=sla.get(priority.split(" ")[0], 14))
    release = str(cfg.get("release_date") or "")
    if release:
        try:
            due = min(due, date.fromisoformat(release))
        except ValueError:
            raise ReportConsistencyError(f"release_date {release!r} is not an ISO date (YYYY-MM-DD)")
    return due.isoformat()


def assign_owners(records: list[dict[str, Any]], cfg: dict[str, Any], run_date: date) -> None:
    missing = []
    for r in records:
        owner = resolve_owner(cfg, r)
        if owner is None:
            missing.append(plain_id(r["id"]))
            continue
        r["owner"] = owner
        r["due"] = due_date(r["priority"], run_date, cfg)
    if missing:
        raise ReportConsistencyError(
            f"no owner for {len(missing)} item(s) ({id_range(missing)}). Name a real person: set REPORT_OWNER, or add "
            f'"default" / "by_category" / "by_url" to {OWNERS_FILE} in the run folder. The report is not emitted with "Unassigned".')


# ------------------------------------------------------------------------------------- run identity, build

def run_id_for(manifest: dict, finished_at: str, started_at: str, results_text: str) -> str:
    if manifest.get("run_id"):
        return str(manifest["run_id"])
    stamp = re.sub(r"[^0-9]", "", finished_at or started_at or "")[:14]
    if stamp:
        return f"run-{stamp}"
    return "run-" + hashlib.sha1(results_text.encode("utf-8", "replace")).hexdigest()[:10]


def ci_link() -> str:
    env = os.environ
    for name in ("CI_JOB_URL", "BUILD_URL", "CIRCLE_BUILD_URL", "BUILDKITE_BUILD_URL", "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI"):
        if env.get(name):
            return env[name]
    if env.get("GITHUB_RUN_ID") and env.get("GITHUB_REPOSITORY"):
        return f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    return "NOT CAPTURED: this run was not started from a CI job (set CI_JOB_URL to record one)"


_BUILD_HEADERS = ("x-build-id", "x-build-number", "x-commit-sha", "x-git-commit", "x-git-sha", "x-app-version", "x-release", "x-version")
_BUILD_META = re.compile(r'<meta[^>]+name=["\'](?:build[-_]?id|build|commit|revision|app[-_]?version|version|release)["\'][^>]+content=["\']([^"\']+)', re.I)


def app_build_info(base_url: str, *, probe: bool = True) -> dict[str, str]:
    """The build of the SITE UNDER TEST (not the pipeline). A remote site only reveals one if it publishes it, so
    the order is: what the CI gave us (APP_BUILD_ID), then what the site itself exposes (response header, <meta>,
    /version.json or /build.json); otherwise NOT CAPTURED with the reason."""
    if os.environ.get("APP_BUILD_ID"):
        return {"id": os.environ["APP_BUILD_ID"], "source": "APP_BUILD_ID set for this run"}
    if not probe or not base_url:
        return {"id": "NOT CAPTURED", "source": "build probe skipped; set APP_BUILD_ID to the deployed commit SHA or build number"}
    try:
        request = urllib.request.Request(base_url, headers={"User-Agent": "website-test-pipeline-report"})
        with urllib.request.urlopen(request, timeout=6) as response:
            for name in _BUILD_HEADERS:
                if value := response.headers.get(name):
                    return {"id": value.strip(), "source": f"response header {name} of {base_url}"}
            html = response.read(200_000).decode("utf-8", "replace")
        if match := _BUILD_META.search(html):
            return {"id": match.group(1).strip(), "source": f"<meta> tag on {base_url}"}
        root = re.match(r"https?://[^/]+", base_url)
        for path in ("/version.json", "/build.json"):
            try:
                with urllib.request.urlopen((root.group(0) if root else base_url) + path, timeout=4) as extra:
                    data = json.loads(extra.read(20_000).decode("utf-8", "replace"))
                for key in ("commit", "sha", "gitSha", "build", "buildId", "version", "revision"):
                    if isinstance(data, dict) and data.get(key):
                        return {"id": str(data[key]), "source": f"{path} of the site"}
            except Exception:
                continue
    except Exception as exc:
        return {"id": "NOT CAPTURED", "source": f"the site could not be read ({type(exc).__name__}); set APP_BUILD_ID"}
    return {"id": "NOT CAPTURED", "source": "the site publishes no build identifier (no header, meta tag or /version.json); set APP_BUILD_ID"}


RETRY_POLICY = "None: every test runs once, a failure is not retried (no pytest --reruns); a flaky result shows up as a flapping risk instead."


def viewport_text(browser_info: dict) -> str:
    info = browser_info or {}
    if info.get("viewport"):
        v = info["viewport"]
        return f"{v.get('width')} x {v.get('height')} px" if isinstance(v, dict) else str(v)
    if info.get("device"):
        return f"device emulation \"{info['device']}\" (the device profile's own viewport)"
    return "1280 x 720 px (Playwright's desktop default; the tests do not override it)"


# --------------------------------------------------------------------------------------- run-over-run delta

HISTORY_FILE = "run-history.json"
_KEEP_RUNS = 30


def stable_test_key(nodeid: str) -> str:
    node = (nodeid or "").replace("\\", "/")
    head, _, rest = node.partition("::")
    return head.rsplit("/", 1)[-1] + "::" + rest if rest else node


def snapshot(*, run_id: str, finished_at: str, counts: dict[str, Any], tests: dict[str, dict[str, str]], pages: list[str],
             tooling_urls: list[str], untested_files: list[str], env: dict[str, str]) -> dict[str, Any]:
    return {"run_id": run_id, "finished_at": finished_at, "pass_rate_percent": counts.get("pass_rate_percent", 0),
            "tests_ran": counts.get("executed_tests", 0), "tests": tests, "pages": sorted(set(pages)),
            "tooling_urls": sorted(set(tooling_urls)), "untested_files": sorted(set(untested_files)), "env": env}


def load_history(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        runs = data.get("runs") if isinstance(data, dict) else None
        return [r for r in runs if isinstance(r, dict) and r.get("run_id")] if isinstance(runs, list) else []
    except (OSError, ValueError):
        return []


def save_history(path: Path, history: list[dict[str, Any]], current: dict[str, Any]) -> None:
    runs = [r for r in history if r["run_id"] != current["run_id"]] + [current]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"runs": runs[-_KEEP_RUNS:]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def previous_run(history: list[dict[str, Any]], run_id: str) -> dict[str, Any] | None:
    earlier = [r for r in history if r["run_id"] != run_id]
    return earlier[-1] if earlier else None


def _failing(status: str | None) -> bool:
    return status in {"failed", "error"}


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", value or ""))


def environment_changes(prev_env: dict[str, str], cur_env: dict[str, str]) -> list[str]:
    out = []
    for label, key in (("Browser", "Browser"), ("Browser version", "Browser version"), ("Playwright", "Playwright")):
        before, after = prev_env.get(key, ""), cur_env.get(key, "")
        if not before or not after or before == after or "NOT CAPTURED" in (before, after):
            continue
        a, b = _version_tuple(before), _version_tuple(after)
        direction = "upgraded" if a and b and b > a else "downgraded" if a and b and b < a else "changed"
        out.append(f"{label} {direction}: {before} -> {after}. Results may differ because of the tool, not the site.")
    return out


def compute_delta(prev: dict[str, Any] | None, cur: dict[str, Any]) -> dict[str, Any]:
    if prev is None:
        return {"baseline": False, "note": "No previous run is recorded for this site, so this run is the baseline for the next comparison."}
    pt, ct = prev.get("tests", {}), cur.get("tests", {})
    added_raw = [k for k in ct if k not in pt]
    removed_raw = [k for k in pt if k not in ct]
    renamed: list[tuple[str, str]] = []
    for old in list(removed_raw):
        file = old.split("::")[0]
        candidates = [n for n in added_raw if n.split("::")[0] == file]
        best, score = None, 0.0
        for new in candidates:
            ratio = difflib.SequenceMatcher(None, old.split("::", 1)[-1], new.split("::", 1)[-1]).ratio()
            if ratio > score:
                best, score = new, ratio
        if best is not None and score >= 0.6:
            renamed.append((old, best))
            added_raw.remove(best)
    renamed_old = {o for o, _ in renamed}
    renamed_new = dict(renamed)
    removed = []
    for key in removed_raw:
        info = pt[key]
        if key in renamed_old:
            reason = f"renamed to {renamed_new[key].split('::', 1)[-1]}"
        elif info.get("file") in cur.get("untested_files", []):
            reason = "journey has no test this run (see Unverified items, section 5)"
        elif info.get("url") in cur.get("tooling_urls", []):
            reason = "spec failed validation (see Tooling issues, section 5)"
        elif info.get("url") and info["url"] not in cur.get("pages", []):
            reason = "page no longer discovered"
        else:
            reason = "deleted (the current spec for that page has no matching test)"
        removed.append({"id": key, "title": info.get("title", key), "reason": reason})
    common = [k for k in ct if k in pt]
    return {
        "baseline": True,
        "previous_run_id": prev["run_id"], "previous_finished_at": prev.get("finished_at", ""),
        "previous_pass_rate_percent": prev.get("pass_rate_percent", 0), "previous_tests_ran": prev.get("tests_ran", 0),
        "added": [{"id": k, "title": ct[k].get("title", k)} for k in added_raw],
        "removed": removed,
        "fixed": [{"id": k, "title": ct[k].get("title", k)} for k in common if _failing(pt[k].get("status")) and ct[k].get("status") == "passed"],
        "still_failing": [{"id": k, "title": ct[k].get("title", k)} for k in common if _failing(pt[k].get("status")) and _failing(ct[k].get("status"))],
        "new_failures": [{"id": k, "title": ct[k].get("title", k)} for k in ct if _failing(ct[k].get("status")) and not _failing(pt.get(k, {}).get("status"))],
        "renamed_count": len(renamed),
        "environment_changes": environment_changes(prev.get("env", {}), cur.get("env", {})),
    }


# ---------------------------------------------------------------------------------- evidence and ID checks

def evidence_reason(category: str, failure_class: str, reason_detail: str = "") -> str:
    if category == "limitation":
        return (f"No screenshot exists: the run stopped at the {reason_detail or 'sign-in'} step, so there is nothing past it to capture."
                if failure_class == "human_input_required" else
                "No screenshot exists: the run stopped at the sign-in form, so there is no signed-in page to capture.")
    if category == "unverified":
        return "No screenshot exists: no test produced a result for this journey in this run, so no browser step was captured."
    if category == "tooling":
        return "No screenshot exists: the test was skipped before any browser step ran."
    return "No screenshot or trace was captured: the test ended before its first evidence step."


def check_ids(sections: dict[str, set[str]], defined: set[str]) -> None:
    """No ID may be cited in section 1, 8 or 9 unless it has a row in section 5 or 6."""
    dangling = sorted({plain_id(i) for ids in sections.values() for i in ids} - {plain_id(i) for i in defined})
    if dangling:
        raise ReportConsistencyError("ID(s) cited without a row in section 5 or 6: " + ", ".join(dangling))
