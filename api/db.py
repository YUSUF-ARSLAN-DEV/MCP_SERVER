"""SQLite store for job metadata. The pipeline's own files (runs/<site>/...) stay where they are."""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACTIVE = ("queued", "running", "needs_human")
TERMINAL = ("done", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, site TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    stage TEXT NOT NULL DEFAULT '', progress_pct INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '', run_dir TEXT NOT NULL, options TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created_at);
CREATE TABLE IF NOT EXISTS human_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}', answer TEXT, state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL, answered_at TEXT, UNIQUE (job_id, seq)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:                       # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    # ---- jobs -------------------------------------------------------------------------------------------------
    def create_job(self, job_id: str, url: str, site: str, run_dir: Path, user_id: str, options: dict) -> dict:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (id, url, site, status, created_at, user_id, run_dir, options) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, url, site, "queued", now(), user_id, str(run_dir), json.dumps(options)))
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row)

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [_job(r) for r in rows]

    def claim_next(self) -> dict | None:
        """Oldest queued job, atomically marked running (so two workers could never take the same one)."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status = 'running', started_at = ?, stage = 'Starting' WHERE id = ?", (now(), row["id"]))
        return self.get_job(row["id"])

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        names = ", ".join(f"{name} = ?" for name in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE jobs SET {names} WHERE id = ?", (*fields.values(), job_id))

    def finish_job(self, job_id: str, status: str, error: str = "") -> None:
        """Move a job to a terminal status - but never overwrite one that already ended (e.g. cancelled by a person)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ?, progress_pct = CASE WHEN ? = 'done' THEN 100 ELSE progress_pct END "
                "WHERE id = ? AND status NOT IN ('done','failed','cancelled')", (status, error, now(), status, job_id))

    def cancel_job(self, job_id: str) -> bool:
        """True when the job was still active and is now cancelled."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status IN ('queued','running','needs_human')",
                (now(), job_id))
        return cur.rowcount > 0

    def recover_interrupted(self) -> int:
        """After a restart nothing is really running: mark those jobs failed rather than leave them stuck."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'failed', error = 'The server restarted while this run was in progress.', finished_at = ? "
                "WHERE status IN ('running','needs_human')", (now(),))
        return cur.rowcount

    # ---- limits -----------------------------------------------------------------------------------------------
    def active_jobs_for(self, user_id: str) -> int:
        marks = ",".join("?" for _ in ACTIVE)
        with self._conn() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status IN ({marks})", (user_id, *ACTIVE)).fetchone()[0]

    def jobs_since(self, user_id: str, hours: int = 24) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND created_at >= ?", (user_id, since)).fetchone()[0]


def _job(row) -> dict | None:
    if row is None:
        return None
    job = dict(row)
    job["options"] = json.loads(job.get("options") or "{}")
    return job
