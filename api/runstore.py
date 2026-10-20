"""What a run was and what it produced, kept in the database.

A run still works in a folder on disk - the browser, pytest and the pipeline all write files as they go - but that folder
is scratch. When the run ends, `store_run` records a summary of it (run_info) and moves everything worth keeping into the
database (run_files), then deletes the folder. From then on the API serves files from the database.

Never kept, whatever else happens: saved logins (auth/), the API key file (secrets/), what a person typed (human/), .env,
browser storage state. Over-large files and types we do not keep are listed as "omitted" with the reason, not silently lost.
"""
from __future__ import annotations
import hashlib
import json
import mimetypes
import posixpath
import shutil
import subprocess
import zlib
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from website_test_pipeline import config

# what a visitor may download from a run (served), and what is additionally worth keeping (stored)
DOWNLOADABLE = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".json", ".log", ".txt", ".md", ".zip", ".webm", ".mp4"}
STORABLE = DOWNLOADABLE | {".py", ".html", ".csv"}
PRIVATE_PARTS = {"auth", "secrets", ".env", "human"}
_SKIP_PARTS = {"__pycache__", "pw", ".git", ".pytest_cache"}
_TEXT = {".json", ".log", ".txt", ".md", ".py", ".html", ".csv"}        # worth compressing; png/docx/zip/video already are
_MB = 1024 * 1024


def clean_relative(relative: str, allowed: set[str] = DOWNLOADABLE) -> str | None:
    """`relative` as a clean posix path inside a run, or None if it is not something a visitor may have (outside the run,
    a private part, a saved login, a type that is not offered)."""
    if not relative or "\x00" in relative:
        return None
    path = posixpath.normpath(relative.replace("\\", "/"))
    if path.startswith(("/", "..")) or path == "." or "/../" in f"/{path}/" or (len(path) > 1 and path[1] == ":"):
        return None
    parts = {p.lower() for p in path.split("/")}
    name = path.rsplit("/", 1)[-1]
    if parts & PRIVATE_PARTS or name.endswith(".storage_state.json") or Path(name).suffix.lower() not in allowed:
        return None
    return path


def _priority(rel: str, suffix: str) -> int:
    """What is kept first if the run hits its size limit: the report, then text, then screenshots, then big evidence."""
    if rel.startswith("artifacts/report/"):
        return 0
    if suffix in _TEXT:
        return 1
    return 2 if suffix in {".png", ".jpg", ".jpeg"} else 3


def collect_files(run_dir: Path, max_file_mb: int, max_run_mb: int) -> tuple[list[dict], list[dict]]:
    """(rows to store, files left out with the reason). Reads each file once; hashes and (for text) compresses it."""
    candidates = []
    for path in run_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(run_dir)
        parts = {p.lower() for p in relative.parts}
        if parts & PRIVATE_PARTS or parts & _SKIP_PARTS or path.name.endswith((".storage_state.json", ".tmp")):
            continue                                                 # never kept, and not worth a line in "omitted"
        candidates.append((_priority(relative.as_posix(), path.suffix.lower()), relative.as_posix(), path))
    rows, omitted, total = [], [], 0
    for _, rel, path in sorted(candidates):
        suffix, size = path.suffix.lower(), path.stat().st_size
        if suffix not in STORABLE:
            omitted.append({"path": rel, "size": size, "reason": "type not kept"})
        elif size > max_file_mb * _MB:
            omitted.append({"path": rel, "size": size, "reason": f"larger than {max_file_mb} MB"})
        elif total + size > max_run_mb * _MB:
            omitted.append({"path": rel, "size": size, "reason": f"the run's {max_run_mb} MB limit was reached"})
        else:
            data = path.read_bytes()
            packed, encoding = data, "raw"
            if suffix in _TEXT:
                squeezed = zlib.compress(data, 6)
                if len(squeezed) < len(data):
                    packed, encoding = squeezed, "zlib"
            rows.append({"path": rel, "size": len(data), "stored_size": len(packed), "encoding": encoding,
                         "sha256": hashlib.sha256(data).hexdigest(), "content_type": mimetypes.guess_type(rel)[0] or "application/octet-stream",
                         "data": packed})
            total += len(data)
    return rows, omitted


