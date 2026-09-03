import json

from website_test_pipeline.failrepair import failing_by_url, _inventory_for, _inv_slug
from website_test_pipeline.models import PageInventory


def test_failing_by_url_groups_and_trims(tmp_path):
    (tmp_path / "test_results.json").write_text(json.dumps({"tests": [
        {"url": "https://x.test/a", "title": "test_one", "status": "failed",
         "error": "Traceback...\nE   assert 0\nlong line " + "x" * 800},
        {"url": "https://x.test/a", "title": "test_two", "status": "error", "error": "boom"},
        {"url": "https://x.test/b", "title": "test_ok", "status": "passed", "error": None},
        {"url": "https://x.test/c", "title": "test_skip", "status": "skipped"},
    ]}), encoding="utf-8")
    out = failing_by_url(tmp_path)
    assert set(out) == {"https://x.test/a"}
    assert len(out["https://x.test/a"]) == 2
    assert all(len(line) < 460 for line in out["https://x.test/a"])
    assert out["https://x.test/a"][0].startswith("- test_one:")

def test_failing_by_url_empty_when_no_file(tmp_path):
    assert failing_by_url(tmp_path) == {}

def test_inventory_for_round_trips(tmp_path):
    url = "https://x.test/page"
    inv = PageInventory(url, "T", headings=[{"level": "H1", "text": "Hi"}],
                        controls=[{"name": "Go", "tag": "button"}], embeds=[])
    (tmp_path / f"{_inv_slug(url)}.inventory.json").write_text(
        json.dumps(inv.__dict__, ensure_ascii=False), encoding="utf-8")
    loaded = _inventory_for(tmp_path, url)
    assert isinstance(loaded, PageInventory)
    assert loaded.url == url and loaded.controls[0]["name"] == "Go"

def test_inventory_for_missing_returns_none(tmp_path):
    assert _inventory_for(tmp_path, "https://x.test/nope") is None

def test_inv_slug_matches_cli_name():
    from website_test_pipeline.cli import name
    for u in ["https://sat.aljazeera.net/ar/frequency-search", "https://x.test/"]:
        assert _inv_slug(u) == name(u)
