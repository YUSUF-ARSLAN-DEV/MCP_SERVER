"""Execution-failure repair loop.

The generator already has a feedback loop for *validator* rejections. This is the
same idea one layer out: after the specs are actually run, feed each failing
page's pytest errors back to the model and regenerate that one module. `report
--repair` runs this between the first and second pytest pass.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

from .generator import generate_spec
from .models import PageInventory

_FEEDBACK_HEAD = (
    "The tests you generated for this page were RUN against the live page and FAILED. "
    "Rewrite the whole module so every test passes. Fix the specific failures listed "
    "below - correct the locator or the assertion. If a test asserts something that is "
    "genuinely not true of this page, replace it with a correct check or drop that test. "
    "Keep every test wrapped in action_evidence / observation_evidence.\n\nFAILURES:\n"
)


def _inv_slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:100]


def failing_by_url(artifacts_dir: Path) -> dict[str, list[str]]:
    """{url: ["<test>: <trimmed error>", ...]} for failed / errored rows."""
    path = artifacts_dir / "test_results.json"
    if not path.exists():
        return {}
    results = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for row in results.get("tests", []):
        if row.get("status") not in {"failed", "error"}:
            continue
        url = row.get("url")
        if not url:
            continue
        lines = [l.strip() for l in (row.get("error") or "").splitlines() if l.strip()]
        tail = " ".join(lines[-4:]) if lines else "(no message captured)"
        title = row.get("title") or row.get("nodeid") or "test"
        out.setdefault(url, []).append(f"- {title}: {tail[:400]}")
    return out


def _inventory_for(artifacts_dir: Path, url: str) -> PageInventory | None:
    path = artifacts_dir / f"{_inv_slug(url)}.inventory.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fields = PageInventory.__dataclass_fields__
    return PageInventory(**{k: v for k, v in data.items() if k in fields})


def repair_failures(artifacts_dir: Path, tests_dir: Path, manifest: dict,
                    client, guide: str, persona: str, log) -> int:
    """Regenerate every page that had a failing test. Returns the count attempted."""
    failing = failing_by_url(artifacts_dir)
    if not failing:
        log.info("failrepair: no failing tests")
        return 0
    specs = {u: (i or {}).get("spec") for u, i in (manifest.get("urls") or {}).items()}
    attempted = 0
    for url, msgs in failing.items():
        inventory = _inventory_for(artifacts_dir, url)
        if inventory is None:
            log.warning("failrepair: no inventory for %s - cannot repair", url)
            continue
        spec_path = Path(specs.get(url) or (tests_dir / f"{_inv_slug(url)}_test.py"))
        feedback = _FEEDBACK_HEAD + "\n".join(msgs[:6])
        log.info("failrepair: regenerating %s (%d failing test(s))", url, len(msgs))
        try:
            generate_spec(client, guide, persona, inventory, spec_path,
                          attempts=3, log=log, seed_feedback=feedback)
            attempted += 1
        except Exception as exc:  # skip stub already written by generate_spec
            log.warning("failrepair: %s not repaired (%s)", url, str(exc).splitlines()[0][:200])
    return attempted
