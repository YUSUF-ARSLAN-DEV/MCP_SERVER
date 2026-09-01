from pathlib import Path
import re
from .models import PageInventory
from .validator import validate_python_spec
from .llm import ModelError

SYSTEM = "Return exactly one complete Python pytest code block and no prose. Generate only tests supported by the observed page inventory."

EVIDENCE_RULES = (
    "EVERY test function MUST capture evidence, because the run produces a Word report a human uses to verify results:\n"
    "- import: from website_test_pipeline.evidence import action_evidence, observation_evidence\n"
    "- use the 'evidence_dir' fixture (a pathlib.Path); do NOT use tmp_path and do NOT open your own browser.\n"
    "- read-only test: call observation_evidence(page, label, verify, evidence_dir) at least once, where verify asserts what is on screen.\n"
    "- every click/fill/select/check/press: wrap it as action_evidence(page, label, action, verify, evidence_dir); "
    "verify must assert the post-action state (e.g. expect(field).to_have_value(...)) before the screenshot is taken.\n"
    "- 'label' is a short kebab-case step name like '01-form-visible' or '02-name-filled'."
)

SCOPE_RULES = (
    "Write 3 to 8 test functions, no more. Cover the highest-value user goals from the persona that this page supports "
    "(finding frequencies, tuning, subscribing, language switch, primary navigation).\n"
    "One test = one user goal. Group only checks that belong to the SAME goal (e.g. all fields of one form). "
    "Keep navigation, search, and each distinct form as their own separate tests - do not fold unrelated checks into "
    "one big test, and never write one test per link, per heading, or per footer item.\n"
    "If a control opens a menu, panel, or dropdown whose contents are NOT in the CONTROLS list, only assert that the "
    "trigger button is visible and enabled - never click it and assert on guessed elements (no '# assuming ...' selectors)."
)

LOCATOR_RULES = (
    "For a <select> / combobox, ALWAYS locate it by its id or selector token from CONTROLS "
    "(e.g. page.locator('#countrylist')). NEVER use get_by_label or get_by_role(name=...) for a select - "
    "its accessible name is often the concatenation of every option and will not match.\n"
    "For any nav or footer link, scope to the landmark AND append .first, e.g. "
    "page.get_by_role('navigation').get_by_role('link', name='News', exact=True).first - "
    "pages routinely render the primary nav twice (desktop + mobile/mega-menu) so even a landmark-scoped "
    "locator can match two elements. Do the same for anything marked AMBIGUOUS in CONTROLS.\n"
    "Only use roles that are real ARIA roles (button, link, heading, textbox, combobox, checkbox, navigation, "
    "banner, contentinfo, list, listitem, dialog, region, ...). There is no 'video' role - a <video> element "
    "has no role; locate a media player by an id/selector or a nearby button you actually observed."
)

ASSERTION_RULES = (
    "URL assertions: sites redirect, add a trailing slash, a locale segment, or tracking params, and "
    "to_have_url() does NOT accept a glob. To check a navigation landed, use "
    "expect(page).to_have_url(re.compile(r'/section')) (add 'import re' at the top) - a partial regex on the "
    "path. page.wait_for_url('**/section**') also works but only OUTSIDE a verify callback. "
    "Never pass an exact URL string or a '**' glob to to_have_url(). "
    "To confirm the page under test loaded, assert its main heading is visible - not its URL.\n"
    "Never assert a state that the action you just performed changes or removes: a submit button that becomes "
    "disabled or relabelled after click, an overlay that _open() already dismissed, a field that clears on submit. "
    "Verify the genuine post-state (a value you set, a panel that appeared, a URL glob).\n"
    "Never assert exact copy you did not observe (no guessed success/error messages). Do not assert the exact "
    "text of individual article/news headlines or any element inside a feed, list, or article region - that "
    "content rotates between crawl and run. Assert only stable structural headings and labels.\n"
    "Never assert on page.locator('div'/'span'/'a'/'p') with no id, attribute, or role qualifier - it matches "
    "hundreds of nodes."
)

