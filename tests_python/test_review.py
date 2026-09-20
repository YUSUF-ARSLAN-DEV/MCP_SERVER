import json
from types import SimpleNamespace

import pytest

from website_test_pipeline.flowgen import MARKER, emit_flow_spec, file_name, run_flowgen
from website_test_pipeline.ratings import derive_status
from website_test_pipeline.review import (
    ReviewError, decide, find_flow, last_result, render_list, render_show, run_flows_command, safe_print,
)


def _flow(flow_id, status="verified", goal="Search by country"):
    return {"id": flow_id, "goal": goal, "status": status, "source": "explorer", "start_url": "https://x.test/en",
            "steps": [{"kind": "click", "selector": None, "name": "Search"}],
            "outcome": {"effect": "navigates", "to": "/en/find"},
            "observed": {"effect": "navigates", "url": "https://x.test/en/find", "new_headings": ["Find"],
                         "step_urls": ["https://x.test/en/find"], "step_effects": ["navigates"]},
            "last_run_at": "2026-09-20T10:00:00+00:00"}


def _doc(*flows):
    return {"version": 1, "flows": list(flows)}


def _ratings(**by_id):
    return {"version": 1, "ratings": dict(by_id)}


A, B = "x-test-en--search", "x-test-en--subscribe"


# ------------------------------------------------------------------ finding

def test_find_flow_by_exact_id_or_unique_fragment():
    doc = _doc(_flow(A), _flow(B))
    assert find_flow(doc, A)["id"] == A
    assert find_flow(doc, "subscribe")["id"] == B


def test_find_flow_explains_unknown_and_ambiguous_references():
    doc = _doc(_flow(A), _flow(B))
    with pytest.raises(ReviewError, match="no flow matches"):
        find_flow(doc, "nope")
    with pytest.raises(ReviewError, match="matches 2 flows") as err:
        find_flow(doc, "x-test")
    assert A in str(err.value) and B in str(err.value)
    with pytest.raises(ReviewError, match="give a flow id"):
        find_flow(doc, "  ")


def test_an_exact_id_wins_over_longer_ids_that_contain_it():
    doc = _doc(_flow("a--b"), _flow("a--b-c"))
    assert find_flow(doc, "a--b")["id"] == "a--b"


# ------------------------------------------------------------------ decisions

def test_approve_sets_the_status_and_records_who_when_and_why():
    doc, ratings = _doc(_flow(A)), _ratings()
    decide(doc, ratings, A, "approve", "matches what QA expects", "sam", "t1")
    assert doc["flows"][0]["status"] == "approved"
    assert ratings["ratings"][A] == [{"source": "human", "at": "t1", "by": "sam", "decision": "approved",
                                      "reason": "matches what QA expects"}]


def test_reject_needs_a_reason_and_changes_nothing_without_one():
    doc, ratings = _doc(_flow(A)), _ratings()
    with pytest.raises(ReviewError, match="needs a reason"):
        decide(doc, ratings, A, "reject", "   ")
    assert doc["flows"][0]["status"] == "verified" and ratings["ratings"] == {}
    decide(doc, ratings, A, "reject", "checkout needs an account", "sam", "t1")
    assert doc["flows"][0]["status"] == "rejected"


def test_a_human_decision_survives_failing_runs_and_reruns():
    doc, ratings = _doc(_flow(A)), _ratings()
    decide(doc, ratings, A, "approve", "", "sam", "t1")
    failing = [{"source": "runner", "passed": False}] * 3
    assert derive_status(doc["flows"][0]["status"], failing) == "approved"


def test_reset_hands_the_flow_back_to_the_tool_which_rederives_it():
    passing, failing = {"source": "runner", "passed": True}, {"source": "runner", "passed": False}
    doc, ratings = _doc(_flow(A, "rejected"), _flow(B, "approved")), _ratings(**{A: [passing], B: [failing]})
    decide(doc, ratings, A, "reset", "", "sam", "t1")
    decide(doc, ratings, B, "reset", "", "sam", "t1")
    assert doc["flows"][0]["status"] == "verified"      # its latest run passed
    assert doc["flows"][1]["status"] == "candidate"     # its latest run failed
    assert ratings["ratings"][A][-1]["decision"] == "reset"


def test_unknown_action_is_refused():
    with pytest.raises(ReviewError, match="unknown action"):
        decide(_doc(_flow(A)), _ratings(), A, "explode")


# ------------------------------------------------------------------ rendering

def test_last_result_reads_the_latest_real_execution_not_opinions():
    entries = [{"source": "runner", "passed": True}, {"source": "pytest", "passed": False},
               {"source": "model", "scores": {}}, {"source": "human", "decision": "approved"}]
    assert last_result(entries) == "pytest FAIL"
    assert last_result([{"source": "model"}]) == "not run"


