"""Runs queued jobs one at a time, each as a subprocess of the pipeline, and keeps the job row in step with it.

A subprocess (not an import) because Playwright is heavy and can leak memory, Settings is read from the environment,
and a crashed run must never take the API down.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from website_test_pipeline import config

from .db import Database
from .settings import AppSettings

# not passed on to the pipeline: it has no use for the API's own secrets
_PRIVATE_ENV = ("ACCESS_CODE", "SESSION_SECRET")
# `all` exits 0 when every stage ran, 1 when some generated tests failed (still a finished run), 2 when a stage could not run
_FINISHED_CODES = (0, 1)


def site_name(url: str, job_id: str, reuse: bool) -> str:
    """The workspace folder name. A fresh run gets its own folder; `reuse` keeps working in runs/<host>/ (flows, ratings)."""
    host = re.sub(r"[^a-z0-9.-]", "_", (urlsplit(url).netloc or "site").lower())
    return host if reuse else f"{host}-{job_id[:6]}"


def last_progress(run_dir: Path) -> dict | None:
    """The newest line of artifacts/progress.jsonl, or None."""
    path = Path(run_dir) / "artifacts" / "progress.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue                          # a half-written or cut first line
    return None


def kill_tree(proc) -> None:
    """Stop the pipeline and everything it started (pytest, the browser)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def log_tail(path: Path, size: int = 1200) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-size:].strip()
    except OSError:
        return ""


class JobRunner:
    def __init__(self, db: Database, settings: AppSettings):
        self.db, self.settings = db, settings
        self._task: asyncio.Task | None = None
        self._current: tuple[str, asyncio.subprocess.Process] | None = None

    async def start(self) -> None:
        self.db.recover_interrupted()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._current:
            kill_tree(self._current[1])

    def cancel(self, job_id: str) -> None:
        """Kill the pipeline if it is the job running now (the row is marked cancelled by the caller)."""
        if self._current and self._current[0] == job_id:
            kill_tree(self._current[1])

    def environment(self, job: dict) -> dict:
        options = job["options"]
        env = {k: v for k, v in os.environ.items() if k not in _PRIVATE_ENV}
        env.update({
            "SEED_URL": job["url"], "SITE": job["site"], "DATA_DIR": str(self.settings.data_dir), "HEADLESS": "true",
            "WTP_JOB": "1", "PYTHONUNBUFFERED": "1",
            "CRAWL_MAX_PAGES": str(min(int(options.get("max_pages") or self.settings.max_pages_limit), self.settings.max_pages_limit)),
        })
        if options.get("probe_max") is not None:
            env["EXPLORE_PROBE_MAX"] = str(options["probe_max"])
        return env

    async def _loop(self) -> None:
        while True:
            job = self.db.claim_next()
            if job is None:
                await asyncio.sleep(self.settings.idle_interval_s)
                continue
            try:
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:          # a bug here must fail this job, not end the worker
                self.db.finish_job(job["id"], "failed", f"Could not run: {exc}")

    async def _run(self, job: dict) -> None:
        run_dir = Path(job["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "job.log"
        kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
        with log_path.open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                *self.settings.command, env=self.environment(job), cwd=str(config.ROOT),
                stdout=log, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, **kwargs)
            self._current = (job["id"], proc)
            started, timed_out, shown = time.monotonic(), False, None
            try:
                while proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), self.settings.poll_interval_s)
                    except asyncio.TimeoutError:
                        pass
                    shown = self._sync_progress(job["id"], run_dir, shown)
                    if time.monotonic() - started > self.settings.job_timeout_s and proc.returncode is None:
                        timed_out = True
                        kill_tree(proc)
                        await proc.wait()
            finally:
                self._current = None
                if proc.returncode is None:
                    kill_tree(proc)           # the worker itself was stopped
        self._sync_progress(job["id"], run_dir, shown)
        if timed_out:
            self.db.finish_job(job["id"], "failed", f"The run passed its time limit ({self.settings.job_timeout_s} seconds) and was stopped.")
        elif proc.returncode in _FINISHED_CODES:
            self.db.finish_job(job["id"], "done")
        else:
            self.db.finish_job(job["id"], "failed", log_tail(log_path) or f"The pipeline stopped with exit code {proc.returncode}.")

    def _sync_progress(self, job_id: str, run_dir: Path, shown: tuple | None) -> tuple | None:
        row = last_progress(run_dir)
        if not row:
            return shown
        view = (row.get("phase") or "", int(float(row.get("fraction") or 0) * 100), row.get("note") or "")
        if view != shown:
            self.db.update_job(job_id, stage=view[0], progress_pct=min(view[1], 99), message=view[2])
        return view
