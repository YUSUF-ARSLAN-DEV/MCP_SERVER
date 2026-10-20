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

CATEGORIES = ("defect", "triage", "unverified", "limitation", "test_defect", "tooling")
PREFIX = {"defect": "DEF", "triage": "TRI", "unverified": "UNV", "limitation": "LIM", "test_defect": "TST", "tooling": "TOOL"}
CATEGORY_LABEL = {"defect": "Defect", "triage": "Needs triage", "unverified": "Unverified", "limitation": "Limitation",
                  "test_defect": "Test defect", "tooling": "Tooling issue"}
ID_RE = re.compile(r"\b(?:DEF|TRI|UNV|LIM|TST|TOOL|COV)[-" + NBH + r"]\d{3}\b")

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


# The test could not PERFORM a step (click, fill, select ... timed out, or another element was on top of the target).
# It never got far enough to see what the site does, so the failure says nothing yet about the site: a hidden skip
# link, a cookie banner over a button, or a wrong selector all look exactly like this. An expect() that ran and saw
# the wrong thing (wrong URL, missing text) is different: that is an observation of the site. Page loads (goto) and
# waits for something to appear are observations too, so they are not in this list.
_ACTION_TIMEOUT = re.compile(r"\b(?:Locator|Page|Frame|ElementHandle)\.(?:click|dblclick|fill|type|press|press_sequentially|check|uncheck|"
                             r"select_option|hover|tap|set_input_files|focus|clear|set_checked|drag_to): Timeout \d+ms exceeded")
_COVERED = re.compile(r"intercepts pointer events|another element (?:was covering|would receive the click)|strict mode violation", re.I)


def is_action_failure(error: str | None) -> bool:
    """True when the test failed while trying to act on the page, before it observed any outcome."""
    text = error or ""
    return bool(_ACTION_TIMEOUT.search(text) or _COVERED.search(text))


def triage_category(kind: str | None, error: str | None) -> str:
    """category_for, except that a failure nobody could explain ("unclear") that happened while the test was
    trying to act is "triage": a person looks at the trace before anyone calls it a bug in the site."""
    category = category_for(kind)
    if category == "defect" and (kind or "unclear") == "unclear" and is_action_failure(error):
        return "triage"
    return category


def display_id(value: str) -> str:
    return re.sub(r"^(DEF|TRI|UNV|LIM|TST|TOOL|COV)-", lambda m: m.group(1) + NBH, value or "")


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


def impact_severity(category: str, *, scope: str, title: str, failure_text: str = "", assertion_text: str = "",
                    historically_reliable: bool = False) -> str:
    """Severity from what a visitor loses, not from how the failure was classified.

    Defect: a core page element that does not render is Critical; landing on the wrong address is High; a broken
    journey is High unless it only concerns a secondary control; a cosmetic or secondary control is Medium.
    Unverified: the business value of the journey (default Medium). Limitation, test defect, tooling: Low.

    historically_reliable caps the result at Medium: a flow that has passed enough runs to still be considered
    verified, and whose current failure is a timing/visibility race rather than a confirmed wrong outcome, has
    demonstrably lower real-world impact than a newly-discovered Critical/High defect - this is still impact-based
    (reliability IS part of impact), not a reclassification that hides the failure from the Defects table."""
    if category in {"limitation", "test_defect", "tooling"}:
        return "Low"
    if category in {"unverified", "triage"}:
        return business_value(title)
    text = f"{failure_text}\n{assertion_text}"
    secondary = bool(_LOW_VALUE.search(title or "")) and not _HIGH_VALUE.search(title or "")
    if _URL_SIGNAL.search(text):
        severity = "Medium" if secondary else "High"
    elif _RENDER_SIGNAL.search(text):
        severity = "Critical" if _CORE_ELEMENT.search(f"{title}\n{assertion_text}") else "Medium"
    elif scope == "flow":
        severity = "Medium" if secondary else "High"
    else:
        severity = "Medium"
    if historically_reliable and severity in {"Critical", "High"}:
        return "Medium"
    return severity


def priority_for(category: str, severity: str) -> str:
    """Urgency follows severity; a limitation is never P1."""
    if category in {"limitation", "test_defect", "tooling"}:
        return "P3 - when convenient"
    if category == "triage":
        return "P2 - next fix cycle"          # a person looks first; it is never a release blocker on its own
    if severity in {"Critical", "High"}:
        return "P1 - before release"
    return "P2 - next fix cycle" if severity == "Medium" else "P3 - when convenient"


