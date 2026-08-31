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
    "its accessible name is often the concatenation of every option and will not match."
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
        "Start every test with _open(page), and define _open to call dismiss_overlays(page) "
        "(from website_test_pipeline.pageutils) right after page.goto - consent dialogs and ad interstitials block clicks and visibility otherwise.\n"
        "Pass exact=True to every get_by_role(name=...); scope nav-link checks to get_by_role('navigation') and footer checks to get_by_role('contentinfo').\n\n"
        f"{SCOPE_RULES}\n\n{LOCATOR_RULES}\n\n{EVIDENCE_RULES}{correction}"
    )

def extract_code(raw: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
    if not match: raise ValueError("model did not return a Python code block")
    return match.group(1).strip() + "\n"

def generate_spec(client, guide: str, persona: str, inventory: PageInventory, output: Path, attempts: int = 3) -> None:
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
    raise RuntimeError(f"spec rejected: {last}")
