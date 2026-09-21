"""The flow documentation must not drift from the code: every command, stage, status and heuristics key it names
has to exist, and its example config has to load cleanly. When one of these fails, either the docs or the code
changed without the other."""
import json
import re
from pathlib import Path

import pytest

from website_test_pipeline import heuristics, intents, pipeline
from website_test_pipeline.review import SUBCOMMANDS

ROOT = Path(__file__).resolve().parent.parent
DOCS = {name: (ROOT / name).read_text(encoding="utf-8") for name in ("README.md", "docs/FLOWS.md")}
FLOWS = DOCS["docs/FLOWS.md"]


def _cli_commands() -> set[str]:
    source = (ROOT / "website_test_pipeline" / "cli.py").read_text(encoding="utf-8")
    return set(re.findall(r"'([a-z]+)'", re.search(r"choices=\[([^\]]*)\]", source).group(1)))


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


@pytest.mark.parametrize("name", list(DOCS))
def test_every_command_the_docs_run_exists(name):
    used = set(re.findall(r"website_test_pipeline\.cli ([a-z][\w-]*)", DOCS[name]))
    assert used, f"{name} shows no commands"
    assert used <= _cli_commands(), sorted(used - _cli_commands())


@pytest.mark.parametrize("name", list(DOCS))
def test_every_flows_subcommand_the_docs_name_exists(name):
    used = set(re.findall(r"cli flows ([a-z]+)", DOCS[name])) | set(re.findall(r"`flows ([a-z]+)", DOCS[name]))
    assert used <= set(SUBCOMMANDS), sorted(used - set(SUBCOMMANDS))


def test_every_command_in_the_reference_table_exists():
    table = _section(FLOWS, "## 3. Commands", "### Exit codes")
    named = set(re.findall(r"^\| `([a-z]+)", table, re.M))
    assert named <= _cli_commands() | {"flows"}, sorted(named)
    assert {"intents", "expand", "verify", "flowgen", "execute", "report"} <= named          # nothing important is undocumented


def test_the_stages_the_docs_list_are_the_stages_the_chain_runs():
    row = next(line for line in FLOWS.splitlines() if line.startswith("| `flows run"))
    assert set(re.findall(r"`([a-z]+)`", row.split("stages are", 1)[1])) == set(pipeline.STAGES)
    assert set(pipeline.STAGES) <= set(re.findall(r"^\| `([a-z]+)` \|", _section(FLOWS, "## 2. The stages", "`propose` is"), re.M))


def test_the_heuristics_table_lists_exactly_the_keys_the_code_has():
    table = _section(FLOWS, "### `heuristics.json`", "```json")
    assert set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M)) == set(heuristics.DEFAULTS)


def test_the_documented_heuristics_example_loads_without_a_single_warning(tmp_path):
    example = _section(FLOWS, "### `heuristics.json`", "## 5.").split("```json", 1)[1].split("```", 1)[0]
    path = tmp_path / "heuristics.json"
    path.write_text(example, encoding="utf-8")
    data = json.loads(example)
    assert set(data) <= set(heuristics.DEFAULTS) | {"replace"}
    assert heuristics.configure(path) == []
    heuristics.configure(None)


def test_the_statuses_the_docs_describe_are_the_ones_the_code_uses():
    flow_table = _section(FLOWS, "**Flow status**", "- One failed run")
    assert set(re.findall(r"^\| `([a-z]+)` \|", flow_table, re.M)) == {"candidate", "verified", "stale", "approved", "rejected"}
    paragraph = FLOWS.split("**Intent status**", 1)[1].split(chr(10) + chr(10), 1)[0]      # it wraps over several lines
    assert set(re.findall(r"`([a-z]+)`", paragraph)) == intents.STATUSES


def test_the_files_the_docs_point_to_exist():
    for name, text in DOCS.items():
        for target in re.findall(r"\]\((docs/[\w./-]+)\)", text):
            assert (ROOT / target).exists(), f"{name} links to missing {target}"
    assert "docs/FLOWS.md" in DOCS["README.md"]                                     # the README points at the reference
