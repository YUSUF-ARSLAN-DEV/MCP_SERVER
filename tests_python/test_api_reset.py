"""`python -m api.reset`: every table emptied, every table kept, nothing else touched."""
import sqlite3
import sys

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from authhelp import PW
from api import reset as reset_tool
from api.db import Database
from api.main import create_app
from api.settings import AppSettings


KEY = Fernet.generate_key()                  # a real key file holds a real key, or the app would refuse to start on it


def fill(data_dir):
    """A database with something in every table, plus the files that sit beside it."""
    db = Database(data_dir / "app.db")
    db.create_user("u1", "alice", "hash")
    run = data_dir / "runs" / "site-1"
    (run / "artifacts").mkdir(parents=True)
    (run / "artifacts" / "scratch.txt").write_text("working file")
    db.create_job("j1", "https://x.test/", "site-1", run, "u1", {})
    db.save_run_info("j1", {"verdict": "GO", "tests_ran": 3}, {})
    db.store_run_files("j1", [{"path": "artifacts/report/a.json", "size": 2, "stored_size": 2, "encoding": "raw", "sha256": "x",
                               "content_type": "application/json", "data": b"{}"}])
    db.create_profile("p1", "u1", "Work", "openai", "m", "https://x.test/v1/chat/completions", "encrypted")
    db.add_human_request("j1", 1, "captcha", {"url": "https://x.test/"})
    (data_dir / "secrets.key").write_bytes(KEY)
    (data_dir / "session.secret").write_text("signing-key")
    return db


def tables(path):
    with sqlite3.connect(path) as conn:
        return {name: sql for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def counts(path):
    with sqlite3.connect(path) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables(path)}


def test_every_table_is_emptied_and_every_table_is_kept_exactly_as_it_was(tmp_path):
    fill(tmp_path)
    before, loaded = tables(tmp_path / "app.db"), counts(tmp_path / "app.db")
    assert all(n > 0 for n in loaded.values()), loaded                          # the test really had data in every table
    result = reset_tool.reset(tmp_path)
    assert tables(tmp_path / "app.db") == before                                # the schema is untouched
    assert set(counts(tmp_path / "app.db").values()) == {0}
    assert result["rows"]["users"] == 1 and result["rows"]["run_files"] == 1 and result["rows"]["model_profiles"] == 1


def test_leftover_run_folders_go_and_the_two_secret_files_stay(tmp_path):
    fill(tmp_path)
    result = reset_tool.reset(tmp_path)
    assert result["run_folders"] == 1 and list((tmp_path / "runs").iterdir()) == []
    assert (tmp_path / "secrets.key").read_bytes() == KEY and (tmp_path / "session.secret").read_text() == "signing-key"


def test_the_app_starts_on_a_reset_database_exactly_as_on_a_first_install(tmp_path):
    fill(tmp_path)
    reset_tool.reset(tmp_path)
    settings = AppSettings(data_dir=tmp_path, command=[sys.executable, "-c", "pass"], require_model=False)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/session").json()["authenticated"] is False
        assert client.post("/api/signup", json={"username": "alice", "password": PW}).status_code == 200      # the old "alice" is gone: the name is free
        assert client.get("/api/jobs").json() == []                                                          # and so are her runs


def test_without_yes_it_only_says_what_it_would_do(tmp_path, capsys):
    fill(tmp_path)
    assert reset_tool.main(["--data-dir", str(tmp_path)]) == 2
    assert "--yes" in capsys.readouterr().out
    assert counts(tmp_path / "app.db")["users"] == 1                            # nothing was touched


def test_with_yes_it_does_it_and_reports_what_it_removed(tmp_path, capsys):
    fill(tmp_path)
    assert reset_tool.main(["--data-dir", str(tmp_path), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "users" in out and "1 row(s) removed" in out and "tables are kept" in out
    assert counts(tmp_path / "app.db")["users"] == 0


def test_a_missing_database_is_not_an_error(tmp_path, capsys):
    assert reset_tool.main(["--data-dir", str(tmp_path / "nothing-here"), "--yes"]) == 0
    assert "nothing to reset" in capsys.readouterr().out
