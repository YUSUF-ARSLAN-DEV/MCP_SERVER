"""Reset the data to brand new: every table emptied, every table kept.

    python -m api.reset --yes                       # the data folder from DATA_DIR (or the repo)
    python -m api.reset --yes --data-dir /data      # inside the container: docker compose exec app python3 -m api.reset --yes

Removes every account, run, summary, stored file and saved model, and any leftover run scratch folders. Keeps the database
file and its tables (so the app starts exactly as on a first install) and the two secret files (encryption key for saved API
keys, session signing key): nothing is stored under them any more, and keeping them changes nothing for the next person.
"""
from __future__ import annotations
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

# children before parents, though nothing enforces foreign keys: the order is for a person reading the output
TABLES = ("human_requests", "run_files", "run_info", "model_profiles", "jobs", "users")


def reset(data_dir: Path) -> dict:
    """Empty the app's data under `data_dir`. Returns {"rows": {table: count removed}, "run_folders": n}."""
    data_dir = Path(data_dir)
    db_path = data_dir / "app.db"
    removed: dict[str, int] = {}
    if db_path.is_file():
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            with conn:
                for table in TABLES:
                    if table in existing:
                        removed[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        conn.execute(f"DELETE FROM {table}")
                if "sqlite_sequence" in existing:                 # tables with AUTOINCREMENT start counting from 1 again
                    conn.execute("DELETE FROM sqlite_sequence")
            conn.execute("VACUUM")                                 # give the space back; the file shrinks
        finally:
            conn.close()
    folders = 0
    runs = data_dir / "runs"
    if runs.is_dir():
        for child in runs.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                folders += 1
    return {"rows": removed, "run_folders": folders}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Empty the app's data and keep its tables.")
    parser.add_argument("--data-dir", default="", help="where app.db lives (default: DATA_DIR from the environment, else the repo folder)")
    parser.add_argument("--yes", action="store_true", help="really do it (without this it only says what it would do)")
    args = parser.parse_args(argv)
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        from website_test_pipeline import config
        data_dir = Path(config.DATA_DIR)
    if not (data_dir / "app.db").is_file():
        print(f"No database at {data_dir / 'app.db'}: nothing to reset.")
        return 0
    if not args.yes:
        print(f"This would remove every account, run, stored file and saved model in {data_dir / 'app.db'} and keep the tables.")
        print("Run it again with --yes to do it.")
        return 2
    result = reset(data_dir)
    for table, count in result["rows"].items():
        print(f"  {table:16s} {count:>6} row(s) removed")
    print(f"  run folders      {result['run_folders']:>6} removed")
    print("Done: the tables are kept and empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
