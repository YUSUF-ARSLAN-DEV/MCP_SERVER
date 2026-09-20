import json
import logging
from types import SimpleNamespace

from website_test_pipeline.flowgen import file_name
from website_test_pipeline.flowresults import apply_pytest_results, feed_results, match_results
from website_test_pipeline.ratings import derive_status


def _run(passed, source="runner"):
    return {"source": source, "at": "t", "passed": passed}


# ------------------------------------------------------------------ derive_status

def test_no_execution_evidence_keeps_the_status():
    assert derive_status("candidate", []) == "candidate"
    assert derive_status("verified", [{"source": "model", "scores": {}}]) == "verified"  # opinions are not runs


def test_a_passing_latest_run_verifies_from_any_state():
    assert derive_status("candidate", [_run(False), _run(True)]) == "verified"
    assert derive_status("stale", [_run(True), _run(False), _run(False), _run(True, "pytest")]) == "verified"


def test_one_failure_is_tolerated_two_in_a_row_make_a_verified_flow_stale():
    assert derive_status("verified", [_run(True), _run(False, "pytest")]) == "verified"
    assert derive_status("verified", [_run(True), _run(False, "pytest"), _run(False)]) == "stale"


def test_failures_do_not_need_to_be_from_the_same_source_but_a_pass_in_between_resets_them():
    assert derive_status("verified", [_run(True), _run(False), _run(True, "pytest"), _run(False)]) == "verified"


def test_a_flow_that_never_passed_stays_a_candidate_not_stale():
    assert derive_status("candidate", [_run(False), _run(False), _run(False)]) == "candidate"
    assert derive_status("candidate", [_run(False)]) == "candidate"


def test_human_decisions_are_never_changed():
    failing = [_run(True), _run(False), _run(False), _run(False)]
    assert derive_status("approved", failing) == "approved"
    assert derive_status("rejected", [_run(True)]) == "rejected"


# ------------------------------------------------------------------ matching and recording

def _flow(flow_id="x--search", status="verified"):
    return {"id": flow_id, "status": status, "start_url": "https://x.test/", "steps": []}


def _results(*rows, finished="2026-09-20T12:00:00+00:00"):
    return {"finished_at": finished, "tests": [
        {"nodeid": f"runs/x/tests/{file_name(_flow(fid))}::test_flow[chromium]", "status": status, "error": err}
        for fid, status, err in rows]}


def test_results_are_matched_to_flows_by_their_spec_file_name():
    flows = [_flow("x--search"), _flow("x--map")]
    rows = _results(("x--search", "passed", None), ("x--map", "skipped", None))
    rows["tests"].append({"nodeid": "runs/x/tests/https-x-test.py::test_page", "status": "failed"})
    assert list(match_results(rows, flows)) == ["x--search"]  # skipped and non-flow tests are ignored


def test_a_failed_row_wins_over_a_passed_one_for_the_same_flow():
    flows = [_flow()]
    rows = _results(("x--search", "passed", None), ("x--search", "failed", "boom"))
    assert match_results(rows, flows)["x--search"]["status"] == "failed"


def test_results_are_recorded_and_the_status_follows_the_history():
    doc, ratings = {"flows": [_flow()]}, {"ratings": {"x--search": [_run(True)]}}
    err = "some context\nE   AssertionError: Page URL expected to be 'x'\nE   more"
    changes = apply_pytest_results(doc, ratings, _results(("x--search", "failed", err)), "now")
    assert changes == [] and doc["flows"][0]["status"] == "verified"           # first failure is tolerated
    entry = ratings["ratings"]["x--search"][-1]
    assert entry["source"] == "pytest" and entry["passed"] is False
    assert entry["error"] == "AssertionError: Page URL expected to be 'x'"

    changes = apply_pytest_results(doc, ratings, _results(("x--search", "failed", err), finished="later"), "now")
    assert changes == [("x--search", "verified", "stale")] and doc["flows"][0]["status"] == "stale"


def test_feeding_the_same_run_twice_adds_nothing():
    doc, ratings = {"flows": [_flow()]}, {"ratings": {}}
    results = _results(("x--search", "passed", None))
    apply_pytest_results(doc, ratings, results, "now")
    apply_pytest_results(doc, ratings, results, "now")
    assert len(ratings["ratings"]["x--search"]) == 1


def test_a_human_status_survives_failing_tests():
    doc, ratings = {"flows": [_flow(status="approved")]}, {"ratings": {}}
    for n in range(3):
        apply_pytest_results(doc, ratings, _results(("x--search", "failed", "E   x"), finished=f"t{n}"), "now")
    assert doc["flows"][0]["status"] == "approved" and len(ratings["ratings"]["x--search"]) == 3


def test_feed_results_reads_and_writes_the_files_and_never_raises(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    flows, ratings = tmp_path / "flows.json", tmp_path / "flow_ratings.json"
    flows.write_text(json.dumps({"version": 1, "flows": [_flow()]}), encoding="utf-8")
    (artifacts / "test_results.json").write_text(json.dumps(_results(("x--search", "passed", None))), encoding="utf-8")
    settings = SimpleNamespace(artifacts_dir=artifacts, flows_file=flows, ratings_file=ratings)
    feed_results(settings, logging.getLogger("t"))
    assert json.loads(ratings.read_text(encoding="utf-8"))["ratings"]["x--search"][0]["source"] == "pytest"

    ratings.write_text("not json", encoding="utf-8")   # a broken ratings file is reported, not raised
    feed_results(settings, logging.getLogger("t"))
    assert ratings.read_text(encoding="utf-8") == "not json"