def priority_uniformity_note(records: list[dict[str, Any]]) -> str | None:
    """A note for the report when every scored item (Defects and Unverified journeys) has the same priority, else None.

    Severity must come from user impact, and one priority across the board COULD mean nothing was scored - but it is
    just as often true: three real defects can all be High. This used to refuse the whole report, which meant a run
    with a few findings of equal weight produced no report at all (found live). It is now said in the report, under
    "Validation warnings", for a person to confirm. Limitations, test defects and tooling issues have a priority fixed
    by the rules (always P3), so they cannot show scoring variety and are not part of this check."""
    scored = [r for r in records if r["category"] in {"defect", "unverified"}]
    if len(scored) >= 2 and len({r["priority"] for r in scored}) == 1:
        return (f"All {len(scored)} scored items (defects and unverified journeys) have the same priority ({scored[0]['priority']}). "
                "That can be right - they may truly share one impact - but check that each severity was judged from its own user impact.")
    return None


# -------------------------------------------------------------------------------------- release decision

REQUIREMENTS_BACKED_THRESHOLD = 60.0


def requirements_backed_percent(reqs: dict[str, Any] | None) -> float | None:
    """The share of PEOPLE's requirements (typed in, or taken from a document) backed by a passing result, or None
    when there are none. Requirements the model guessed by itself (source "ai") are shown in the report but never
    cap the verdict: a guess such as "I click Search and see that no results are shown" is the tool's idea of the
    site, not the site's promise, and failing to confirm it says nothing against the site."""
    reqs = reqs or {}
    if "written_total" in reqs:
        total, not_backed = reqs["written_total"], reqs["written_not_backed"]
    else:                                   # data written before the split: every requirement counted
        total, not_backed = reqs.get("total", 0), reqs.get("not_backed", 0)
    return round(100 * (total - not_backed) / total) if total else None


NO_VERDICT = "NO VERDICT"


def release_decision(records: list[dict[str, Any]], requirements_backed_percent: float | None = None,
                     threshold: float = REQUIREMENTS_BACKED_THRESHOLD, executed_tests: int | None = None) -> str:
    """Derived from the Defects table first. Any open High/Critical Defect is NO-GO; with none, any other open
    Defect is CONDITIONAL GO. Unverified journeys, failed checks that need triage, limitations, test defects and
    tooling issues never change the verdict: each is something the RUN could not establish, and a run's blind spot
    is not evidence against the site (found live: a production site could never be better than CONDITIONAL GO,
    because its map page and CAPTCHA form always left a few journeys unverified).

    C4: requirements coverage is a second, independent verdict input - a report may show zero open defects and
    still have most of its written requirements unbacked by any passing result, which an inert coverage number
    does not stop from reading as GO. Below the threshold, a would-be GO is capped at CONDITIONAL GO; it never
    overrides a verdict a Defect already made worse (NO-GO stays NO-GO)."""
    if executed_tests == 0:
        # Not one test ran (found live: every model call was refused, so nothing was generated): there is no evidence
        # for or against the site, and any GO-family word would be a claim this run cannot make.
        return NO_VERDICT
    open_defects = [r for r in records if r["category"] == "defect" and r["status"] == "Open"]
    if any(r["severity"] in {"Critical", "High"} for r in open_defects):
        return "NO-GO"
    if open_defects:
        return "CONDITIONAL GO"
    if requirements_backed_percent is not None and requirements_backed_percent < threshold:
        return "CONDITIONAL GO"
    return "GO"


def release_rule_text() -> str:
    """P5: the ONE statement of the release rule - S5 and S8 both render this, never their own restatement,
    so the two can never drift apart from each other."""
    return (
        "Any open High or Critical Defect is NO-GO. A defect's severity reflects user impact and is fixed when the "
        "defect is confirmed; a run that does not reproduce it changes neither its severity nor its open status. An open "
        "defect keeps its blocking status until a person closes it, which requires 3 consecutive clean runs or a "
        "deliberate close. With no open High or Critical Defect, any other open Defect is CONDITIONAL GO. What the run "
        "could not establish - unverified journeys, failed checks that need triage, limitations, test defects and tooling "
        "issues - is listed for a person to look at but never changes the verdict. With none of the above, a would-be GO "
        f"is capped at CONDITIONAL GO if fewer than {REQUIREMENTS_BACKED_THRESHOLD:.0f}% of the requirements people wrote "
        "(not ones the model guessed) are backed by a passing result (section 6). A run in which no test executed gets "
        "NO VERDICT: it has no evidence either way."
    )


