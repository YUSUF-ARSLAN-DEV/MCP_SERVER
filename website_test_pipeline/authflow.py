"""Sign in / sign up on a wall the explorer found, confirm it worked, and keep the session.

Fills the fields the person typed into the popup (authpopup), submits the form, and judges the outcome from what
the page shows afterwards - the password form gone, a visible error message - never from words like "welcome".
On success the logged-in session (Playwright storage_state) is saved under runs/<site>/auth/, and the details are
written to .env only if the person asked for that. Typed values go straight into the page; they are never logged.
See docs/CREDENTIAL_POPUP_DESIGN.md.
"""
from __future__ import annotations
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .authpopup import AuthAnswer, AuthRequest, ask_credentials, field_key, is_interactive, should_ask

MAX_ATTEMPTS = 3
SETTLE_MS = 1500

# Visible text that is plainly an error. Narrower than the explorer's validation selector on purpose: a bare
# `.messages` / `.alert` is often a success banner, and mistaking that for a failed login would be worse.
_AUTH_ERROR_SEL = ('[role="alert"],[aria-invalid="true"],.error,.errors,.invalid-feedback,.field-error,'
                   '.form-error,.has-error,.alert-danger,[class*="error" i]')

_SUBMIT_JS = """root => {
    if (root.tagName === 'FORM') { root.requestSubmit ? root.requestSubmit() : root.submit(); return 'form'; }
    const b = [...root.querySelectorAll('button, input[type=submit], [role=button]')]
        .find(x => x.getClientRects().length && !x.disabled);
    if (b) { b.click(); return 'button'; }
    return null;
}"""

_STATE_JS = """() => ({
    password_visible: [...document.querySelectorAll('input[type=password]')]
        .some(e => e.getClientRects().length && !e.readOnly && !e.disabled),
})"""


@dataclass
class AuthOutcome:
    ok: bool
    signals: dict = field(default_factory=dict)   # what was observed, so a reader can check the verdict
    error_text: str = ""                          # the site's own error message, if one showed
    url_after: str = ""
    attempts: int = 1


def _fill(page, group: int, index: int, fld: dict, value: str) -> None:
    locator = page.locator(f'[data-wtp-auth="{group}-{index}"]')
    if (fld.get("type") or "").startswith("select"):
        locator.select_option(label=value)
    else:
        locator.fill(value)


def perform_auth(page, wall: dict, answer: AuthAnswer, settle_ms: int = SETTLE_MS) -> AuthOutcome:
    """Fill and submit `wall` (an entry of PageInventory.auth, found on the CURRENT page), then judge the result."""
    group = wall["group"]
    url_before = page.url
    for i, fld in enumerate(wall["fields"]):
        value = answer.values.get(field_key(i, fld))
        if value:
            _fill(page, group, i, fld, value)
    try:
        page.locator(f'[data-wtp-auth-root="{group}"]').evaluate(_SUBMIT_JS)
    except Exception:
        pass                              # the submit navigated and tore down the page: judge it below
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(settle_ms)
    state = page.evaluate(_STATE_JS)
    errors = [t for t in (e.strip() for e in page.locator(_AUTH_ERROR_SEL).all_inner_texts()) if t][:3]
    signals = {"password_form_gone": not state["password_visible"], "error_shown": bool(errors),
               "url_changed": page.url != url_before}
    ok = signals["password_form_gone"] and not signals["error_shown"]
    return AuthOutcome(ok, signals, errors[0][:200] if errors else "", page.url)


# ------------------------------------------------------------------ keeping things out of git

def ensure_ignored(path: Path, repo_root: Path) -> bool:
    """True if git ignores `path` afterwards. If it did not, append its repo-relative path to .gitignore first."""
    def ignored() -> bool:
        return subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=repo_root,
                              capture_output=True).returncode == 0
    try:
        if ignored():
            return True
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
        with open(repo_root / ".gitignore", "a", encoding="utf-8") as fh:
            fh.write(f"\n# added by the auth flow: holds a login secret\n{rel}\n")
        return ignored()
    except (OSError, ValueError):
        return False


