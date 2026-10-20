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
CREATE TABLE IF NOT EXISTS run_info (
    job_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, exit_code INTEGER, duration_s INTEGER,
    verdict TEXT, tests_ran INTEGER, tests_passed INTEGER, tests_failed INTEGER, pass_rate INTEGER,
    journeys_total INTEGER, journeys_passed INTEGER, open_defects INTEGER, findings_total INTEGER,
    requirements_total INTEGER, requirements_backed INTEGER, pages_explored INTEGER,
    model_provider TEXT, model_name TEXT, model_profile TEXT, code_version TEXT, browser TEXT,
    files_stored INTEGER NOT NULL DEFAULT 0, bytes_stored INTEGER NOT NULL DEFAULT 0, files_omitted INTEGER NOT NULL DEFAULT 0,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS run_files (
    job_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, stored_size INTEGER NOT NULL, encoding TEXT NOT NULL DEFAULT 'raw',
    sha256 TEXT NOT NULL, content_type TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY (job_id, path)
);
CREATE TABLE IF NOT EXISTS model_profiles (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE, provider TEXT NOT NULL, model TEXT NOT NULL,
    api_url TEXT NOT NULL DEFAULT '', key_enc TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member', disabled INTEGER NOT NULL DEFAULT 0, session_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS human_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}', answer TEXT, state TEXT NOT NULL DEFAULT 'open',
    outcome TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, answered_at TEXT, UNIQUE (job_id, seq)
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
            if "stored" not in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}:                  # a database from before files were kept in it
                conn.execute("ALTER TABLE jobs ADD COLUMN stored INTEGER NOT NULL DEFAULT 0")
            if "outcome" not in {r[1] for r in conn.execute("PRAGMA table_info(human_requests)")}:      # a database from before outcomes were kept
                conn.execute("ALTER TABLE human_requests ADD COLUMN outcome TEXT NOT NULL DEFAULT ''")

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

    def list_jobs(self, limit: int = 50, user_id: str | None = None) -> list[dict]:
        """Newest first. With user_id, only that person's runs."""
        where, args = ("WHERE user_id = ? ", (user_id,)) if user_id is not None else ("", ())
        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM jobs {where}ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
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

    # ---- questions for a person ---------------------------------------------------------------------------------
    # state: open (waiting for the person) -> answered (the run has not picked it up yet) -> closed (the run is done with it)
    def add_human_request(self, job_id: str, seq: int, kind: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO human_requests (job_id, seq, kind, payload, created_at) VALUES (?,?,?,?,?)",
                         (job_id, seq, kind, json.dumps(payload), now()))

    def get_human_request(self, job_id: str, seq: int | None = None, state: str | None = "open") -> dict | None:
        """One request of the job: `seq`, or the newest open one. state=None accepts any state."""
        sql, args = "SELECT * FROM human_requests WHERE job_id = ?", [job_id]
        if seq is not None:
            sql, args = sql + " AND seq = ?", args + [seq]
        if state is not None:
            sql, args = sql + " AND state = ?", args + [state]
        with self._conn() as conn:
            row = conn.execute(sql + " ORDER BY seq DESC LIMIT 1", args).fetchone()
        if row is None:
            return None
        found = dict(row)
        found["payload"] = json.loads(found["payload"])
        return found

    def human_requests(self, job_id: str, states: tuple = ("open", "answered")) -> list[dict]:
        marks = ",".join("?" for _ in states)
        with self._conn() as conn:
            rows = conn.execute(f"SELECT seq, state FROM human_requests WHERE job_id = ? AND state IN ({marks}) ORDER BY seq",
                                (job_id, *states)).fetchall()
        return [dict(r) for r in rows]

    def set_human_state(self, job_id: str, seq: int, state: str, answer: str | None = None, outcome: str | None = None) -> None:
        """`answer` is only ever the action taken (submit / skip), never what the person typed. `outcome` is what the run
        did about the request (submitted / skipped / timeout / ended). Whatever is left out is left as it was."""
        sets, args = ["state = ?"], [state]
        if answer is not None:
            sets += ["answer = ?", "answered_at = ?"]
            args += [answer, now()]
        if outcome is not None:
            sets.append("outcome = ?")
            args.append(outcome)
        with self._conn() as conn:
            conn.execute(f"UPDATE human_requests SET {', '.join(sets)} WHERE job_id = ? AND seq = ?", (*args, job_id, seq))

    def human_history(self, job_id: str) -> list[dict]:
        """Every question this run put to a person, oldest first, with what became of each."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM human_requests WHERE job_id = ? ORDER BY seq", (job_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def close_human_requests(self, job_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE human_requests SET state = 'closed', outcome = CASE WHEN outcome = '' THEN 'ended' ELSE outcome END "
                         "WHERE job_id = ? AND state != 'closed'", (job_id,))

    def sync_waiting(self, job_id: str) -> None:
        """The job is `needs_human` exactly while a question is open, and `running` otherwise."""
        with self._conn() as conn:
            waiting = conn.execute("SELECT COUNT(*) FROM human_requests WHERE job_id = ? AND state = 'open'", (job_id,)).fetchone()[0]
            if waiting:
                conn.execute("UPDATE jobs SET status = 'needs_human' WHERE id = ? AND status = 'running'", (job_id,))
            else:
                conn.execute("UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'needs_human'", (job_id,))

    # ---- accounts ---------------------------------------------------------------------------------------------
    def count_users(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, user_id: str, username: str, password_hash: str, role: str) -> dict:
        with self._conn() as conn:
            conn.execute("INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                         (user_id, username, password_hash, role, now()))
        return self.get_user(user_id)

    def create_first_user(self, user_id: str, username: str, password_hash: str) -> dict | None:
        """The administrator, created only if there are no accounts at all (None when someone got there first)."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (id, username, password_hash, role, created_at) SELECT ?,?,?,'admin',? WHERE (SELECT COUNT(*) FROM users) = 0",
                (user_id, username, password_hash, now()))
        return self.get_user(user_id) if cur.rowcount else None

    def users_created_since(self, hours: int = 24) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (since,)).fetchone()[0]

    def get_user(self, user_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_name(self, username: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at, rowid").fetchall()]

    def active_admins(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0").fetchone()[0]

    def touch_login(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user_id))

    def update_user(self, user_id: str, *, role: str | None = None, disabled: bool | None = None, password_hash: str | None = None) -> None:
        """A new password or a disabled account also ends every session that user has (session_version moves on)."""
        sets, args = [], []
        if role is not None:
            sets.append("role = ?"); args.append(role)
        if disabled is not None:
            sets.append("disabled = ?"); args.append(1 if disabled else 0)
        if password_hash is not None:
            sets.append("password_hash = ?"); args.append(password_hash)
        if disabled or password_hash is not None:
            sets.append("session_version = session_version + 1")
        if sets:
            with self._conn() as conn:
                conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*args, user_id))

    # ---- what a run was (info) and what it produced (files) ------------------------------------------------------
    INFO_COLUMNS = ("exit_code", "duration_s", "verdict", "tests_ran", "tests_passed", "tests_failed", "pass_rate", "journeys_total",
                    "journeys_passed", "open_defects", "findings_total", "requirements_total", "requirements_backed", "pages_explored",
                    "model_provider", "model_name", "model_profile", "code_version", "browser", "files_stored", "bytes_stored",
                    "files_omitted")

    def save_run_info(self, job_id: str, info: dict, details: dict) -> None:
        cols = [c for c in self.INFO_COLUMNS if c in info]
        with self._conn() as conn:
            conn.execute(f"INSERT OR REPLACE INTO run_info (job_id, recorded_at, {', '.join(cols)}, details) VALUES (?, ?, {', '.join('?' for _ in cols)}, ?)",
                         (job_id, now(), *[info[c] for c in cols], json.dumps(details)))

    def get_run_info(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM run_info WHERE job_id = ?", (job_id,)).fetchone()
        return _info(row)

    def run_infos(self, job_ids: list[str]) -> dict[str, dict]:
        if not job_ids:
            return {}
        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM run_info WHERE job_id IN ({','.join('?' for _ in job_ids)})", job_ids).fetchall()
        return {r["job_id"]: _info(r) for r in rows}

    def store_run_files(self, job_id: str, rows: list[dict]) -> None:
        """All of a run's files in one transaction, then the run is marked stored - so a reader sees all of it or none of it."""
        with self._conn() as conn:
            conn.execute("DELETE FROM run_files WHERE job_id = ?", (job_id,))
            conn.executemany("INSERT INTO run_files (job_id, path, size, stored_size, encoding, sha256, content_type, data) VALUES (?,?,?,?,?,?,?,?)",
                             [(job_id, r["path"], r["size"], r["stored_size"], r["encoding"], r["sha256"], r["content_type"], r["data"]) for r in rows])
            conn.execute("UPDATE jobs SET stored = 1 WHERE id = ?", (job_id,))

    def stored_file(self, job_id: str, path: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM run_files WHERE job_id = ? AND path = ?", (job_id, path)).fetchone()
        return dict(row) if row else None

    def stored_files(self, job_id: str, prefix: str = "") -> list[dict]:
        """Path, size and hash of a run's stored files (never the content), optionally only those under `prefix`."""
        with self._conn() as conn:
            rows = conn.execute("SELECT path, size, stored_size, sha256, content_type FROM run_files WHERE job_id = ? AND path LIKE ? ESCAPE '\\' ORDER BY path",
                                (job_id, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")).fetchall()
        return [dict(r) for r in rows]

    def unstored_finished_jobs(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE stored = 0 AND status IN ('done','failed','cancelled') ORDER BY created_at").fetchall()
        return [_job(r) for r in rows]

    # ---- saved model configurations (one person's own; the key is stored encrypted, never plain) ---------------------
    def create_profile(self, profile_id: str, user_id: str, name: str, provider: str, model: str, api_url: str, key_enc: str) -> dict:
        stamp = now()
        with self._conn() as conn:
            conn.execute("INSERT INTO model_profiles (id, user_id, name, provider, model, api_url, key_enc, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (profile_id, user_id, name, provider, model, api_url, key_enc, stamp, stamp))
        return self.get_profile(profile_id)

    def get_profile(self, profile_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM model_profiles WHERE id = ?", (profile_id,)).fetchone()
        return dict(row) if row else None

    def get_profile_by_name(self, user_id: str, name: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM model_profiles WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
        return dict(row) if row else None

    def list_profiles(self, user_id: str) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM model_profiles WHERE user_id = ? ORDER BY created_at, rowid", (user_id,)).fetchall()]

    def update_profile(self, profile_id: str, **fields) -> None:
        if not fields:
            return
        names = ", ".join(f"{name} = ?" for name in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE model_profiles SET {names}, updated_at = ? WHERE id = ?", (*fields.values(), now(), profile_id))

    def delete_profile(self, profile_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM model_profiles WHERE id = ?", (profile_id,))

    # ---- limits -----------------------------------------------------------------------------------------------
    def active_jobs_for(self, user_id: str) -> int:
        marks = ",".join("?" for _ in ACTIVE)
        with self._conn() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status IN ({marks})", (user_id, *ACTIVE)).fetchone()[0]

    def jobs_since(self, user_id: str, hours: int = 24) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND created_at >= ?", (user_id, since)).fetchone()[0]


def _info(row) -> dict | None:
    if row is None:
        return None
    info = dict(row)
    info["details"] = json.loads(info.get("details") or "{}")
    return info


def _job(row) -> dict | None:
    if row is None:
        return None
    job = dict(row)
    job["options"] = json.loads(job.get("options") or "{}")
    return job