def read_stored(db, job_id: str, relative: str) -> tuple[bytes, str, str] | None:
    """(content, content type, file name) of a stored file a visitor may have, or None."""
    path = clean_relative(relative)
    row = db.stored_file(job_id, path) if path else None
    if row is None:
        return None
    data = zlib.decompress(row["data"]) if row["encoding"] == "zlib" else bytes(row["data"])
    return data, row["content_type"], path.rsplit("/", 1)[-1]


@lru_cache(maxsize=1)
def code_version() -> str:
    """The commit of the code that ran the pipeline, so a run can be tied to the version that produced it."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _report_data(run_dir: Path) -> dict:
    try:
        return json.loads((Path(run_dir) / "artifacts" / "report" / "report-data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _seconds_between(start: str | None, end: str | None) -> int | None:
    try:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except (TypeError, ValueError):
        return None


def build_info(job: dict, run_dir: Path, exit_code: int | None, duration_s: int | None, stored: int, stored_bytes: int,
               omitted: list[dict]) -> tuple[dict, dict]:
    """(summary columns, details) for run_info: what this run was, from the job and the report it produced."""
    report = _report_data(run_dir)
    counts, requirements = report.get("counts") or {}, report.get("requirements") or {}
    environment = report.get("environment") or {}
    model = (job.get("options") or {}).get("model") or {}
    total = requirements.get("total")
    pages = len(list((Path(run_dir) / "artifacts").glob("*.inventory.json"))) if (Path(run_dir) / "artifacts").is_dir() else None
    browser = " ".join(x for x in (environment.get("Browser"), environment.get("Browser version")) if x and x != "NOT CAPTURED") or None
    info = {
        "exit_code": exit_code, "duration_s": duration_s if duration_s is not None else _seconds_between(job.get("started_at"), job.get("finished_at")),
        "verdict": report.get("recommendation"), "tests_ran": counts.get("executed_tests"), "tests_passed": counts.get("passed"),
        "tests_failed": counts.get("failed"), "pass_rate": counts.get("pass_rate_percent"), "journeys_total": counts.get("journeys_total"),
        "journeys_passed": counts.get("journeys_passed"), "open_defects": counts.get("open_defects"),
        "findings_total": len(report.get("defects") or []) if report else None, "requirements_total": total,
        "requirements_backed": (total - (requirements.get("not_backed") or 0)) if total is not None else None, "pages_explored": pages,
        "model_provider": model.get("provider"), "model_name": model.get("model"), "model_profile": (model.get("profile") or {}).get("name"),
        "code_version": code_version(), "browser": browser, "files_stored": stored, "bytes_stored": stored_bytes, "files_omitted": len(omitted)}
    details = {"omitted": omitted[:200], "taxonomy": report.get("taxonomy"), "environment": environment,
               "options": {k: v for k, v in (job.get("options") or {}).items() if k != "model"}, "report_run_id": report.get("run_id")}
    return info, details


def store_run(db, job: dict, run_dir: Path, settings, exit_code: int | None = None, duration_s: int | None = None) -> dict:
    """Record the run's summary, and (unless turned off) move its files into the database and delete the folder.
    Files first, in one transaction; the folder goes only after they are safely in."""
    run_dir = Path(run_dir)
    rows, omitted = [], []
    keep_files = settings.store_files_in_db and run_dir.is_dir()
    if keep_files:
        rows, omitted = collect_files(run_dir, settings.max_stored_file_mb, settings.max_stored_run_mb)
    info, details = build_info(job, run_dir, exit_code, duration_s, len(rows), sum(r["size"] for r in rows), omitted)
    db.save_run_info(job["id"], info, details)
    if keep_files:
        db.store_run_files(job["id"], rows)
        runs_root = Path(settings.runs_dir).resolve()
        if runs_root in run_dir.resolve().parents:                 # never delete anything that is not inside the runs folder
            shutil.rmtree(run_dir, ignore_errors=True)
    return info