def _compact_controls(controls: list[dict], limit: int = 70) -> str:
    lines: list[str] = []
    external = 0
    for control in controls:
        href = str(control.get("href") or "")
        if href.startswith("http"):
            external += 1
            if external > 10:
                continue
        parts: list[str] = [str(control.get("tag") or "?")]
        name = (control.get("name") or "").strip()
        if name:
            parts.append(f'"{name[:80]}"')
        for key in ("selector", "id", "testid", "field_name"):
            if control.get(key):
                parts.append(f"{key}={control[key]}")
                break
        if href:
            parts.append(f"href={href}")
        if control.get("type"):
            parts.append(f"type={control['type']}")
        if control.get("options"):
            parts.append(f"options={list(control['options'])[:15]}")
        for flag in ("required", "disabled"):
            if control.get(flag):
                parts.append(flag)
        if control.get("ambiguous"):
            parts.append("AMBIGUOUS(name seen on >1 element - scope to a landmark and/or append .first)")
        if control.get("checked") is not None:
            parts.append(f"checked={control['checked']}")
        lines.append(" ".join(parts))
        if len(lines) >= limit:
            break
    return "\n".join(lines)

def _compact_headings(headings: list[dict]) -> str:
    return "\n".join(
        f"{h.get('level') or 'H?'}: {(h.get('text') or '').strip()[:120]}"
        for h in headings if (h.get("text") or "").strip()
    )

def prompt_for(guide: str, persona: str, inventory: PageInventory, feedback: str = "") -> str:
    correction = f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED: {feedback}\nFix exactly that problem and return a corrected module." if feedback else ""
    return (
        f"{guide}\n\nPERSONA:\n{persona}\n\n"
        f"PAGE URL: {inventory.url}\nPAGE TITLE: {inventory.title}\n\n"
        f"HEADINGS:\n{_compact_headings(inventory.headings)}\n\n"
        f"CONTROLS (one per line; use the id/selector token verbatim when present, else get_by_role with the quoted name; never invent a selector):\n{_compact_controls(inventory.controls)}\n\n"
        f"FORMS:\n{inventory.forms}\n\n"
        f"ACCESSIBILITY SIGNALS:\n{inventory.accessibility[:6000]}\n\n"
        "Generate dynamic, page-specific smoke tests as one pytest module. Only test controls that appear above; skip any marked disabled. "
        "Respect required/checked state and use only the listed select options. Use the pytest-playwright 'page' fixture. Never invent controls or outcomes.\n"
        "Start every test with _open(page); define _open as a one-liner: open_page(page, URL) "
        "(from website_test_pipeline.pageutils) - it navigates, waits for load, and clears consent dialogs and ad interstitials.\n"
        "Pass exact=True to every get_by_role(name=...); scope nav-link checks to get_by_role('navigation').first "
        "and footer checks to get_by_role('contentinfo').first.\n\n"
        f"{SCOPE_RULES}\n\n{LOCATOR_RULES}\n\n{ASSERTION_RULES}\n\n{EVIDENCE_RULES}{correction}"
    )

def extract_code(raw: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
    if not match: raise ValueError("model did not return a Python code block")
    return match.group(1).strip() + "\n"

def _skip_stub(inventory: PageInventory, reason: str) -> str:
    safe = re.sub(r"\s+", " ", reason).strip().replace('"', "'")[:200]
    return (
        "import pytest\n\n"
        f'URL = "{inventory.url}"\n\n'
        f'@pytest.mark.skip(reason="NOT TESTABLE: no spec passed validation - {safe}")\n'
        "def test_page_not_testable():\n"
        "    pass\n"
    )

def generate_spec(client, guide: str, persona: str, inventory: PageInventory, output: Path, attempts: int = 5) -> None:
    last = None
    feedback = ""
    for _ in range(attempts):
        try:
            code = extract_code(client.generate(prompt_for(guide, persona, inventory, feedback), SYSTEM)); validate_python_spec(code, inventory.url, inventory); output.write_text(code, encoding='utf-8'); return
        except ModelError as exc:
            last = exc
            if exc.status in {401, 403, 429, 500, 502, 503, 504, 524, 530}:
                break
        except Exception as exc: last = exc; feedback = str(exc)
    if isinstance(last, ModelError): raise last
    # The model never produced a valid spec for this page - ship a skipped module
    # so the page is still recorded and `execute` stays green instead of crashing.
    output.write_text(_skip_stub(inventory, str(last)), encoding="utf-8")
    raise RuntimeError(f"spec rejected after {attempts} attempts, wrote skip stub: {last}")
