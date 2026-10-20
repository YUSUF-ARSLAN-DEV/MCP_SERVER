"""Everything the API reads from the environment, in one place (so a test can build its own)."""
from __future__ import annotations
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

from website_test_pipeline import config


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class AppSettings:
    data_dir: Path = field(default_factory=lambda: config.DATA_DIR)
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET", ""))
    cookie_secure: bool = field(default_factory=lambda: _flag("COOKIE_SECURE"))
    trust_proxy: bool = field(default_factory=lambda: _flag("TRUST_PROXY"))
    # anyone who can reach the server may create an account, so the protection is limits: this many per address per hour,
    # and this many per day for the whole server (0 = sign-ups are closed)
    signups_per_address_per_hour: int = field(default_factory=lambda: _int("SIGNUPS_PER_ADDRESS_PER_HOUR", 3))
    max_signups_per_day: int = field(default_factory=lambda: _int("MAX_SIGNUPS_PER_DAY", 100))
    # keep what a run produces (reports, evidence, logs, generated tests) in the database instead of leaving it on disk:
    # a run works in a scratch folder, and when it ends everything worth keeping is stored and the folder deleted.
    # STORE_FILES_IN_DB=0 keeps the old behaviour (files stay on disk). Limits keep one huge file from bloating the database.
    store_files_in_db: bool = field(default_factory=lambda: os.getenv("STORE_FILES_IN_DB", "1").strip().lower() not in {"0", "false", "no"})
    max_stored_file_mb: int = field(default_factory=lambda: _int("MAX_STORED_FILE_MB", 30))
    max_stored_run_mb: int = field(default_factory=lambda: _int("MAX_STORED_RUN_MB", 250))
    # master key for saved API keys (see secretbox.py); empty = a key file is created next to the database
    secrets_key: str = field(default_factory=lambda: os.getenv("SECRETS_KEY", ""))
    # every run must name its own model (and key). Off = a run with no model falls back to the server's own .env
    # (REQUIRE_MODEL=0): fine for one person on their own machine, wrong for a server several people share.
    require_model: bool = field(default_factory=lambda: os.getenv("REQUIRE_MODEL", "1").strip().lower() not in {"0", "false", "no"})
    # test sites on private networks (a staging server behind a VPN, localhost). Off by default: on a shared server a
    # visitor could otherwise make it browse its own network. Fine on a machine only you can reach.
    allow_private_targets: bool = field(default_factory=lambda: _flag("ALLOW_PRIVATE_TARGETS"))
    # a person may point a run at their own model server (Ollama, LM Studio ...) on a private address; off by default,
    # because on a shared server that would let a visitor make it call into its own network
    allow_private_model_urls: bool = field(default_factory=lambda: _flag("ALLOW_PRIVATE_MODEL_URLS"))
    # how many runs may execute at the same time (each is a browser plus a pipeline process): different people's runs
    # no longer wait in one line. Each person still has one active run at a time.
    max_concurrent_jobs: int = field(default_factory=lambda: max(1, _int("MAX_CONCURRENT_JOBS", 2)))
    max_pages_limit: int = field(default_factory=lambda: _int("MAX_PAGES_LIMIT", 25))
    job_timeout_s: int = field(default_factory=lambda: _int("JOB_TIMEOUT_S", 1800))
    max_jobs_per_day: int = field(default_factory=lambda: _int("MAX_JOBS_PER_DAY", 5))
    poll_interval_s: float = 1.0
    idle_interval_s: float = 0.5
    # what one job runs; a test swaps in a fake. The per-job environment is added by the runner.
    command: list[str] = field(default_factory=lambda: [sys.executable, "-m", "website_test_pipeline.cli", "all", "--no-window"])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def secret(self) -> str:
        """Key that signs the session cookie: SESSION_SECRET if set, else one generated on first start and kept beside the
        database (so people stay signed in across restarts)."""
        if self.session_secret:
            return self.session_secret
        path = Path(self.data_dir) / "session.secret"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        return value
