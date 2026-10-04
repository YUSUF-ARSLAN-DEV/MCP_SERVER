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


def test_download_report_copies_the_combined_docx_to_downloads(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import logging
    from website_test_pipeline.cli import _download_report
    monkeypatch.setattr("website_test_pipeline.cli.Path.home", lambda: tmp_path)   # never touch the real Downloads
    report_dir = tmp_path / "artifacts" / "report"
    report_dir.mkdir(parents=True)
    (report_dir / "full-report.docx").write_bytes(b"not a real docx, just bytes to copy")
    settings = SimpleNamespace(artifacts_dir=tmp_path / "artifacts", site="x.test")
    _download_report(settings, logging.getLogger("test"))
    copies = list((tmp_path / "Downloads").glob("x.test-report-*.docx"))
    assert len(copies) == 1 and copies[0].read_bytes() == b"not a real docx, just bytes to copy"


def test_download_report_falls_back_to_the_locked_file_alternative(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import logging
    from website_test_pipeline.cli import _download_report
    monkeypatch.setattr("website_test_pipeline.cli.Path.home", lambda: tmp_path)
    report_dir = tmp_path / "artifacts" / "report"
    report_dir.mkdir(parents=True)
    (report_dir / "full-report-new.docx").write_bytes(b"the -new fallback _save_document writes when locked")
    settings = SimpleNamespace(artifacts_dir=tmp_path / "artifacts", site="x.test")
    _download_report(settings, logging.getLogger("test"))
    assert list((tmp_path / "Downloads").glob("x.test-report-*.docx"))


def test_download_report_warns_and_does_not_raise_when_nothing_was_built(tmp_path):
    from types import SimpleNamespace
    import logging
    from website_test_pipeline.cli import _download_report
    settings = SimpleNamespace(artifacts_dir=tmp_path / "artifacts", site="x.test")
    _download_report(settings, logging.getLogger("test"))     # no report/ dir at all - must not raise