def test_list_shows_every_flow_with_status_last_run_and_a_count():
    doc = _doc(_flow(A), _flow(B, "rejected", "Subscribe"))
    text = render_list(doc, _ratings(**{A: [{"source": "runner", "passed": True}]}))
    assert A in text and B in text and "runner pass" in text and "not run" in text
    assert "1 rejected, 1 verified" in text
    assert "1 rejected" in render_list(doc, _ratings(), status="rejected") and A not in render_list(doc, _ratings(), status="rejected")
    assert render_list(doc, _ratings(), status="stale") == "no flows with status stale"


def test_show_lists_steps_expected_observed_and_the_whole_history():
    entries = [{"source": "model", "at": "2026-09-20T08:00:00", "prompt_version": "critic-v1",
                "scores": {"coherence": 5, "importance": 4, "outcome_strength": 3}, "reason": "clear journey"},
               {"source": "runner", "at": "2026-09-20T09:00:00", "passed": True,
                "checks": {"steps_completed": "1/1", "observed_effect": "navigates"}},
               {"source": "pytest", "at": "2026-09-20T09:30:00", "passed": False, "error": "url did not match"},
               {"source": "human", "at": "2026-09-20T10:00:00", "by": "sam", "decision": "approved", "reason": "ok"}]
    text = render_show(_flow(A), entries)
    for part in ("1. click \"Search\"", "expected: navigates -> /en/find", "observed: navigates at https://x.test/en/find",
                 "new headings: Find", "[model critic-v1] coherence 5", "[runner] pass steps 1/1",
                 "[pytest] FAIL - url did not match", "[human sam] approved - ok"):
        assert part in text, part


def test_show_says_when_a_flow_was_never_run():
    flow = _flow(A)
    del flow["observed"]
    assert "never run" in render_show(flow, [])


# ------------------------------------------------------------------ the command

def _settings(tmp_path, flows):
    path = tmp_path / "flows.json"
    path.write_text(json.dumps(_doc(*flows)), encoding="utf-8")
    return SimpleNamespace(flows_file=path, ratings_file=tmp_path / "flow_ratings.json")


def _run(settings, *words, **kw):
    lines: list[str] = []
    code = run_flows_command(settings, list(words), out=lines.append, **kw)
    return code, "\n".join(lines)


def test_command_round_trip_persists_status_and_history(tmp_path):
    settings = _settings(tmp_path, [_flow(A), _flow(B)])
    assert _run(settings, "approve", "search", reason="looks right")[0] == 0
    code, text = _run(settings, "reject", B, reason="needs a login")
    assert code == 0 and text == f"{B} -> rejected"
    flows = {f["id"]: f["status"] for f in json.loads(settings.flows_file.read_text(encoding="utf-8"))["flows"]}
    assert flows == {A: "approved", B: "rejected"}
    history = json.loads(settings.ratings_file.read_text(encoding="utf-8"))["ratings"]
    assert history[A][0]["decision"] == "approved" and history[B][0]["reason"] == "needs a login"
    assert "approved" in _run(settings, "list")[1] and "needs a login" in _run(settings, "show", B)[1]


def test_command_defaults_to_list_and_reports_problems_with_exit_codes(tmp_path):
    settings = _settings(tmp_path, [_flow(A)])
    assert _run(settings)[0] == 0
    code, text = _run(settings, "reject", A)
    assert code == 2 and "needs a reason" in text
    assert _run(settings, "show", "zzz")[0] == 2
    assert _run(settings, "frobnicate")[0] == 2
    settings.flows_file.write_text("not json", encoding="utf-8")
    assert _run(settings, "list")[0] == 1


def test_a_rejected_flow_loses_its_generated_test_and_an_approved_one_keeps_it(tmp_path):
    inventory = {"url": "https://x.test/en", "controls": [{"tag": "button", "name": "Search", "hidden": False}],
                 "headings": [], "revealed": [], "forms": [], "embeds": [], "accessibility": ""}
    artifacts, tests = tmp_path / "artifacts", tmp_path / "tests"
    artifacts.mkdir()
    tests.mkdir()
    (artifacts / "x.inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    settings = _settings(tmp_path, [_flow(A), _flow(B)])
    settings.artifacts_dir, settings.tests_dir = artifacts, tests
    import logging
    log = logging.getLogger("t")
    run_flowgen(settings, log)
    assert (tests / file_name(_flow(A))).exists() and (tests / file_name(_flow(B))).exists()

    _run(settings, "approve", A)
    _run(settings, "reject", B, reason="wrong")
    run_flowgen(settings, log)
    assert (tests / file_name(_flow(A))).exists() and not (tests / file_name(_flow(B))).exists()
    assert (tests / file_name(_flow(A))).read_text(encoding="utf-8").startswith(MARKER)


def test_long_goals_are_cut_with_plain_ascii():
    text = render_list(_doc(_flow(A, goal="word " * 40)), _ratings())
    assert "..." in text and "…" not in text


def test_safe_print_survives_a_console_that_cannot_show_the_text(monkeypatch, capsys):
    import io
    import sys
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", console)
    safe_print("Search البحث")      # Arabic: not in cp1252, plain print() would raise
    console.flush()
    assert console.buffer.getvalue().startswith(b"Search ")
