import importlib

from website_test_pipeline import config


def _reload(monkeypatch, data_dir):
    if data_dir is None:
        monkeypatch.delenv("DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("DATA_DIR", str(data_dir))
    return importlib.reload(config)


def test_without_data_dir_the_workspace_stays_inside_the_repo(monkeypatch):
    try:
        cfg = _reload(monkeypatch, None)
        assert cfg.RUNS == cfg.ROOT / "runs"
        assert cfg.Settings(site="x.test").workspace == cfg.ROOT / "runs" / "x.test"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_data_dir_moves_every_workspace_path_and_leaves_the_code_root_alone(monkeypatch, tmp_path):
    try:
        cfg = _reload(monkeypatch, tmp_path)
        settings = cfg.Settings(site="x.test")
        base = tmp_path / "runs" / "x.test"
        assert settings.workspace == base
        for path in (settings.tests_dir, settings.artifacts_dir, settings.flows_file, settings.ratings_file,
                     settings.intents_file, settings.heuristics_file, settings.seeds_file, settings.urls_file):
            assert base in path.parents
        assert settings.root == cfg.ROOT
    finally:
        monkeypatch.undo()
        importlib.reload(config)