def _account(settings) -> str:
    return getattr(settings, "auth_account", "default") or "default"


def session_path(settings) -> Path:
    return settings.workspace / "auth" / f"state.{_account(settings)}.json"


def has_session(settings) -> bool:
    return hasattr(settings, "workspace") and session_path(settings).is_file()


def context_kwargs(settings) -> dict:
    """Arguments for browser.new_context(): start already signed in when a saved session exists."""
    return {"storage_state": str(session_path(settings))} if has_session(settings) else {}


def save_session(context, settings) -> Path:
    path = session_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not ensure_ignored(path, settings.root):
        raise RuntimeError(f"refusing to write a login session to {path}: git would not ignore it")
    context.storage_state(path=str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _env_name(site: str, account: str, key: str) -> str:
    return "AUTH_" + re.sub(r"[^A-Z0-9]+", "_", f"{site}_{account}_{key}".upper()).strip("_")


def env_names(site: str, account: str, wall: dict) -> dict[str, dict]:
    """{variable name: the field it fills} for every field of `wall` - what a person must put in .env so this
    login can run without anyone typing."""
    return {_env_name(site, account, field_key(i, f)): f for i, f in enumerate(wall["fields"])}


def credentials_from_env(site: str, account: str, wall: dict, environ=None) -> AuthAnswer | None:
    """The details for `wall` from .env, or None unless every required field (and every password) has a value."""
    environ = os.environ if environ is None else environ
    values = {}
    for i, f in enumerate(wall["fields"]):
        key = field_key(i, f)
        value = environ.get(_env_name(site, account, key), "")
        if value:
            values[key] = value
        elif f.get("required") or f.get("type") == "password":
            return None
    return AuthAnswer(values) if values else None


def remember_credentials(env_path: Path, repo_root: Path, site: str, wall: dict, answer: AuthAnswer,
                         account: str = "default") -> list[str]:
    """Write the typed values to .env as AUTH_<SITE>_<ACCOUNT>_<FIELD>=... (replacing older ones). Returns the
    variable NAMES written - only names are ever shown or logged."""
    if not ensure_ignored(env_path, repo_root):
        raise RuntimeError(f"refusing to write credentials to {env_path}: git would not ignore it")
    new = {name: answer.values[field_key(i, f)] for i, (name, f) in enumerate(env_names(site, account, wall).items())
           if answer.values.get(field_key(i, f))}
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in new]
    lines += [f"{name}={value}" for name, value in new.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sorted(new)


# ------------------------------------------------------------------ the whole thing

def authenticate_wall(page, wall: dict, url: str, ask=ask_credentials, max_attempts: int = MAX_ATTEMPTS,
                      log=None) -> tuple[AuthOutcome | None, AuthAnswer | None]:
    """Ask, fill, confirm - retrying with the site's own error shown - until it works, the person skips, or the
    attempts run out. (None, None) = the person skipped."""
    notice = ""
    for attempt in range(1, max_attempts + 1):
        answer = ask(AuthRequest(url, wall["kind"], wall["fields"], notice), lambda: page.screenshot())
        if answer is None:
            return None, None
        outcome = perform_auth(page, wall, answer)
        outcome.attempts = attempt
        if log:
            log.info("auth: attempt %d %s signals=%s", attempt, "ok" if outcome.ok else "failed", outcome.signals)
        if outcome.ok:
            return outcome, answer
        notice = ("That did not work" + (f': the site says "{outcome.error_text}"' if outcome.error_text
                                          else " - the form is still showing") + ". Please check and try again.")
        if attempt < max_attempts:
            from .explorer import _detect_auth
            page.goto(url)
            walls = _detect_auth(page, url)
            if not walls:
                return outcome, None
            wall = walls[min(wall["group"], len(walls) - 1)]
    return outcome, None


@dataclass
class SessionResult:
    status: str       # no-wall | off | session-ok | signed-in-env | signed-in-popup | not-tested | skipped | failed
    detail: str = ""


def _say(log, message: str) -> None:
    if log:
        log.info("auth: %s", message)
    else:
        print(message)


def ensure_session(settings, browser, url: str, log=None, ask=ask_credentials, interactive: bool | None = None) -> SessionResult:
    """Make sure the browser can see past a login wall on `url`, asking as little as possible:
       1. a saved session that still works  ->  nothing to do;
       2. details in .env for this site + account  ->  sign in silently (an unattended run is never interrupted);
       3. an interactive run  ->  the popup;
       4. otherwise the wall is left untested and the .env names to fill in are printed."""
    from .explorer import _detect_auth
    if settings.auth_mode == "none":
        return SessionResult("off")
    account = _account(settings)
    had_session = has_session(settings)
    context = browser.new_context(**context_kwargs(settings))
    try:
        page = context.new_page()
        page.goto(url, timeout=settings.navigation_timeout_ms)
        walls = _detect_auth(page, url, log)
        if not walls:
            return SessionResult("session-ok" if had_session else "no-wall")
        wall = walls[0]
        if had_session:
            _say(log, "the saved session no longer gets past the login wall - signing in again")

        creds = credentials_from_env(settings.site, account, wall)
        if creds:
            outcome = perform_auth(page, wall, creds)
            if outcome.ok:
                save_session(context, settings)
                save_landing(settings, outcome.url_after, url)
                _say(log, f"signed in as '{account}' from .env; session saved")
                return SessionResult("signed-in-env")
            _say(log, f"the details in .env for '{account}' were refused"
                      + (f' (the site says "{outcome.error_text}")' if outcome.error_text else ""))
            page.goto(url, timeout=settings.navigation_timeout_ms)
            walls = _detect_auth(page, url, log)
            if not walls:
                return SessionResult("failed", "the login form disappeared after a refused attempt")
            wall = walls[0]

        go, reason = should_ask(settings.auth_mode, wall["kind"], False, is_interactive() if interactive is None else interactive)
        if not go and had_session:
            names = ", ".join(env_names(settings.site, account, wall))
            return SessionResult("failed", f"the saved session expired and nothing can renew it without a person; "
                                           f"set in .env: {names}, or run `auth`")
        if not go:
            names = ", ".join(env_names(settings.site, account, wall))
            _say(log, f"login wall on {url} left untested ({reason}). To sign in unattended, set in .env: {names}")
            return SessionResult("not-tested", reason)
        outcome, answer = authenticate_wall(page, wall, url, ask=ask, log=log)
        if answer is None:
            return SessionResult("skipped" if outcome is None else "failed",
                                 "" if outcome is None else f"gave up after {outcome.attempts} attempts")
        save_session(context, settings)
        save_landing(settings, outcome.url_after, url)
        if answer.remember:
            names = remember_credentials(settings.root / ".env", settings.root, settings.site, wall, answer, account)
            _say(log, "remembered in .env as: " + ", ".join(names))
        return SessionResult("signed-in-popup", f"attempt {outcome.attempts}")
    finally:
        context.close()


def preflight_session(settings, log) -> int:
    """Before the browser stages of a run: check the saved login still works and renew it from .env if not, so an
    expired session cannot turn every test into a failure at the login page. 0 = go ahead; 2 = it could not be
    renewed. Never blocks a site with no login wall, and never stops a run just because the check itself broke."""
    url = getattr(settings, "seed_url", "")
    if getattr(settings, "auth_mode", "none") == "none" or not url:
        return 0
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.headless)
            try:
                result = ensure_session(settings, browser, url, log)
            finally:
                browser.close()
    except Exception as exc:
        log.warning("auth: could not check the login session (%s); carrying on", str(exc).splitlines()[0][:150] if str(exc) else exc.__class__.__name__)
        return 0
    if result.status in {"failed", "skipped"}:
        log.error("auth: the login could not be renewed%s - the browser stages were not run. Fix the details in .env "
                  "or run `auth`", f" ({result.detail})" if result.detail else "")
        return 2
    return 0


