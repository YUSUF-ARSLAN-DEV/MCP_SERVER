from pathlib import Path
import re
from .models import PageInventory
from .validator import validate_python_spec
from .llm import ModelError

SYSTEM = "Return exactly one complete Python pytest code block and no prose. Generate only tests supported by the observed page inventory."

def prompt_for(guide: str, persona: str, inventory: PageInventory) -> str:
    return f"{guide}\n\nPERSONA:\n{persona}\n\nPAGE URL: {inventory.url}\nPAGE TITLE: {inventory.title}\n\nOBSERVED INVENTORY:\n{inventory.headings}\n{inventory.controls}\n\nACCESSIBILITY SIGNALS:\n{inventory.accessibility}\n\nGenerate dynamic, page-specific smoke tests as one pytest module. Use the pytest-playwright 'page' fixture and 'tmp_path'; do not open your own browser. Every state-changing action must use action_evidence(page, label, action, verify, tmp_path) and assert the post-state before capturing evidence. Use pytest and Playwright Python; never invent controls or outcomes."

def extract_code(raw: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
    if not match: raise ValueError("model did not return a Python code block")
    return match.group(1).strip() + "\n"

def generate_spec(client, guide: str, persona: str, inventory: PageInventory, output: Path, attempts: int = 2) -> None:
    last = None
    for _ in range(attempts):
        try:
            code = extract_code(client.generate(prompt_for(guide, persona, inventory), SYSTEM)); validate_python_spec(code, inventory.url); output.write_text(code, encoding='utf-8'); return
        except ModelError as exc:
            last = exc
            if exc.status in {401, 403, 429, 500, 502, 503, 504, 524, 530}:
                break
        except Exception as exc: last = exc
    raise RuntimeError(f"spec rejected: {last}")
