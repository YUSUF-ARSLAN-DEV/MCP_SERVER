"""FastAPI app: queue a run, follow it, fetch its files.  Start:  uvicorn --factory api.main:create_app"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import shutil
import uuid
from types import SimpleNamespace
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import security
from .db import ACTIVE, TERMINAL, Database
from website_test_pipeline.providers import get_provider

from .jobs import JobRunner, forget_model_secret, site_name
from .models import (HumanAnswerIn, JobCreate, JobOut, Login, ModelChoice, PasswordChange, SetupIn, UserCreate, UserOut,
                     UserPatch)
from .security import UrlRejected
from .settings import AppSettings

COOKIE = "wtp_session"
# files a visitor may download from a run folder: results and evidence, never saved logins or credentials
_DOWNLOADABLE = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".json", ".log", ".txt", ".md", ".zip"}
_PRIVATE_PARTS = {"auth", "secrets", ".env", "human"}


def _atomic_write(path: Path, data: bytes, private: bool = False) -> None:
    """Write so the run never reads half a file; a typed answer is readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


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
    logins = security.SlidingWindow(limit=8, window_s=60)           # per client address: sign-ins and first-account setup
    user_logins = security.SlidingWindow(limit=10, window_s=60)     # per username: slows guessing spread across addresses

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

    def identity(request: Request) -> dict | None:
        """Who is asking: {id, username, role, legacy} - or None. Before the first account exists the shared access code
        works as it always did (everyone is the one shared 'person'); once an account exists, only a signed-in user does."""
        if settings.allow_open:
            return {"id": client_id(request), "username": "open", "role": "admin", "legacy": True}
        if db.count_users():
            read = security.read_user_token(settings.secret, request.cookies.get(COOKIE))
            user = db.get_user(read[0]) if read else None
            if user is None or user["disabled"] or user["session_version"] != read[1]:
                return None
            return {"id": user["id"], "username": user["username"], "role": user["role"], "legacy": False}
        if settings.access_code and (security.verify_token(settings.secret, request.cookies.get(COOKIE))
                                     or security.code_matches(settings.access_code, request.headers.get("x-access-code"))):
            return {"id": client_id(request), "username": "shared", "role": "admin", "legacy": True}
        return None

    def require_access(request: Request) -> dict:
        if not settings.allow_open and not settings.access_code and not db.count_users():
            raise HTTPException(503, "The server has no ACCESS_CODE set and no account, so it refuses every request.")
        me = identity(request)
        if me is None:
            raise HTTPException(401, "Sign in first." if db.count_users() else "Enter the access code first.")
        request.state.user = me
        return me

    def require_admin(request: Request) -> dict:
        me = require_access(request)
        if me["legacy"]:
            raise HTTPException(409, "Create the first account before managing users.")
        if me["role"] != "admin":
            raise HTTPException(403, "Only an administrator can do that.")
        return me

    def job_or_404(job_id: str, request: Request) -> dict:
        """The run, if it exists AND belongs to the person asking (an administrator may see every run). Someone else's run is a 404, not a 403."""
        job = db.get_job(job_id)
        me = getattr(request.state, "user", None) or require_access(request)
        if job is None or (me["role"] != "admin" and job["user_id"] != me["id"]):
            raise HTTPException(404, "No such run.")
        return job

    def start_session(response: Response, user: dict) -> None:
        response.set_cookie(COOKIE, security.make_user_token(settings.secret, user["id"], user["session_version"]),
                            max_age=security.SESSION_TTL_S, httponly=True, samesite="lax", secure=settings.cookie_secure)

    def weak(exc: security.WeakCredentials) -> HTTPException:
        return HTTPException(422, str(exc))

    def checked_model(choice: ModelChoice) -> dict:
        """The non-secret part of the person's model choice to keep with the job - or a 422 saying what is wrong."""
        provider = get_provider(choice.provider)
        url, key = choice.api_url.strip(), choice.api_key.strip()
        if url:
            try:
                url = security.validate_model_url(url, settings.allow_private_model_urls, resolver)
            except UrlRejected as exc:
                raise HTTPException(422, f"Model address: {exc}") from exc
        elif not provider.default_url:
            raise HTTPException(422, "Enter the chat-completions address of your OpenAI-compatible server.")
        for problem in provider.problems(SimpleNamespace(api_key=key)):
            raise HTTPException(422, problem)
        return {"provider": provider.name, "model": choice.model.strip(), "api_url": url, "key_set": bool(key)}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/session")
    def session(request: Request):
        me = identity(request)
        accounts = db.count_users() > 0
        return {"authenticated": me is not None, "open": settings.allow_open, "accounts": accounts,
                "setup_needed": not accounts and not settings.allow_open, "needs_code": bool(settings.access_code),
                "user": {"id": me["id"], "username": me["username"], "role": me["role"]} if me and not me["legacy"] else None}

    @app.post("/api/setup")
    def setup(body: SetupIn, request: Request, response: Response):
        """The first account, who becomes the administrator. Guarded by the access code - or, with none set, by being on the server's own machine."""
        if not logins.allow(client_id(request)):
            raise HTTPException(429, "Too many attempts. Wait a minute.")
        if db.count_users():
            raise HTTPException(409, "An account already exists. Sign in.")
        if settings.access_code:
            if not security.code_matches(settings.access_code, body.code):
                raise HTTPException(401, "That access code is not right.")
        elif settings.trust_proxy or (request.client.host if request.client else "") not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "Set up the first account from the machine the server runs on, or give the server an ACCESS_CODE.")
        try:
            name = security.clean_username(body.username)
            security.check_password(body.password, name)
        except security.WeakCredentials as exc:
            raise weak(exc) from exc
        user = db.create_first_user(uuid.uuid4().hex[:12], name, security.hash_password(body.password))
        if user is None:
            raise HTTPException(409, "An account already exists. Sign in.")
        start_session(response, user)
        return {"authenticated": True, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        if db.count_users():
            # only FAILED attempts count: a whole team behind one office address signing in normally is never throttled
            address, name = client_id(request), (body.username or "").strip().lower() or "-"
            if logins.blocked(address) or user_logins.blocked(name):
                raise HTTPException(429, "Too many wrong attempts. Wait a minute.")
            user = db.get_user_by_name(name) if name != "-" else None
            valid = security.verify_password(body.password, user["password_hash"] if user else security.DUMMY_HASH)
            if user is None or not valid or user["disabled"]:
                logins.record(address)
                user_logins.record(name)
                raise HTTPException(401, "Wrong username or password.")
            db.touch_login(user["id"])
            start_session(response, user)
            return {"authenticated": True, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}
        if not logins.allow(client_id(request)):
            raise HTTPException(429, "Too many attempts. Wait a minute.")
        if not settings.access_code:
            raise HTTPException(503, "The server has no ACCESS_CODE set.")
        if not security.code_matches(settings.access_code, body.code):
            raise HTTPException(401, "That code is not right.")
        response.set_cookie(COOKIE, security.make_token(settings.secret), max_age=security.SESSION_TTL_S,
                            httponly=True, samesite="lax", secure=settings.cookie_secure)
        return {"authenticated": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"authenticated": False}

    @app.post("/api/password")
    def change_password(body: PasswordChange, request: Request, response: Response, me: dict = Depends(require_access)):
        if me["legacy"]:
            raise HTTPException(409, "There is no account to change a password for.")
        user = db.get_user(me["id"])
        if not security.verify_password(body.current, user["password_hash"]):
            raise HTTPException(401, "The current password is not right.")
        try:
            security.check_password(body.new, user["username"])
        except security.WeakCredentials as exc:
            raise weak(exc) from exc
        db.update_user(user["id"], password_hash=security.hash_password(body.new))      # every other session of theirs ends here
        start_session(response, db.get_user(user["id"]))
        return {"ok": True}

    # ---- administrators manage the people who may sign in -------------------------------------------------------
    @app.get("/api/users", response_model=list[UserOut])
    def list_users(me: dict = Depends(require_admin)):
        return [UserOut.of(u) for u in db.list_users()]

    @app.post("/api/users", response_model=UserOut, status_code=201)
    def create_user(body: UserCreate, me: dict = Depends(require_admin)):
        try:
            name = security.clean_username(body.username)
            security.check_password(body.password, name)
        except security.WeakCredentials as exc:
            raise weak(exc) from exc
        if db.get_user_by_name(name):
            raise HTTPException(409, "That username is taken.")
        return UserOut.of(db.create_user(uuid.uuid4().hex[:12], name, security.hash_password(body.password), body.role))

    @app.patch("/api/users/{user_id}", response_model=UserOut)
    def patch_user(user_id: str, body: UserPatch, me: dict = Depends(require_admin)):
        user = db.get_user(user_id)
        if user is None:
            raise HTTPException(404, "No such user.")
        role = body.role or user["role"]
        disabled = bool(user["disabled"]) if body.disabled is None else body.disabled
        if user["role"] == "admin" and not user["disabled"] and (role != "admin" or disabled) and db.active_admins() <= 1:
            raise HTTPException(409, "There must be at least one active administrator.")
        new_hash = None
        if body.password is not None:
            try:
                security.check_password(body.password, user["username"])
            except security.WeakCredentials as exc:
                raise weak(exc) from exc
            new_hash = security.hash_password(body.password)
        db.update_user(user_id, role=body.role, disabled=body.disabled, password_hash=new_hash)
        return UserOut.of(db.get_user(user_id))

    @app.post("/api/jobs", response_model=JobOut, status_code=201, dependencies=[Depends(require_access)])
    def create_job(body: JobCreate, request: Request):
        try:
            url = security.validate_url(body.url, resolver, settings.allow_private_targets)
        except UrlRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.model is None and settings.require_model:
            raise HTTPException(422, "Choose a model for this run: its provider, model name and your API key.")
        model = checked_model(body.model) if body.model else None
        me = request.state.user
        user = me["id"]
        if db.active_jobs_for(user):
            raise HTTPException(409, "You already have a run in progress. Wait for it or cancel it.")
        if db.jobs_since(user) >= settings.max_jobs_per_day:
            raise HTTPException(429, f"Daily limit reached ({settings.max_jobs_per_day} runs per day).")
        job_id = uuid.uuid4().hex[:12]
        site = site_name(url, job_id, body.options.reuse_workspace, "" if me["legacy"] else me["id"])
        options = body.options.model_dump()
        run_dir = settings.runs_dir / site
        if model:
            options["model"] = model            # no key in here: the database never holds it
            if body.model.api_key.strip():       # written BEFORE the job exists, so the worker can never start without it
                _atomic_write(run_dir / "secrets" / "model.json", json.dumps({"api_key": body.model.api_key.strip()}).encode("utf-8"), private=True)
        try:
            job = db.create_job(job_id, url, site, run_dir, user, options)
        except Exception:
            forget_model_secret(run_dir)
            raise
        return JobOut.of(job)

    @app.get("/api/jobs", response_model=list[JobOut], dependencies=[Depends(require_access)])
    def list_jobs(request: Request, limit: int = 50):
        me = request.state.user
        return [JobOut.of(j) for j in db.list_jobs(max(1, min(limit, 200)), None if me["role"] == "admin" else me["id"])]

    @app.get("/api/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_access)])
    def get_job(request: Request, job_id: str):
        return JobOut.of(job_or_404(job_id, request))

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobOut, dependencies=[Depends(require_access)])
    def cancel_job(request: Request, job_id: str):
        job_or_404(job_id, request)
        if db.cancel_job(job_id):
            runner.cancel(job_id)
            forget_model_secret(job_or_404(job_id, request)["run_dir"])
            db.close_human_requests(job_id)
        return JobOut.of(db.get_job(job_id))

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(require_access)])
    async def events(job_id: str, request: Request):
        job_or_404(job_id, request)

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
    def list_files(request: Request, job_id: str):
        """The reports a run produced (artifacts/report/*), as paths for /files/{path}."""
        run_dir = Path(job_or_404(job_id, request)["run_dir"])
        found = sorted(p for p in (run_dir / "artifacts" / "report").glob("**/*") if p.is_file()) if run_dir.is_dir() else []
        return [{"path": p.relative_to(run_dir).as_posix(), "size": p.stat().st_size} for p in found if downloadable(run_dir, p.relative_to(run_dir).as_posix())]

    @app.get("/api/jobs/{job_id}/files/{path:path}", dependencies=[Depends(require_access)])
    def get_file(request: Request, job_id: str, path: str):
        target = downloadable(Path(job_or_404(job_id, request)["run_dir"]), path)
        if target is None:
            raise HTTPException(404, "No such file.")
        return FileResponse(target, filename=target.name)

    # ---- a person is needed: a CAPTCHA code or sign-in details (the run asks, the web page answers) ---------------
    def question_view(job_id: str, row: dict) -> dict:
        payload = row["payload"]
        created = datetime.fromisoformat(payload.get("created_at") or row["created_at"])
        return {**payload, "seq": row["seq"], "kind": row["kind"], "state": row["state"],
                "expires_at": (created + timedelta(seconds=int(payload.get("timeout_s") or 0))).isoformat(timespec="seconds"),
                "image": f"/api/jobs/{job_id}/human/{row['seq']}/image" if payload.get("has_image") else None}

    @app.get("/api/jobs/{job_id}/human", dependencies=[Depends(require_access)])
    def open_question(request: Request, job_id: str):
        """The question the run is waiting on, or {"request": null}. Never carries anything a person typed."""
        job_or_404(job_id, request)
        row = db.get_human_request(job_id)
        return {"request": question_view(job_id, row) if row else None}

    @app.get("/api/jobs/{job_id}/human/{seq}/image", dependencies=[Depends(require_access)])
    def question_image(request: Request, job_id: str, seq: int):
        run_dir = Path(job_or_404(job_id, request)["run_dir"])
        image = run_dir / "human" / f"request-{seq}.png"
        if db.get_human_request(job_id, seq, state=None) is None or not image.is_file():
            raise HTTPException(404, "No such picture.")
        return FileResponse(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/jobs/{job_id}/human", dependencies=[Depends(require_access)])
    def answer_question(request: Request, job_id: str, body: HumanAnswerIn):
        run_dir = Path(job_or_404(job_id, request)["run_dir"])
        row = db.get_human_request(job_id, body.seq, state=None) if body.seq is not None else db.get_human_request(job_id)
        if row is None:
            raise HTTPException(404, "Nothing is waiting for an answer.")
        if row["state"] != "open":
            raise HTTPException(409, "That question was already answered or has ended.")
        payload, seq, folder = row["payload"], row["seq"], run_dir / "human"
        if body.action == "refresh":
            if not payload.get("can_reload"):
                raise HTTPException(422, "This page has no way to show a different image.")
            _atomic_write(folder / f"refresh-{seq}.flag", b"")
            return {"ok": True}
        allowed = {f.get("key") or f.get("selector") for f in payload.get("fields") or []}
        if set(body.values) - allowed or any(len(v) > 1000 for v in body.values.values()):
            raise HTTPException(422, "Those answers do not match the fields that were asked for.")
        if body.action == "submit" and not (body.code.strip() if row["kind"] == "captcha" else any(body.values.values())):
            raise HTTPException(422, "Type the code from the image, or skip." if row["kind"] == "captcha"
                                else "Fill in the details, or skip.")
        answer = {"action": body.action}
        if body.action == "submit":
            answer.update(code=body.code, values=body.values)
        _atomic_write(folder / f"answer-{seq}.json", json.dumps(answer).encode("utf-8"), private=True)
        db.set_human_state(job_id, seq, "answered", body.action)            # the action only - never what was typed
        db.sync_waiting(job_id)
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}/session", dependencies=[Depends(require_access)])
    def forget_login(request: Request, job_id: str):
        """Delete the sign-in this site's run saved (runs/<site>/auth/), so the next run asks again."""
        job = job_or_404(job_id, request)
        if job["status"] in ACTIVE:
            raise HTTPException(409, "Wait for the run to finish first.")
        folder = Path(job["run_dir"]) / "auth"
        existed = folder.is_dir()
        shutil.rmtree(folder, ignore_errors=True)
        return {"removed": existed}

    static = Path(__file__).resolve().parents[1] / "static"
    if static.is_dir():                                    # the built front-end, when there is one
        app.mount("/", StaticFiles(directory=static, html=True), name="web")
    return app
