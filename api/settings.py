"""Everything the API reads from the environment, in one place (so a test can build its own)."""
from __future__ import annotations
import os
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
    # shared access code (see security.py). Empty = the API refuses everything unless allow_open is set.
    access_code: str = field(default_factory=lambda: os.getenv("ACCESS_CODE", ""))
    allow_open: bool = field(default_factory=lambda: _flag("API_ALLOW_OPEN"))
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET", ""))
    cookie_secure: bool = field(default_factory=lambda: _flag("COOKIE_SECURE"))
    trust_proxy: bool = field(default_factory=lambda: _flag("TRUST_PROXY"))
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
        """Key that signs the session cookie: SESSION_SECRET, else derived from the access code."""
        return self.session_secret or f"wtp-session:{self.access_code}"
