import json
from types import SimpleNamespace

from website_test_pipeline import progress


def test_every_report_updates_the_snapshot_and_appends_a_history_line(tmp_path):
    settings = SimpleNamespace(artifacts_dir=tmp_path)
    progress.report(settings, "Explore", done=1, total=4, note="page 1")
    progress.report(settings, "Explore", done=2, total=4, note="page 2")
    progress.finish(settings)

    assert json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))["finished"] is True
    lines = [json.loads(x) for x in (tmp_path / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["note"] for x in lines] == ["page 1", "page 2", ""]
    assert lines[1]["fraction"] == 0.5 and lines[-1]["finished"] is True


def test_reporting_never_raises_when_the_folder_is_missing(tmp_path):
    progress.report(SimpleNamespace(artifacts_dir=tmp_path / "nope"), "Explore")


def test_the_window_is_unavailable_without_a_display_on_linux(monkeypatch):
    monkeypatch.setattr(progress.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert progress.window_available() is False