def verdict_trigger(records: list[dict[str, Any]], requirements_backed_percent: float | None = None,
                    threshold: float = REQUIREMENTS_BACKED_THRESHOLD, executed_tests: int | None = None) -> str:
    """C2/C4: name what actually triggered a non-GO verdict, so nobody has to reverse-engineer it from a
    severity field or an inert coverage number - "NO-GO: 1 open defect not confirmed fixed (DEF-001)",
    not a bare "NO-GO"."""
    if executed_tests == 0:
        return "no test ran, so this run has no evidence either way"
    open_defects = [r for r in records if r["category"] == "defect" and r["status"] == "Open"]
    blocking = [r for r in open_defects if r["severity"] in {"Critical", "High"}]
    if blocking:
        ids = id_range([r["id"] for r in blocking])
        return f"{len(blocking)} open defect(s) not confirmed fixed ({ids})"
    if open_defects:
        ids = id_range([r["id"] for r in open_defects])
        return f"{len(open_defects)} open defect(s) not confirmed fixed ({ids})"
    if requirements_backed_percent is not None and requirements_backed_percent < threshold:
        return f"only {requirements_backed_percent:.0f}% of requirements backed by a passing result, below the {threshold:.0f}% threshold"
    return ""


def taxonomy_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {c: sum(r["category"] == c for r in records) for c in CATEGORIES}


def taxonomy_sentence(tax: dict[str, int]) -> str:
    return (f"{tax['defect']} application defect(s), {tax.get('triage', 0)} failed check(s) needing triage, {tax['unverified']} unverified journey(s), "
            f"{tax['limitation']} tooling limitation(s), {tax['test_defect']} test defect(s) and "
            f"{tax['tooling']} tooling issue(s) were recorded.")


def defect_headline(found: int, carried: int = 0) -> str:
    """C3: "found this run" and "carried from a previous run" are different claims - a ledger item that did
    not reproduce this run was not found this run, it failed to reproduce and remains open."""
    if found == 0 and carried == 0:
        return "0 application defects found this run. No application defect was confirmed by any test that ran."
    found_clause = f"{found} application defect{'s' if found != 1 else ''} found this run" if found else "0 application defects found this run"
    carried_clause = f"; {carried} open defect{'s' if carried != 1 else ''} carried from previous runs (not reproduced this run)." if carried else "."
    return found_clause + carried_clause


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
    triage, unverified = ids("triage"), ids("unverified")
    # one bullet for "a person should look", so the five-bullet limit still holds with the triage type added
    look = ([f"{len(triage)} failed check(s) whose cause is not known yet ({id_range(triage)})"] if triage else []) + \
           ([f"{len(unverified)} journey(s) the run could not confirm either way ({id_range(unverified)})"] if unverified else [])
    if look:
        fixed.append(f"A person should look at {' and '.join(look)}, section 5.")
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


def exit_criteria(records: list[dict[str, Any]], cov_ids: list[str], verdict: str, *,
                  requirements_backed_percent: float | None = None,
                  threshold: float = REQUIREMENTS_BACKED_THRESHOLD) -> list[str]:
    """What flips the CURRENT verdict to GO, and nothing else: only an open Defect or the requirements threshold can
    hold it back, so only those are listed. The things a person should still look at (triage, unverified journeys,
    limitations, tooling issues, coverage gaps) are in the conditions and in section 5, not presented as hurdles."""
    defects = [plain_id(r["id"]) for r in records if r["category"] == "defect" and r.get("status") == "Open"]

    out = []
    if verdict == NO_VERDICT:
        tooling = [plain_id(r["id"]) for r in records if r["category"] in {"tooling", "test_defect"}]
        out.append("Tests actually run: fix what stopped them" + (f" ({id_range(tooling)}, section 5)" if tooling else "")
                   + " and run again. Until then there is no verdict to improve on.")
    if defects:
        out.append(f"Every application defect ({id_range(defects)}) is fixed, retested and closed.")
    # P4: every clause capable of CAPPING the verdict must appear here, not just the ones that produced an
    # open record - all four other criteria above could be satisfied and GO would still be unreachable if
    # requirements coverage stays below the threshold, with nothing here saying so.
    if requirements_backed_percent is not None and requirements_backed_percent < threshold:
        out.append(f"At least {threshold:.0f}% of written requirements are backed by a passing result "
                  f"(currently {requirements_backed_percent:.0f}%).")
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


