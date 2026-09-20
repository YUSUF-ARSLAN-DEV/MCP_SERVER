import json
import logging
from types import SimpleNamespace

import pytest

from website_test_pipeline.critic import parse_ratings, prompt_for, rate_flows
from website_test_pipeline.proposer import _apply_critic
from website_test_pipeline.ratings import RatingsFileError, append_rating, load_ratings, save_ratings

FLOWS = [
    {"id": "a", "goal": "Search", "start_url": "https://x.test/en", "evidence": "e",
     "steps": [{"kind": "click", "selector": None, "name": "Search"}], "outcome": {"effect": "results"}},
    {"id": "b", "goal": "Glued", "start_url": "https://x.test/en", "evidence": "e",
     "steps": [{"kind": "click", "selector": None, "name": "Channel"}], "outcome": {"effect": "reveals"}},
]


def _row(n, coh, imp=4, out=3, reason="r"):
    return {"n": n, "coherence": coh, "importance": imp, "outcome_strength": out, "reason": reason}


def _client(payload):
    class C:
        def generate(self, prompt, system):
            return payload if isinstance(payload, str) else json.dumps(payload)
    return C()


def _settings(tmp_path):
    return SimpleNamespace(model="m", ratings_file=tmp_path / "r.json")


def test_prompt_numbers_flows_and_names_the_rubric():
    text = prompt_for(FLOWS, "SITE MAP (1 pages explored)")
    assert "1. goal: Search" in text and "2. goal: Glued" in text and "coherence" in text


def test_parse_ratings_keeps_valid_rows_only():
    raw = json.dumps({"ratings": [_row(1, 5), _row(2, 9), {"n": 3, "coherence": 3}, "junk"]})
    got = parse_ratings("```json\n" + raw + "\n```", 2)
    assert list(got) == [1]
    assert got[1]["scores"] == {"coherence": 5, "importance": 4, "outcome_strength": 3}


def test_rate_flows_records_model_and_prompt_version():
    got = rate_flows(_client({"ratings": [_row(1, 4)]}), FLOWS, "MAP", "m1", "t")
    assert got[1]["source"] == "model" and got[1]["model"] == "m1" and got[1]["prompt_version"] == "critic-v1"


def test_incoherent_flow_is_dropped_but_its_rating_is_kept(tmp_path):
    client = _client({"ratings": [_row(1, 4), _row(2, 1, reason="unrelated controls")]})
    kept = _apply_critic(FLOWS, client, "MAP", _settings(tmp_path), "t", logging.getLogger("t"))
    assert [f["id"] for f in kept] == ["a"]
    hist = load_ratings(tmp_path / "r.json")["ratings"]
    assert hist["a"][0]["kept"] is True and hist["b"][0]["kept"] is False
    assert hist["b"][0]["reason"] == "unrelated controls"


def test_history_accumulates_across_runs(tmp_path):
    client = _client({"ratings": [_row(1, 4), _row(2, 4)]})
    for _ in range(2):
        _apply_critic(FLOWS, client, "MAP", _settings(tmp_path), "t", logging.getLogger("t"))
    assert len(load_ratings(tmp_path / "r.json")["ratings"]["a"]) == 2


def test_critic_failure_keeps_flows_unrated(tmp_path):
    kept = _apply_critic(FLOWS, _client("not json"), "MAP", _settings(tmp_path), "t", logging.getLogger("t"))
    assert kept == FLOWS and not (tmp_path / "r.json").exists()


def test_flow_the_critic_skipped_is_kept_unrated(tmp_path):
    kept = _apply_critic(FLOWS, _client({"ratings": [_row(2, 5)]}), "MAP", _settings(tmp_path),
                         "t", logging.getLogger("t"))
    assert [f["id"] for f in kept] == ["a", "b"]


def test_corrupt_ratings_file_is_refused(tmp_path):
    (tmp_path / "r.json").write_text("{ nope", encoding="utf-8")
    with pytest.raises(RatingsFileError):
        load_ratings(tmp_path / "r.json")


def test_append_and_save_round_trip(tmp_path):
    doc = load_ratings(tmp_path / "none.json")
    append_rating(doc, "x", {"source": "human", "kept": True})
    save_ratings(tmp_path / "r.json", doc)
    assert load_ratings(tmp_path / "r.json")["ratings"]["x"][0]["source"] == "human"
