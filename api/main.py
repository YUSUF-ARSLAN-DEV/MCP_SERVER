"""FastAPI app: queue a run, follow it, fetch its files.  Start:  uvicorn --factory api.main:create_app"""
from __future__ import annotations
import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import security
from .db import ACTIVE, TERMINAL, Database
from .jobs import JobRunner, site_name
from .models import JobCreate, JobOut, Login
from .security import UrlRejected
from .settings import AppSettings

COOKIE = "wtp_session"
# files a visitor may download from a run folder: results and evidence, never saved logins or credentials
_DOWNLOADABLE = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".json", ".log", ".txt", ".md", ".zip"}
_PRIVATE_PARTS = {"auth", "secrets", ".env"}


def downloadable(run_dir: Path, relative: str) -> Path | None:
    """The file at `relative` inside run_dir if a visitor may have it, else None (outside the folder, a saved login ...)."""
    if not relative or "\x00" in relative:
        return None
    root = Path(run_dir).resolve()
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        return None
    parts = {p.lower() for p in target.relative_to(root).parts}
    if parts & _PRIVATE_PARTS or target.name.endswith(".storage_state.json") or target.suffix.lower() not in _DOWNLOADABLE:
        return None
    return target


def create_app(settings: AppSettings | None = None, resolver=security._resolve) -> FastAPI:
    settings = settings or AppSettings()
    db = Database(settings.db_path)
    runner = JobRunner(db, settings)
    logins = security.SlidingWindow(limit=8, window_s=60)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runner.start()
        yield
        await runner.stop()

    app = FastAPI(title="Website test pipeline", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.runner = settings, db, runner

    def client_id(request: Request) -> str:
        host = request.client.host if request.client else "unknown"
        if settings.trust_proxy and request.headers.get("x-forwarded-for"):
            host = request.headers["x-forwarded-for"].split(",")[0].strip()
        return hashlib.sha256(host.encode()).hexdigest()[:16]

    def authenticated(request: Request) -> bool:
        if settings.allow_open:
            return True
        if not settings.access_code:
            return False
        return (security.verify_token(settings.secret, request.cookies.get(COOKIE))
                or security.code_matches(settings.access_code, request.headers.get("x-access-code")))

    def require_access(request: Request) -> None:
        if not settings.allow_open and not settings.access_code:
            raise HTTPException(503, "The server has no ACCESS_CODE set, so it refuses every request.")
        if not authenticated(request):
            raise HTTPException(401, "Enter the access code first.")

    def job_or_404(job_id: str) -> dict:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "No such run.")
        return job

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/session")
    def session(request: Request):
        return {"authenticated": authenticated(request), "open": settings.allow_open}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        if not settings.access_code:
            raise HTTPException(503, "The server has no ACCESS_CODE set.")
        if not logins.allow(client_id(request)):
            raise HTTPException(429, "Too many attempts. Wait a minute.")
        if not security.code_matches(settings.access_code, body.code):
            raise HTTPException(401, "That code is not right.")
        response.set_cookie(COOKIE, security.make_token(settings.secret), max_age=security.SESSION_TTL_S,
                            httponly=True, samesite="lax", secure=settings.cookie_secure)
        return {"authenticated": True}

    @app.post("/api/jobs", response_model=JobOut, status_code=201, dependencies=[Depends(require_access)])
    def create_job(body: JobCreate, request: Request):
        try:
            url = security.validate_url(body.url, resolver)
        except UrlRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        user = client_id(request)
        if db.active_jobs_for(user):
            raise HTTPException(409, "You already have a run in progress. Wait for it or cancel it.")
        if db.jobs_since(user) >= settings.max_jobs_per_day:
            raise HTTPException(429, f"Daily limit reached ({settings.max_jobs_per_day} runs per day).")
        job_id = uuid.uuid4().hex[:12]
        site = site_name(url, job_id, body.options.reuse_workspace)
        job = db.create_job(job_id, url, site, settings.runs_dir / site, user, body.options.model_dump())
        return JobOut.of(job)

    @app.get("/api/jobs", response_model=list[JobOut], dependencies=[Depends(require_access)])
    def list_jobs(limit: int = 50):
        return [JobOut.of(j) for j in db.list_jobs(max(1, min(limit, 200)))]

    @app.get("/api/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_access)])
    def get_job(job_id: str):
        return JobOut.of(job_or_404(job_id))

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobOut, dependencies=[Depends(require_access)])
    def cancel_job(job_id: str):
        job_or_404(job_id)
        if db.cancel_job(job_id):
            runner.cancel(job_id)
        return JobOut.of(db.get_job(job_id))

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(require_access)])
    async def events(job_id: str, request: Request):
        job_or_404(job_id)

        async def stream():
            last = None
            while True:
                job = db.get_job(job_id)
                if job is None or await request.is_disconnected():
                    return
                view = JobOut.of(job).model_dump()
                if view != last:
                    yield f"event: job\ndata: {json.dumps(view)}\n\n"
                    last = view
                if job["status"] in TERMINAL:
                    return
                await asyncio.sleep(settings.poll_interval_s)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/{job_id}/files", dependencies=[Depends(require_access)])
    def list_files(job_id: str):
        """The reports a run produced (artifacts/report/*), as paths for /files/{path}."""
        run_dir = Path(job_or_404(job_id)["run_dir"])
        found = sorted(p for p in (run_dir / "artifacts" / "report").glob("**/*") if p.is_file()) if run_dir.is_dir() else []
        return [{"path": p.relative_to(run_dir).as_posix(), "size": p.stat().st_size} for p in found if downloadable(run_dir, p.relative_to(run_dir).as_posix())]

    @app.get("/api/jobs/{job_id}/files/{path:path}", dependencies=[Depends(require_access)])
    def get_file(job_id: str, path: str):
        target = downloadable(Path(job_or_404(job_id)["run_dir"]), path)
        if target is None:
            raise HTTPException(404, "No such file.")
        return FileResponse(target, filename=target.name)

    static = Path(__file__).resolve().parents[1] / "static"
    if static.is_dir():                                    # the built front-end, when there is one
        app.mount("/", StaticFiles(directory=static, html=True), name="web")
    return app