CATEGORY_ALIASES = {"defect": "application_defect", "triage": "failure_triage", "tooling": "test_harness",
                    "limitation": "site_owner_agreement", "unverified": "unverified_journey", "test_defect": "test_defect"}


def resolve_owner(cfg: dict[str, Any], record: dict[str, Any]) -> str | None:
    name, _source = resolve_owner_with_source(cfg, record)
    return name


def resolve_owner_with_source(cfg: dict[str, Any], record: dict[str, Any]) -> tuple[str | None, str]:
    """Same resolution as resolve_owner, but also says WHICH config key actually produced the name
    ("by_url"/"by_category"/"default") - without this, "everyone has the same owner" is indistinguishable
    from "by_category is fully configured but one person does all of it today", and a disclosure that
    assumes the former is simply wrong once the latter is true (confirmed live across repeated revisions)."""
    by_category = cfg.get("by_category") or {}
    # owners.json may use the internal category name ("tooling") or the human-readable alias
    # ("test_harness") the report shows people - whichever a person actually wrote.
    category_owner = by_category.get(record["category"]) or by_category.get(CATEGORY_ALIASES.get(record["category"], ""))
    for source, value in (("by_url", (cfg.get("by_url") or {}).get(record.get("url") or "")),
                         ("by_category", category_owner), ("default", cfg.get("default"))):
        if name := _real_name(value):
            return name, source
    return None, "unset"


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
        owner, source = resolve_owner_with_source(cfg, r)
        if owner is None:
            missing.append(plain_id(r["id"]))
            continue
        r["owner"] = owner
        r["owner_source"] = source
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
             tooling_urls: list[str], untested_files: list[str], env: dict[str, str], journeys_total: int = 0,
             requirements_total: int = 0, taxonomy: dict[str, int] | None = None) -> dict[str, Any]:
    return {"run_id": run_id, "finished_at": finished_at, "pass_rate_percent": counts.get("pass_rate_percent", 0),
            "tests_ran": counts.get("executed_tests", 0), "journeys_total": journeys_total,
            "requirements_total": requirements_total, "tests": tests, "pages": sorted(set(pages)),
            "tooling_urls": sorted(set(tooling_urls)), "untested_files": sorted(set(untested_files)), "env": env,
            "taxonomy": taxonomy or {}}


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


# ------------------------------------------------------------------------------------- defect ledger

LEDGER_FILE = "defect_ledger.json"
AUTO_CLOSE_AFTER_CLEAN_RUNS = 3