def run_auth(settings, log, url: str) -> int:
    """`cli auth [url]`: find the login / sign-up form on `url` and sign in - saved session, then .env, then the popup."""
    from playwright.sync_api import sync_playwright
    if not url:
        log.error("auth needs a URL (or SEED_URL in .env)")
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        try:
            result = ensure_session(settings, browser, url, log)
        finally:
            browser.close()
    messages = {
        "no-wall": "No login or sign-up form found on this page - nothing to do.",
        "off": "AUTH_MODE=none: not signing in.",
        "session-ok": f"The saved session for '{settings.auth_account}' still works.",
        "signed-in-env": f"Signed in from .env. Session saved to {session_path(settings)} (git-ignored).",
        "signed-in-popup": f"Signed in ({result.detail}). Session saved to {session_path(settings)} (git-ignored).",
        "not-tested": f"Left untested: {result.detail}.",
        "skipped": "Skipped.",
        "failed": f"Could not sign in: {result.detail}.",
    }
    print(messages[result.status])
    return 0 if result.status in {"no-wall", "off", "session-ok", "signed-in-env", "signed-in-popup", "not-tested"} else 1


# ------------------------------------------------------------------ flows that use the login (step 6)

def account_available(settings, inventories: list[dict]) -> bool:
    """True when the pipeline can act as a signed-in visitor: a saved session, or .env details for a login wall it
    found. This is what lets the AI write journeys that sign in or that sit behind the login."""
    if getattr(settings, "auth_mode", "auto") == "none" or not hasattr(settings, "site"):
        return False
    if has_session(settings):
        return True
    account = _account(settings)
    return any(credentials_from_env(settings.site, account, wall)
               for inv in inventories for wall in inv.get("auth") or [])


