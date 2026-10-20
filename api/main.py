"""FastAPI app: queue a run, follow it, fetch its files.  Start:  uvicorn --factory api.main:create_app"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
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
from .secretbox import SecretBoxError, load_secretbox
from .models import (HumanAnswerIn, JobCreate, JobOut, Login, ModelChoice, PasswordChange, ProfileIn, ProfileOut, ProfilePatch,
                     SignupIn)
from .security import UrlRejected
from .settings import AppSettings

COOKIE = "wtp_session"
# files a visitor may download from a run folder: results and evidence, never saved logins or credentials
from .runstore import DOWNLOADABLE as _DOWNLOADABLE, PRIVATE_PARTS as _PRIVATE_PARTS, clean_relative, read_stored


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
    box = load_secretbox(settings.data_dir, settings.secrets_key)      # encrypts the keys people save
    logins = security.SlidingWindow(limit=8, window_s=60)           # per client address: wrong sign-ins
    signups = security.SlidingWindow(limit=settings.signups_per_address_per_hour, window_s=3600)      # accounts created per address per hour
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
        """Who is asking: {id, username} - or None when nobody is signed in. A session is a signed token that names the user;
        changing the password ends every session of theirs."""
        read = security.read_user_token(settings.secret, request.cookies.get(COOKIE))
        user = db.get_user(read[0]) if read else None
        if user is None or user["session_version"] != read[1]:
            return None
        return {"id": user["id"], "username": user["username"]}

    def require_access(request: Request) -> dict:
        me = identity(request)
        if me is None:
            raise HTTPException(401, "Sign in first.")
        request.state.user = me
        return me

    def job_or_404(job_id: str, request: Request) -> dict:
        """The run, if it exists AND is yours. Someone else's run is a 404, not a 403, so its existence is not revealed."""
        job = db.get_job(job_id)
        me = getattr(request.state, "user", None) or require_access(request)
        if job is None or job["user_id"] != me["id"]:
            raise HTTPException(404, "No such run.")
        return job

    SUMMARY_FIELDS = ("verdict", "tests_ran", "tests_passed", "tests_failed", "pass_rate", "journeys_total", "journeys_passed", "open_defects",
                      "findings_total", "duration_s", "exit_code", "model_name", "model_profile", "code_version", "files_stored", "bytes_stored")

    def summarize(info: dict | None) -> dict | None:
        return {k: info.get(k) for k in SUMMARY_FIELDS} if info else None

    def view(job: dict, info: dict | None = None) -> JobOut:
        out = JobOut.of(job)
        out.summary = summarize(info if info is not None else db.get_run_info(job["id"]))
        return out

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
        return {"authenticated": me is not None, "user": me, "server_model": not settings.require_model}

    @app.post("/api/signup")
    def signup(body: SignupIn, request: Request, response: Response):
        """Create your own account. Open to anyone who can reach the server; the protection is limits, not a password: a few
        accounts per address per hour and a daily cap for the whole server (a cap of 0 closes sign-ups)."""
        address = client_id(request)
        if signups.blocked(address):
            raise HTTPException(429, "Too many accounts were created from your address. Try again later.")
        if db.users_created_since(24) >= settings.max_signups_per_day:
            raise HTTPException(429, "This server is not taking new accounts right now. Try again tomorrow.")
        try:
            name = security.clean_username(body.username)
            security.check_password(body.password, name)
        except security.WeakCredentials as exc:
            raise weak(exc) from exc
        if db.get_user_by_name(name):
            raise HTTPException(409, "That username is taken.")
        try:
            user = db.create_user(uuid.uuid4().hex[:12], name, security.hash_password(body.password))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That username is taken.") from None
        signups.record(address)
        start_session(response, user)
        return {"authenticated": True, "user": {"id": user["id"], "username": user["username"]}}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        # only FAILED attempts count: a whole team behind one office address signing in normally is never throttled
        address, name = client_id(request), (body.username or "").strip().lower() or "-"
        if logins.blocked(address) or user_logins.blocked(name):
            raise HTTPException(429, "Too many wrong attempts. Wait a minute.")
        user = db.get_user_by_name(name) if name != "-" else None
        valid = security.verify_password(body.password, user["password_hash"] if user else security.DUMMY_HASH)
        if user is None or not valid:
            logins.record(address)
            user_logins.record(name)
            raise HTTPException(401, "Wrong username or password.")
        db.touch_login(user["id"])
        start_session(response, user)
        return {"authenticated": True, "user": {"id": user["id"], "username": user["username"]}}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"authenticated": False}

    @app.post("/api/password")
    def change_password(body: PasswordChange, request: Request, response: Response, me: dict = Depends(require_access)):
        user = db.get_user(me["id"])
        if not security.verify_password(body.current, user["password_hash"]):
            raise HTTPException(401, "The current password is not right.")
        try:
            security.check_password(body.new, user["username"])
        except security.WeakCredentials as exc:
            raise weak(exc) from exc
        db.set_password(user["id"], security.hash_password(body.new))      # every other session of theirs ends here
        start_session(response, db.get_user(user["id"]))
        return {"ok": True}

    # ---- saved model configurations: yours alone, the key encrypted and never returned --------------------------
    MAX_PROFILES = 20

    def my_profile(profile_id: str, me: dict) -> dict:
        row = db.get_profile(profile_id)
        if row is None or row["user_id"] != me["id"]:
            raise HTTPException(404, "No such saved model.")
        return row

    def stored_key(row: dict) -> str:
        try:
            return box.decrypt(row["key_enc"]) if row["key_enc"] else ""
        except SecretBoxError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/models", response_model=list[ProfileOut])
    def list_models(me: dict = Depends(require_access)):
        return [ProfileOut.of(p) for p in db.list_profiles(me["id"])]

    @app.post("/api/models", response_model=ProfileOut, status_code=201)
    def create_model(body: ProfileIn, me: dict = Depends(require_access)):
        name = body.name.strip()
        checked = checked_model(ModelChoice(provider=body.provider, model=body.model, api_url=body.api_url, api_key=body.api_key))
        if db.get_profile_by_name(me["id"], name):
            raise HTTPException(409, "You already have a saved model with that name.")
        if len(db.list_profiles(me["id"])) >= MAX_PROFILES:
            raise HTTPException(422, f"You can save up to {MAX_PROFILES} models. Delete one first.")
        key = body.api_key.strip()
        row = db.create_profile(uuid.uuid4().hex[:12], me["id"], name, checked["provider"], checked["model"], checked["api_url"],
                                box.encrypt(key) if key else "")
        return ProfileOut.of(row)

    @app.patch("/api/models/{profile_id}", response_model=ProfileOut)
    def update_model(profile_id: str, body: ProfilePatch, me: dict = Depends(require_access)):
        row = my_profile(profile_id, me)
        new_key = None if body.api_key is None else body.api_key.strip()
        plain = stored_key(row) if new_key is None else new_key
        checked = checked_model(ModelChoice(provider=body.provider or row["provider"], model=body.model or row["model"],
                                            api_url=row["api_url"] if body.api_url is None else body.api_url, api_key=plain))
        name = (body.name or row["name"]).strip()
        other = db.get_profile_by_name(me["id"], name)
        if other and other["id"] != row["id"]:
            raise HTTPException(409, "You already have a saved model with that name.")
        fields = {"name": name, "provider": checked["provider"], "model": checked["model"], "api_url": checked["api_url"]}
        if new_key is not None:
            fields["key_enc"] = box.encrypt(new_key) if new_key else ""
        db.update_profile(row["id"], **fields)
        return ProfileOut.of(db.get_profile(row["id"]))

    @app.delete("/api/models/{profile_id}")
    def delete_model(profile_id: str, me: dict = Depends(require_access)):
        my_profile(profile_id, me)
        db.delete_profile(profile_id)
        return {"removed": True}

    @app.post("/api/models/{profile_id}/test")
    def test_model(profile_id: str, me: dict = Depends(require_access)):
        """Ask the model's own server which models it serves, with the saved key: does the address work, is the key accepted,
        is this model offered. Says so in words; never returns or logs the key."""
        row = my_profile(profile_id, me)
        provider, key = get_provider(row["provider"]), stored_key(row)
        url = row["api_url"]
        if url:
            try:
                url = security.validate_model_url(url, settings.allow_private_model_urls, resolver)      # checked again: DNS can change
            except UrlRejected as exc:
                return {"ok": False, "message": f"Model address: {exc}", "models": []}
        probe = SimpleNamespace(api_url=url or provider.default_url, api_key=key, model=row["model"], model_provider=row["provider"])
        try:
            request_url, headers = provider.models_request(probe)
            with urllib.request.urlopen(urllib.request.Request(request_url, headers=headers), timeout=10) as response:
                ids = provider.model_ids(json.load(response))
        except urllib.error.HTTPError as exc:
            reason = {401: "The server did not accept the key.", 403: "The server did not accept the key.",
                      404: "The server answered, but not like this kind of API. Check the address and the provider."}.get(exc.code, f"The server answered with an error ({exc.code}).")
            return {"ok": False, "message": reason, "models": []}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {"ok": False, "message": f"Could not reach the server ({type(exc).__name__}). Check the address.", "models": []}
        wanted = row["model"].removeprefix("models/")
        if not ids:
            return {"ok": True, "message": "Connected. The server does not list its models, so the model name could not be checked.", "models": []}
        if wanted in ids:
            return {"ok": True, "message": f"Connected. '{wanted}' is available.", "models": ids[:30]}
        return {"ok": False, "message": f"Connected, but '{wanted}' is not offered. It offers: {', '.join(ids[:8])}{' ...' if len(ids) > 8 else ''}.", "models": ids[:30]}

    @app.post("/api/jobs", response_model=JobOut, status_code=201, dependencies=[Depends(require_access)])
    def create_job(body: JobCreate, request: Request):
        try:
            url = security.validate_url(body.url, resolver, settings.allow_private_targets)
        except UrlRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        choice, profile = body.model, None
        if body.model_id:
            if body.model:
                raise HTTPException(422, "Send either a saved model or a one-off one, not both.")
            profile = my_profile(body.model_id, request.state.user)
            choice = ModelChoice(provider=profile["provider"], model=profile["model"], api_url=profile["api_url"], api_key=stored_key(profile))
        if choice is None and settings.require_model:
            raise HTTPException(422, "Choose a model for this run: one of your saved models, or a provider, model name and API key.")
        model = checked_model(choice) if choice else None
        if model and profile:
            model["profile"] = {"id": profile["id"], "name": profile["name"]}
        me = request.state.user
        user = me["id"]
        if db.active_jobs_for(user):
            raise HTTPException(409, "You already have a run in progress. Wait for it or cancel it.")
        if db.jobs_since(user) >= settings.max_jobs_per_day:
            raise HTTPException(429, f"Daily limit reached ({settings.max_jobs_per_day} runs per day).")
        job_id = uuid.uuid4().hex[:12]
        site = site_name(url, job_id, body.options.reuse_workspace, me["id"])
        options = body.options.model_dump()
        run_dir = settings.runs_dir / site
        if model:
            options["model"] = model            # no key in here: the database never holds it
            if choice.api_key.strip():           # written BEFORE the job exists, so the worker can never start without it
                _atomic_write(run_dir / "secrets" / "model.json", json.dumps({"api_key": choice.api_key.strip()}).encode("utf-8"), private=True)
        try:
            job = db.create_job(job_id, url, site, run_dir, user, options)
        except Exception:
            forget_model_secret(run_dir)
            raise
        return view(job)

    @app.get("/api/jobs", response_model=list[JobOut], dependencies=[Depends(require_access)])
    def list_jobs(request: Request, limit: int = 50):
        me = request.state.user
        jobs = db.list_jobs(max(1, min(limit, 200)), me["id"])
        infos = db.run_infos([j["id"] for j in jobs])
        return [view(j, infos.get(j["id"]) or {}) if j["id"] in infos else view(j, {}) for j in jobs]

    @app.get("/api/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_access)])
    def get_job(request: Request, job_id: str):
        return view(job_or_404(job_id, request))

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobOut, dependencies=[Depends(require_access)])
    def cancel_job(request: Request, job_id: str):
        job_or_404(job_id, request)
        if db.cancel_job(job_id):
            runner.cancel(job_id)
            forget_model_secret(job_or_404(job_id, request)["run_dir"])
            db.close_human_requests(job_id)
        return view(db.get_job(job_id))

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(require_access)])
    async def events(job_id: str, request: Request):
        job_or_404(job_id, request)

        async def stream():
            last = None
            while True:
                job = db.get_job(job_id)
                if job is None or await request.is_disconnected():
                    return
                shown = view(job).model_dump()
                if shown != last:
                    yield f"event: job\ndata: {json.dumps(shown)}\n\n"
                    last = shown
                if job["status"] in TERMINAL:
                    return
                await asyncio.sleep(settings.poll_interval_s)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/{job_id}/info", dependencies=[Depends(require_access)])
    def run_info(request: Request, job_id: str):
        """Everything recorded about a run: its summary and details (the model, the options, the code version, how long it
        took, what was kept in the database and what was left out and why). Never any key or what a person typed."""
        job = job_or_404(job_id, request)
        info = db.get_run_info(job_id)
        return {"job": view(job, info or {}).model_dump(), "info": info, "stored_in_database": bool(job.get("stored"))}

    @app.get("/api/jobs/{job_id}/files", dependencies=[Depends(require_access)])
    def list_files(request: Request, job_id: str):
        """The reports a run produced (artifacts/report/*), as paths for /files/{path}."""
        job = job_or_404(job_id, request)
        if job.get("stored"):                                   # the run's files live in the database
            return [{"path": f["path"], "size": f["size"]} for f in db.stored_files(job_id, "artifacts/report/") if clean_relative(f["path"])]
        run_dir = Path(job["run_dir"])
        found = sorted(p for p in (run_dir / "artifacts" / "report").glob("**/*") if p.is_file()) if run_dir.is_dir() else []
        return [{"path": p.relative_to(run_dir).as_posix(), "size": p.stat().st_size} for p in found if downloadable(run_dir, p.relative_to(run_dir).as_posix())]

    @app.get("/api/jobs/{job_id}/files/{path:path}", dependencies=[Depends(require_access)])
    def get_file(request: Request, job_id: str, path: str):
        job = job_or_404(job_id, request)
        if job.get("stored"):
            found = read_stored(db, job_id, path)
            if found is None:
                raise HTTPException(404, "No such file.")
            data, content_type, name = found
            return Response(content=data, media_type=content_type,
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"})
        target = downloadable(Path(job["run_dir"]), path)
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

    @app.get("/api/jobs/{job_id}/human/history", dependencies=[Depends(require_access)])
    def human_history(request: Request, job_id: str):
        """What became of every question this run asked a person: waiting, sent, and how the run ended up using it.
        Never carries what was typed. (The run does not check whether a SITE accepted a code; that shows in the test result.)"""
        job_or_404(job_id, request)
        return [{"seq": r["seq"], "kind": r["kind"], "state": r["state"], "action": r["answer"] or "", "outcome": r["outcome"] or "",
                 "url": r["payload"].get("url", ""), "label": r["payload"].get("label", ""), "asked_at": r["created_at"],
                 "answered_at": r["answered_at"]} for r in db.human_history(job_id)]

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