def load_ledger(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("defects"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "defects": {}}


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["version"] = 1
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _audit(entry: dict[str, Any], *, at: str, event: str, actor: str, reason: str) -> dict[str, Any]:
    """P1.2: every close or reopen - automatic or manual - gets ONE entry with the fields a reader needs to
    tell the two apart and verify the threshold was actually met: date, actor, reason, and the clean-run
    count at the moment of the event. Appended to the ledger entry's own audit_log (never erased) and
    returned so the caller (report.py) can also surface it in the review history and category-changes table."""
    record = {"at": at, "event": event, "actor": actor, "reason": reason,
             "clean_run_count": entry.get("consecutive_not_reproduced", 0)}
    entry.setdefault("audit_log", []).append(record)
    return record


def update_ledger(ledger: dict[str, Any], current: dict[str, dict[str, str]], run_id: str,
                  run_at: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Upsert this run's fresh defects into the ledger, then return "ghost" entries for every OPEN defect that
    did not reproduce this run - a defect is never silently dropped from the report just because one run
    didn't reproduce it; only an explicit close_defect() does that. A closed defect that reproduces again
    reopens automatically (its closure moves into closure_history, never erased) rather than staying silently
    closed while the underlying problem is back.

    Returns (ghosts, closed_this_run) - closed_this_run maps key -> the audit entry for a defect whose status
    changed to "closed" in THIS call (auto-close only; a manual close goes through close_defect), so the
    caller can render it in the same run it happened, not just leave it to be discovered on the next read."""
    entries = ledger.setdefault("defects", {})
    for key, rec in current.items():
        entry = entries.get(key)
        if entry is None:
            entry = {"first_seen_run": run_id, "first_seen_at": run_at, "total_failures": 0, "closure_history": []}
            entries[key] = entry
        if entry.get("status") == "closed":
            entry.setdefault("closure_history", []).append({
                "closed_by": entry.get("closed_by"), "closed_at": entry.get("closed_at"),
                "closed_reason": entry.get("closed_reason"), "reopened_at": run_at, "reopened_run": run_id})
            _audit(entry, at=run_at, event="reopened",
                  actor="[auto]" if str(entry.get("closed_by", "")).startswith("system") else f'[human {entry.get("closed_by")}, reopened automatically]',
                  reason="reproduced again")
        entry.update(title=rec["title"], url=rec["url"], scope=rec["scope"], flow_id=rec.get("flow_id", "NOT CAPTURED"),
                    last_failed_run=run_id, last_failed_at=run_at, consecutive_not_reproduced=0, status="open",
                    closed_by=None, closed_at=None, closed_reason=None)
        if not entry.get("severity"):
            # C2: severity is set ONCE, when a defect is first confirmed, from user impact - a run that
            # reproduces it again (or fails to) is never a reason to change it. Already-set severity (even
            # on a reopened, previously-closed entry) is left untouched here.
            entry["severity"] = rec.get("severity", "Medium")
            entry["severity_set_at"] = rec.get("severity_set_at") or run_at
            entry["severity_reason"] = rec.get("severity_reason") or rec.get("title", "")
        entry["total_failures"] = entry.get("total_failures", 0) + 1
    ghosts = []
    closed_this_run: dict[str, dict[str, Any]] = {}
    for key, entry in entries.items():
        if entry.get("status") != "open" or key in current:
            continue
        entry["consecutive_not_reproduced"] = entry.get("consecutive_not_reproduced", 0) + 1
        if entry["consecutive_not_reproduced"] >= AUTO_CLOSE_AFTER_CLEAN_RUNS:
            # The other half of "an open defect keeps its blocking status until a person closes it, which
            # requires 3 consecutive clean runs OR a deliberate close" - the deliberate path was the only one
            # implemented; without this, that sentence would describe a mechanism that doesn't exist.
            reason = f"not reproduced for {entry['consecutive_not_reproduced']} consecutive runs"
            entry.update(status="closed", closed_by="system (auto)", closed_at=run_at, closed_reason=reason)
            closed_this_run[key] = _audit(entry, at=run_at, event="closed", actor="[auto]", reason=reason)
            continue
        if not entry.get("severity"):
            # Migration fallback: a ledger entry written before severity tracking existed. Recorded now,
            # not backdated to when it was actually first confirmed - the report says so.
            entry["severity"] = impact_severity("defect", scope=entry.get("scope", "page"), title=entry.get("title", ""),
                                                failure_text=entry.get("title", ""), assertion_text="")
            entry["severity_set_at"] = run_at
            entry["severity_reason"] = f'{entry.get("title", "")} (recorded on migration to tracked severity; not necessarily when first confirmed)'
        ghosts.append({"key": key, **entry})
    return ghosts, closed_this_run


def close_defect(ledger: dict[str, Any], key: str, by: str, reason: str, at: str) -> bool:
    entry = ledger.get("defects", {}).get(key)
    if not entry or entry.get("status") == "closed":
        return False
    entry.update(status="closed", closed_by=by, closed_at=at, closed_reason=reason)
    _audit(entry, at=at, event="closed", actor=f"[human {by}]", reason=reason)
    return True


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


def compute_delta(prev: dict[str, Any] | None, cur: dict[str, Any], *, ledger_open_keys: frozenset[str] = frozenset(),
                  closed_this_run: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
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
    # A test that stays in both snapshots but changes quarantine status shrinks or grows tests_ran without
    # being "added" or "removed" - the reconciliation equation (report.py) needs this as its own term, or
    # prev_total - removed + added silently stops balancing the moment anything gets quarantined.
    newly_quarantined = sum(1 for k in common if ct[k].get("quarantined") and not pt[k].get("quarantined"))
    newly_unquarantined = sum(1 for k in common if pt[k].get("quarantined") and not ct[k].get("quarantined"))
    # Category drift (Unverified/Limitation/Tooling/Defect counts moving) with no stated cause reads as
    # arbitrary - record WHICH test moved and why, not just that the totals changed.
    category_changes = []
    for k in common:
        before, after = pt[k].get("category"), ct[k].get("category")
        if before == after:
            continue
        title = ct[k].get("title", k)
        # C1: the ledger is the single authority on whether a defect is still open - a test that passes
        # clean this run but whose ledger entry is still "open" (a ghost) did NOT resolve; it did not
        # reproduce. Nothing may call the same item both ledger-open and resolved in the same render.
        audit = (closed_this_run or {}).get(k)
        if before and not after and audit:
            # P1.2: a defect that closed THIS run - render the audit trail here, not a generic "resolved",
            # so the event that ended its ledger life is never silent.
            reason = (f'closed {audit["actor"]} on {audit["at"][:10]} - {audit["reason"]} '
                     f'(clean run count at close: {audit["clean_run_count"]})')
            after = None
        elif before and not after and k in ledger_open_keys:
            reason = "not reproduced this run - still open per the defect ledger, not resolved"
            after = before
        elif before and after:
            reason = f"reclassified from {before} to {after}"
        elif after:
            reason = f"newly classified as {after}"
        else:
            reason = f"resolved (was {before}, now passes clean)"
        category_changes.append({"id": k, "title": title, "from": before, "to": after, "reason": reason})
    return {
        "baseline": True,
        "previous_run_id": prev["run_id"], "previous_finished_at": prev.get("finished_at", ""),
        "previous_pass_rate_percent": prev.get("pass_rate_percent", 0), "previous_tests_ran": prev.get("tests_ran", 0),
        "previous_journeys_total": prev.get("journeys_total"), "previous_requirements_total": prev.get("requirements_total"),
        "added": [{"id": k, "title": ct[k].get("title", k)} for k in added_raw],
        "removed": removed,
        "quarantined_delta": newly_quarantined - newly_unquarantined,
        "quarantined_now": sum(1 for k in ct if ct[k].get("quarantined")),
        # tests whose failure is the test's own fault leave tests_ran without being added or removed, exactly
        # like quarantine; counted over both snapshots whole so renames, additions and removals all balance
        "test_defect_delta": sum(1 for k in ct if ct[k].get("invalid")) - sum(1 for k in pt if pt[k].get("invalid")),
        "category_changes": category_changes,
        # C1: a test the ledger still calls open did not pass for good - it is a ghost (category_changes already
        # says "not reproduced"), never counted here as "now passing" alongside an Executive Summary that
        # simultaneously says the same defect is still open.
        "fixed": [{"id": k, "title": ct[k].get("title", k)} for k in common
                 if _failing(pt[k].get("status")) and ct[k].get("status") == "passed" and k not in ledger_open_keys],
        "still_failing": [{"id": k, "title": ct[k].get("title", k)} for k in common if _failing(pt[k].get("status")) and _failing(ct[k].get("status"))],
        # common, not ct: a brand-new test (no counterpart in the previous run) failing on its first appearance
        # is not a regression of something that used to pass - conflating the two made "new_failures" count a
        # test that was simply added and happened to fail, which is a different, separate fact ("added") from
        # "this broke since last time" ("new_failures" is meant to mean).
        "new_failures": [{"id": k, "title": ct[k].get("title", k)} for k in common if _failing(ct[k].get("status")) and not _failing(pt[k].get("status"))],
        "renamed_count": len(renamed),
        "environment_changes": environment_changes(prev.get("env", {}), cur.get("env", {})),
        # P2: so a pre-render check can assert a category count never moves without a category_changes row -
        # the absence of that table in a run where a category count truly changed is the exact bug it exists
        # to catch. None (not {}) when the previous run predates taxonomy tracking - a missing baseline is
        # not the same claim as "every category was genuinely at zero", and must not read as one.
        "previous_taxonomy": prev.get("taxonomy"),
    }


# ---------------------------------------------------------------------------------- evidence and ID checks

def evidence_reason(category: str, failure_class: str, reason_detail: str = "") -> str:
    if failure_class == "not_reproduced":
        # D4: this record's check passed this run (that is what "not reproduced" means) - it did not stop
        # before an evidence step, so the generic Defect fallback below is factually wrong for it. Evidence
        # from the failure that first confirmed it is not persisted across runs, so there is no link to offer.
        return "No failure evidence this run - the check passed. Evidence from the last failure: not retained."
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