def _fold(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def bind_credentials(steps: list[dict], inventories: list[dict], site: str, account: str, environ=None) -> str:
    """Point every fill step that targets a login / sign-up field at the .env variable holding its value
    ({env:NAME} - a reference, never the secret; the model never sees a value). '' when fine, else why the flow
    cannot be built: typing into a login form needs details that are not set in .env."""
    from .secretrefs import make_ref
    environ = os.environ if environ is None else environ
    walls: dict[str, list[dict]] = {}
    for inv in inventories:
        path = re.sub(r"^https?://[^/]+", "", inv.get("url", "")).split("?")[0].split("#")[0] or "/"
        walls.setdefault(path, []).extend(inv.get("auth") or [])
    missing: list[str] = []
    for step in steps:
        if step.get("kind") != "fill":
            continue
        page = (step.get("page") or "").split("?")[0].split("#")[0] or "/"
        selector, name = step.get("selector") or "", _fold(step.get("name") or "")
        for wall in walls.get(page, []):
            for index, fld in enumerate(wall["fields"]):
                attr = fld.get("name") or ""
                same = ((attr and (f'name="{attr}"' in selector or f"name='{attr}'" in selector or selector == f"#{attr}"))
                        or (name and name in {_fold(fld.get("label")), _fold(attr)}))
                if not same:
                    continue
                variable = _env_name(site, account, field_key(index, fld))
                if not environ.get(variable):
                    missing.append(variable)
                else:
                    step["value"] = make_ref(variable)
                break
    if missing:
        return "typing into a login form needs details that are not set in .env: " + ", ".join(dict.fromkeys(missing))
    return ""


# ------------------------------------------------------------------ where a sign-in lands (so the crawl can start there)

def landing_path(settings) -> Path:
    return settings.workspace / "auth" / f"landing.{_account(settings)}.txt"


def save_landing(settings, landed_url: str, login_url: str) -> None:
    """Remember the page a successful sign-in ended on. It is not a link anywhere on the public site, so without this
    nothing would ever discover the logged-in area."""
    def key(u: str) -> str:
        return (u or "").split("#")[0].rstrip("/")
    if not landed_url or key(landed_url) == key(login_url):
        return
    path = landing_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(landed_url + "\n", encoding="utf-8")


def landing_urls(settings) -> list[str]:
    if not hasattr(settings, "workspace") or not landing_path(settings).is_file():
        return []
    return [line.strip() for line in landing_path(settings).read_text(encoding="utf-8").splitlines() if line.strip()]
