"""Runs queued jobs one at a time, each as a subprocess of the pipeline, and keeps the job row in step with it.

A subprocess (not an import) because Playwright is heavy and can leak memory, Settings is read from the environment,
and a crashed run must never take the API down.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from website_test_pipeline import config
from website_test_pipeline.providers import get_provider

from .db import Database
from .settings import AppSettings

# not passed on to the pipeline: it has no use for the API's own secrets
_PRIVATE_ENV = ("ACCESS_CODE", "SESSION_SECRET")
# `all` exits 0 when every stage ran, 1 when some generated tests failed (still a finished run), 2 when a stage could not run
_FINISHED_CODES = (0, 1)


def site_name(url: str, job_id: str, reuse: bool, owner: str = "") -> str:
    """The workspace folder name. A fresh run gets its own folder; `reuse` keeps working in runs/<host>/ (flows, ratings).
    With accounts the reused folder is per person (runs/<owner>-<host>/): nobody works in, or sees, another person's."""
    host = re.sub(r"[^a-z0-9.-]", "_", (urlsplit(url).netloc or "site").lower())
    if reuse:
        return f"{owner}-{host}" if owner else host
    return f"{host}-{job_id[:6]}"


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


def forget_model_secret(run_dir) -> None:
    """Delete the API key a person typed for this run (runs/<site>/secrets/). It lives only as long as the run does."""
    shutil.rmtree(Path(run_dir) / "secrets", ignore_errors=True)


def read_model_secret(run_dir) -> str:
    try:
        return str(json.loads((Path(run_dir) / "secrets" / "model.json").read_text(encoding="utf-8")).get("api_key") or "")
    except (OSError, ValueError):
        return ""


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
        self._running: set[asyncio.Task] = set()                       # one task per run in progress
        self._procs: dict[str, asyncio.subprocess.Process] = {}        # job id -> its pipeline process

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
        for task in list(self._running):
            task.cancel()
        await asyncio.gather(*self._running, return_exceptions=True)
        for proc in list(self._procs.values()):
            kill_tree(proc)

    def cancel(self, job_id: str) -> None:
        """Kill the pipeline if this job is running now (the row is marked cancelled by the caller)."""
        proc = self._procs.get(job_id)
        if proc is not None:
            kill_tree(proc)

    def environment(self, job: dict) -> dict:
        options = job["options"]
        env = {k: v for k, v in os.environ.items() if k not in _PRIVATE_ENV}
        env.update({
            "SEED_URL": job["url"], "SITE": job["site"], "DATA_DIR": str(self.settings.data_dir), "HEADLESS": "true",
            "WTP_JOB": "1", "PYTHONUNBUFFERED": "1",
            "CRAWL_MAX_PAGES": str(min(int(options.get("max_pages") or self.settings.max_pages_limit), self.settings.max_pages_limit)),
        })
        choice = options.get("model")
        if choice:                               # the person picked a model for this run; otherwise the server's own .env applies
            provider = get_provider(choice["provider"])
            env.update({"MODEL_PROVIDER": provider.name, "MODEL_NAME": choice["model"],
                        "API_URL": choice.get("api_url") or provider.default_url, "API_KEY": read_model_secret(job["run_dir"])})
        if options.get("probe_max") is not None:
            env["EXPLORE_PROBE_MAX"] = str(options["probe_max"])
        if options.get("ask_human", True):       # a CAPTCHA or login wall is put to the person on the web page
            env.update({"HUMAN_CHANNEL": "web", "WTP_HUMAN_DIR": str(Path(job["run_dir"]) / "human")})
        else:                                    # unattended: such steps are skipped and reported as needing a person
            env["HUMAN_CHANNEL"] = "off"
        return env

    def sync_human(self, job_id: str, run_dir: Path) -> None:
        """Mirror the run's questions (human/request-N.json) into the database, and the job status with them."""
        folder = Path(run_dir) / "human"
        for path in sorted(folder.glob("request-*.json")):
            seq = path.stem.split("-", 1)[1]
            if not seq.isdigit():
                continue
            if not self.db.get_human_request(job_id, int(seq), state=None):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                self.db.add_human_request(job_id, int(seq), payload.get("kind", "captcha"), payload)
        for row in self.db.human_requests(job_id):
            if (folder / f"closed-{row['seq']}.json").is_file():
                self.db.set_human_state(job_id, row["seq"], "closed", "")
        self.db.sync_waiting(job_id)

    def _finish_human(self, job_id: str, run_dir: Path) -> None:
        """The run is over: no question is open any more, and no typed answer may be left on disk."""
        self.sync_human(job_id, run_dir)
        self.db.close_human_requests(job_id)
        for leftover in (Path(run_dir) / "human").glob("answer-*.json*"):
            leftover.unlink(missing_ok=True)

    async def _loop(self) -> None:
        """Start queued runs, oldest first, while there is room: up to max_concurrent_jobs at once."""
        while True:
            job = self.db.claim_next() if len(self._running) < self.settings.max_concurrent_jobs else None
            if job is None:
                await asyncio.sleep(self.settings.idle_interval_s)
                continue
            task = asyncio.create_task(self._run_safely(job))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _run_safely(self, job: dict) -> None:
        try:
            await self._run(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:              # a bug here must fail this job, not end the worker (or anyone else's run)
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
            self._procs[job["id"]] = proc
            forget_model_secret(run_dir)       # the process has its environment now; the key does not need to sit on disk
            started, timed_out, shown = time.monotonic(), False, None
            try:
                while proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), self.settings.poll_interval_s)
                    except asyncio.TimeoutError:
                        pass
                    shown = self._sync_progress(job["id"], run_dir, shown)
                    self.sync_human(job["id"], run_dir)
                    if time.monotonic() - started > self.settings.job_timeout_s and proc.returncode is None:
                        timed_out = True
                        kill_tree(proc)
                        await proc.wait()
            finally:
                self._procs.pop(job["id"], None)
                forget_model_secret(run_dir)   # the run has read its key; nothing keeps it
                if proc.returncode is None:
                    kill_tree(proc)           # the worker itself was stopped
        self._sync_progress(job["id"], run_dir, shown)
        self._finish_human(job["id"], run_dir)
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
