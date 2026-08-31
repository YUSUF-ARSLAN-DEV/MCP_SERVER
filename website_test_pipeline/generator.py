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

def prompt_for(guide: str, persona: str, inventory: PageInventory, feedback: str = "") -> str:
    correction = f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED: {feedback}\nFix exactly that problem and return a corrected module." if feedback else ""
    return f"{guide}\n\nPERSONA:\n{persona}\n\nPAGE URL: {inventory.url}\nPAGE TITLE: {inventory.title}\n\nHEADINGS:\n{inventory.headings}\n\nCONTROLS (use the 'selector' field verbatim when present; otherwise use get_by_role with 'name'; never invent a selector):\n{inventory.controls}\n\nFORMS:\n{inventory.forms}\n\nACCESSIBILITY SIGNALS:\n{inventory.accessibility}\n\nGenerate dynamic, page-specific smoke tests as one pytest module. Only test controls that appear above; skip any marked disabled. Respect required/checked state and use only the listed select options. Use the pytest-playwright 'page' fixture. Never invent controls or outcomes.\n\n{EVIDENCE_RULES}{correction}"

def extract_code(raw: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
    if not match: raise ValueError("model did not return a Python code block")
    return match.group(1).strip() + "\n"

def generate_spec(client, guide: str, persona: str, inventory: PageInventory, output: Path, attempts: int = 2) -> None:
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
