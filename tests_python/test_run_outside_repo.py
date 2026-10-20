"""A run whose folder is outside the repo (a server keeping runs in DATA_DIR) must still get the pipeline's shared pytest
fixtures - and a run whose tests could not start must say so instead of "finishing" with no report.

Found live: a hosted run spent five minutes, every one of 76 tests failed at setup with `fixture 'evidence_dir' not found`,
no results file was written, the report step crashed, and the server called the run "done"."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from website_test_pipeline import cli, config
from website_test_pipeline.pipeline import conftest_args

ROOT = Path(config.ROOT)
SPEC = "def test_uses_the_pipelines_fixture(evidence_dir):\n    assert evidence_dir.is_dir()\n"


def test_tests_inside_the_repo_need_nothing_extra_and_tests_outside_load_the_shared_fixtures_explicitly(tmp_path):
    assert conftest_args(SimpleNamespace(tests_dir=ROOT / "runs" / "site" / "tests", root=ROOT)) == []
    assert conftest_args(SimpleNamespace(tests_dir=tmp_path / "runs" / "site" / "tests", root=ROOT)) == ["-p", "conftest"]


def _run_pytest(tests_dir: Path, artifacts: Path, extra: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "WTP_ARTIFACTS": str(artifacts)}
    return subprocess.run([sys.executable, "-m", "pytest", str(tests_dir), "-q", "-p", "no:cacheprovider", *extra],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def outside(tmp_path):
    tests, artifacts = tmp_path / "runs" / "site" / "tests", tmp_path / "runs" / "site" / "artifacts"
    tests.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    (tests / "sample_test.py").write_text(SPEC, encoding="utf-8")
    return tests, artifacts


def test_the_bug_without_the_fix_every_test_errors_at_setup_and_no_results_are_written(outside):
    tests, artifacts = outside
    result = _run_pytest(tests, artifacts, [])
    assert "fixture 'evidence_dir' not found" in result.stdout + result.stderr
    assert result.returncode != 0 and not (artifacts / "test_results.json").exists()


def test_the_fix_with_the_shared_fixtures_loaded_the_tests_run_and_results_are_recorded(outside):
    tests, artifacts = outside
    settings = SimpleNamespace(tests_dir=tests, root=ROOT)
    result = _run_pytest(tests, artifacts, conftest_args(settings))
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = json.loads((artifacts / "test_results.json").read_text(encoding="utf-8"))
    assert [t["status"] for t in recorded["tests"]] == ["passed"]
    assert (artifacts / "evidence").is_dir()


def test_a_rerun_whose_tests_wrote_no_results_stops_with_a_clear_exit_code_instead_of_crashing_the_report(tmp_path, monkeypatch, caplog):
    runs = tmp_path / "runs"
    monkeypatch.setattr(config, "RUNS", runs)
    monkeypatch.setenv("SITE", "nores")
    monkeypatch.setenv("WTP_JOB", "1")
    monkeypatch.setattr(sys, "argv", ["cli", "report", "--rerun"])
    artifacts = runs / "nores" / "artifacts"
    artifacts.mkdir(parents=True)
    (runs / "nores" / "urls.txt").write_text("https://example.test/", encoding="utf-8")
    stale = artifacts / "test_results.json"
    stale.write_text(json.dumps({"tests": [{"nodeid": "old::test", "status": "passed"}]}), encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1)              # pytest "ran" and wrote nothing

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    with caplog.at_level("ERROR"):
        code = cli.main()
    assert code == 2                                            # "a stage could not run": `all` stops, the API marks the run failed
    assert "wrote no results" in caplog.text
    assert not stale.exists()                                   # the earlier run's file was cleared, never reported by mistake
    assert not (artifacts / "report").exists()                  # and no report was built from nothing
    assert any("pytest" in part for part in calls[0])


def test_the_all_in_one_pipeline_builds_the_combined_word_report(tmp_path, monkeypatch):
    """Found on the first real run from the web page: its Downloads held per-page documents but no full-report.docx,
    because `all` ran `report --rerun` without --combined."""
    monkeypatch.setattr(config, "RUNS", tmp_path / "runs")
    monkeypatch.setenv("SITE", "allsteps")
    monkeypatch.setenv("SEED_URL", "https://example.test/")
    monkeypatch.setenv("WTP_JOB", "1")
    monkeypatch.setattr(sys, "argv", ["cli", "all", "--no-window"])
    commands = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert cli.main() == 0
    steps = [c[3:] for c in commands if c[1:3] == ["-m", "website_test_pipeline.cli"]]
    assert [s[0] for s in steps] == ["crawl", "generate", "flows", "report"]
    assert steps[-1][:3] == ["report", "--rerun", "--combined"]


def test_parallel_tests_are_only_asked_for_when_pytest_xdist_is_installed(monkeypatch):
    """A container built from the production requirements once lacked pytest-xdist, so the default of four workers made
    pytest fail on "-n" for every run. Without xdist the tests simply run in one process."""
    import importlib.util
    from website_test_pipeline.pipeline import xdist_args
    monkeypatch.delenv("EXECUTE_CONCURRENCY", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "xdist" else object())
    assert xdist_args() == []                                           # not installed: no "-n"
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert xdist_args() == ["-n", "4"]                                  # installed: the default of four
    monkeypatch.setenv("EXECUTE_CONCURRENCY", "2")
    assert xdist_args() == ["-n", "2"]
    for single in ("1", "0", ""):
        monkeypatch.setenv("EXECUTE_CONCURRENCY", single)
        assert xdist_args() == []                                       # the single-process setting is respected either way
