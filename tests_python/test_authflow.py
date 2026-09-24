import subprocess
from types import SimpleNamespace

import pytest

from website_test_pipeline.authflow import (
    authenticate_wall, context_kwargs, credentials_from_env, ensure_ignored, ensure_session, env_names, has_session,
    perform_auth, remember_credentials, save_session, session_path,
)
from website_test_pipeline.authpopup import AuthAnswer
from website_test_pipeline.explorer import _detect_auth

LOGIN = ('<main><h1>Sign in</h1>{error}<form id="signin" method="post" action="/session">'
         '<label for="e">Email</label><input id="e" name="email" type="email">'
         '<label for="p">Password</label><input id="p" name="pw" type="password">'
         '<button>Sign in</button></form></main>')
DASHBOARD = "<main><h1>Your account</h1><a href='/out'>Leave</a></main>"


def _site(route):
    request = route.request
    if request.method == "POST":
        if "pw=right" in (request.post_data or ""):
            route.fulfill(status=200, content_type="text/html", body=DASHBOARD)
        else:
            route.fulfill(status=200, content_type="text/html", body=LOGIN.format(error='<p class="error">Wrong password</p>'))
    else:
        route.fulfill(status=200, content_type="text/html", body=LOGIN.format(error=""))


@pytest.fixture(scope="module")
def browser():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium here: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    context.route("https://fake.test/**", _site)
    pg = context.new_page()
    pg.goto("https://fake.test/login")
    yield pg
    context.close()


def _wall(page):
    walls = _detect_auth(page, "https://fake.test/login")
    assert len(walls) == 1 and walls[0]["kind"] == "login"
    return walls[0]


def test_the_right_password_signs_in_and_the_form_goes_away(page):
    outcome = perform_auth(page, _wall(page), AuthAnswer({"email": "a@b.c", "pw": "right"}), settle_ms=100)
    assert outcome.ok and outcome.signals["password_form_gone"] and not outcome.signals["error_shown"]


def test_the_wrong_password_fails_and_reports_the_sites_own_error(page):
    outcome = perform_auth(page, _wall(page), AuthAnswer({"email": "a@b.c", "pw": "nope"}), settle_ms=100)
    assert not outcome.ok
    assert outcome.error_text == "Wrong password"
    assert not outcome.signals["password_form_gone"]


def test_a_failed_attempt_is_retried_with_the_error_shown_and_counted(page):
    requests, answers = [], iter([AuthAnswer({"email": "a@b.c", "pw": "nope"}), AuthAnswer({"email": "a@b.c", "pw": "right"})])

    def ask(request, screenshot):
        requests.append(request)
        assert screenshot()                     # the mirror gets real bytes
        return next(answers)

    outcome, answer = authenticate_wall(page, _wall(page), "https://fake.test/login", ask=ask)
    assert outcome.ok and outcome.attempts == 2 and answer.values["pw"] == "right"
    assert requests[0].notice == ""
    assert 'the site says "Wrong password"' in requests[1].notice


def test_skipping_returns_nothing(page):
    assert authenticate_wall(page, _wall(page), "https://fake.test/login", ask=lambda r, s: None) == (None, None)


def test_giving_up_after_the_attempt_limit_returns_no_answer(page):
    outcome, answer = authenticate_wall(page, _wall(page), "https://fake.test/login", max_attempts=2,
                                        ask=lambda r, s: AuthAnswer({"email": "a@b.c", "pw": "nope"}))
    assert answer is None and not outcome.ok and outcome.attempts == 2


# ------------------------------------------------------------------ secrets stay out of git

def _repo(tmp_path, gitignore=""):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    return tmp_path


def test_ensure_ignored_adds_a_rule_when_git_would_track_the_file(tmp_path):
    repo = _repo(tmp_path)
    target = repo / "site" / "auth" / "state.json"
    target.parent.mkdir(parents=True)
    assert ensure_ignored(target, repo) is True
    assert "site/auth/state.json" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_ensure_ignored_leaves_gitignore_alone_when_already_covered(tmp_path):
    repo = _repo(tmp_path, "runs/\n")
    assert ensure_ignored(repo / "runs" / "x" / "state.json", repo) is True
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "runs/\n"


def test_save_session_writes_an_ignored_file_under_the_workspace(tmp_path, page):
    repo = _repo(tmp_path, "runs/\n")
    settings = SimpleNamespace(root=repo, workspace=repo / "runs" / "fake.test")
    assert not has_session(settings)
    path = save_session(page.context, settings)
    assert path == session_path(settings) and path.is_file() and has_session(settings)


def test_save_session_refuses_when_git_cannot_ignore_it(tmp_path, page):
    settings = SimpleNamespace(root=tmp_path, workspace=tmp_path / "runs" / "x")      # not a git repo
    with pytest.raises(RuntimeError, match="git would not ignore"):
        save_session(page.context, settings)
    assert not has_session(settings)


def test_remember_credentials_writes_named_variables_and_replaces_older_ones(tmp_path):
    repo = _repo(tmp_path, ".env\n")
    env = repo / ".env"
    env.write_text("SEED_URL=https://x.test\nAUTH_SAT_STG_ALJAZEERA_TV_ADMIN_PW=old\n", encoding="utf-8")
    wall = {"fields": [{"name": "email", "type": "email"}, {"name": "pw", "type": "password"}]}
    names = remember_credentials(env, repo, "sat-stg.aljazeera.tv", wall, AuthAnswer({"email": "a@b.c", "pw": "new"}, True), "admin")
    assert names == ["AUTH_SAT_STG_ALJAZEERA_TV_ADMIN_EMAIL", "AUTH_SAT_STG_ALJAZEERA_TV_ADMIN_PW"]
    text = env.read_text(encoding="utf-8")
    assert "SEED_URL=https://x.test" in text and "PW=old" not in text
    assert "AUTH_SAT_STG_ALJAZEERA_TV_ADMIN_PW=new" in text and "AUTH_SAT_STG_ALJAZEERA_TV_ADMIN_EMAIL=a@b.c" in text


# ------------------------------------------------------------------ accounts and .env

WALL = {"fields": [{"name": "email", "type": "email", "required": True}, {"name": "pw", "type": "password", "required": True}]}


def test_two_accounts_of_one_site_get_their_own_variable_names():
    admin = env_names("x.test", "admin", WALL)
    customer = env_names("x.test", "customer", WALL)
    assert list(admin) == ["AUTH_X_TEST_ADMIN_EMAIL", "AUTH_X_TEST_ADMIN_PW"]
    assert not set(admin) & set(customer)


def test_credentials_from_env_needs_every_required_field_and_password():
    env = {"AUTH_X_TEST_ADMIN_EMAIL": "a@b.c", "AUTH_X_TEST_ADMIN_PW": "pw1", "AUTH_X_TEST_CUSTOMER_EMAIL": "c@b.c"}
    assert credentials_from_env("x.test", "admin", WALL, env).values == {"email": "a@b.c", "pw": "pw1"}
    assert credentials_from_env("x.test", "customer", WALL, env) is None          # password missing
    assert credentials_from_env("x.test", "nobody", WALL, env) is None


def test_the_session_file_and_context_arguments_are_per_account(tmp_path):
    a = SimpleNamespace(root=tmp_path, workspace=tmp_path / "w", auth_account="admin")
    c = SimpleNamespace(root=tmp_path, workspace=tmp_path / "w", auth_account="customer")
    assert session_path(a).name == "state.admin.json" and session_path(c).name == "state.customer.json"
    assert context_kwargs(a) == {}
    session_path(a).parent.mkdir(parents=True)
    session_path(a).write_text("{}", encoding="utf-8")
    assert context_kwargs(a) == {"storage_state": str(session_path(a))} and context_kwargs(c) == {}


# ------------------------------------------------------------------ ensure_session: session, then .env, then the popup

class _Routed:
    """A browser whose every new context serves the fake site, which remembers a signed-in visitor by cookie."""
    def __init__(self, browser, sign_in_ok=True):
        self.browser, self.sign_in_ok = browser, sign_in_ok

    def new_context(self, **kwargs):
        context = self.browser.new_context(**kwargs)
        context.route("https://fake.test/**", self._serve)
        return context

    def _serve(self, route):
        request = route.request
        signed_in = "sid=1" in (request.headers.get("cookie") or "")
        if request.method == "POST" and "pw=right" in (request.post_data or ""):
            route.fulfill(status=200, content_type="text/html", body=DASHBOARD, headers={"set-cookie": "sid=1; Path=/"})
        elif request.method == "POST":
            route.fulfill(status=200, content_type="text/html", body=LOGIN.format(error='<p class="error">Wrong password</p>'))
        elif signed_in and self.sign_in_ok:
            route.fulfill(status=200, content_type="text/html", body=DASHBOARD)
        else:
            route.fulfill(status=200, content_type="text/html", body=LOGIN.format(error=""))


def _settings(tmp_path, **over):
    repo = _repo(tmp_path, "runs/\n.env\n")
    base = dict(auth_mode="auto", auth_account="default", site="fake.test", root=repo, workspace=repo / "runs" / "fake.test",
                navigation_timeout_ms=15000)
    base.update(over)
    return SimpleNamespace(**base)


URL = "https://fake.test/login"


def test_a_page_without_a_wall_needs_nothing(browser, tmp_path):
    class Plain(_Routed):
        def _serve(self, route):
            route.fulfill(status=200, content_type="text/html", body="<main><h1>Public</h1></main>")
    assert ensure_session(_settings(tmp_path), Plain(browser), URL, interactive=False).status == "no-wall"


def test_auth_mode_none_never_touches_the_site(browser, tmp_path):
    assert ensure_session(_settings(tmp_path, auth_mode="none"), None, URL).status == "off"


def test_details_in_env_sign_in_silently_and_the_session_is_then_reused(browser, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_PW", "right")
    boom = lambda *a, **k: pytest.fail("the popup must not open when .env has the details")
    first = ensure_session(settings, _Routed(browser), URL, ask=boom, interactive=False)
    assert first.status == "signed-in-env" and has_session(settings)
    assert ensure_session(settings, _Routed(browser), URL, ask=boom, interactive=False).status == "session-ok"


def test_a_stale_session_is_replaced_from_env(browser, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_PW", "right")
    assert ensure_session(settings, _Routed(browser), URL, interactive=False).status == "signed-in-env"
    expired = _Routed(browser, sign_in_ok=False)              # the site stops honouring the cookie
    assert ensure_session(settings, expired, URL, interactive=False).status == "signed-in-env"


def test_no_details_and_no_display_leaves_the_wall_untested_and_names_the_variables(browser, tmp_path, monkeypatch):
    for name in ("AUTH_FAKE_TEST_DEFAULT_EMAIL", "AUTH_FAKE_TEST_DEFAULT_PW"):
        monkeypatch.delenv(name, raising=False)
    lines = []
    log = SimpleNamespace(info=lambda fmt, *a: lines.append(fmt % a))
    result = ensure_session(_settings(tmp_path), _Routed(browser), URL, log=log, interactive=False)
    assert result.status == "not-tested"
    assert any("AUTH_FAKE_TEST_DEFAULT_EMAIL" in l and "AUTH_FAKE_TEST_DEFAULT_PW" in l for l in lines)


def test_refused_env_details_fall_back_to_the_popup_and_can_be_remembered(browser, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_PW", "stale-password")
    popup = lambda request, screenshot: AuthAnswer({"email": "a@b.c", "pw": "right"}, remember=True)
    result = ensure_session(settings, _Routed(browser), URL, ask=popup, interactive=True)
    assert result.status == "signed-in-popup" and has_session(settings)
    assert "AUTH_FAKE_TEST_DEFAULT_PW=right" in (settings.root / ".env").read_text(encoding="utf-8")


def test_an_expired_session_with_nothing_to_renew_it_is_a_clear_failure(browser, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_PW", "right")
    assert ensure_session(settings, _Routed(browser), URL, interactive=False).status == "signed-in-env"
    monkeypatch.delenv("AUTH_FAKE_TEST_DEFAULT_EMAIL")
    monkeypatch.delenv("AUTH_FAKE_TEST_DEFAULT_PW")
    result = ensure_session(settings, _Routed(browser, sign_in_ok=False), URL, interactive=False)
    assert result.status == "failed" and "AUTH_FAKE_TEST_DEFAULT_EMAIL" in result.detail


def test_preflight_lets_a_run_continue_unless_the_login_truly_cannot_be_renewed(tmp_path, monkeypatch):
    from website_test_pipeline import authflow
    log = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    off = SimpleNamespace(auth_mode="none", seed_url="https://x.test")
    assert authflow.preflight_session(off, log) == 0                         # AUTH_MODE=none: never checks
    assert authflow.preflight_session(SimpleNamespace(auth_mode="auto", seed_url=""), log) == 0
    on = SimpleNamespace(auth_mode="auto", seed_url="https://x.test", headless=True)
    for status, expected in (("no-wall", 0), ("session-ok", 0), ("signed-in-env", 0), ("not-tested", 0),
                             ("failed", 2), ("skipped", 2)):
        monkeypatch.setattr(authflow, "ensure_session", lambda *a, _s=status, **k: authflow.SessionResult(_s))
        monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _FakePW())
        assert authflow.preflight_session(on, log) == expected, status


class _FakePW:
    def __enter__(self):
        return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **k: SimpleNamespace(close=lambda: None)))

    def __exit__(self, *a):
        return False


# ------------------------------------------------------------------ the post-login page is remembered for the crawl

def test_the_page_a_sign_in_ends_on_is_remembered_so_the_crawl_can_start_there(browser, tmp_path, monkeypatch):
    from website_test_pipeline.authflow import landing_urls
    settings = _settings(tmp_path)
    assert landing_urls(settings) == []
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_PW", "right")
    assert ensure_session(settings, _Routed(browser), URL, interactive=False).status == "signed-in-env"
    assert landing_urls(settings) == ["https://fake.test/session"]


def test_a_sign_in_that_stays_on_the_login_page_records_no_landing(tmp_path):
    from website_test_pipeline.authflow import landing_urls, save_landing
    settings = _settings(tmp_path)
    save_landing(settings, "https://fake.test/login#top", "https://fake.test/login")
    save_landing(settings, "", "https://fake.test/login")
    assert landing_urls(settings) == []
    assert landing_urls(SimpleNamespace()) == []                             # settings without a workspace


# ------------------------------------------------------------------ a wall deeper in the site, met while crawling

class _GatedSite:
    """/ is public; /account shows a login form until the visitor is signed in, then links to /orders."""
    def __init__(self, browser):
        self.browser = browser

    def page(self):
        context = self.browser.new_context()
        context.route("https://fake.test/**", self._serve)
        return context.new_page()

    def _serve(self, route):
        request, path = route.request, route.request.url.replace("https://fake.test", "").split("?")[0]
        signed_in = "sid=1" in (request.headers.get("cookie") or "")
        html = lambda body, headers=None: route.fulfill(status=200, content_type="text/html", body=body, headers=headers or {})
        if request.method == "POST":
            if "pw=right" in (request.post_data or ""):
                html("<main><h1>Account</h1><a href='/orders'>Orders</a></main>", {"set-cookie": "sid=1; Path=/"})
            else:
                html(LOGIN.format(error='<p class="error">Wrong password</p>'))
        elif path == "/":
            html("<main><h1>Home</h1><a href='/account'>My account</a></main>")
        elif path == "/account":
            html("<main><h1>Account</h1><a href='/orders'>Orders</a></main>" if signed_in else LOGIN.format(error=""))
        else:
            html("<main><h1>Orders</h1></main>")


def _crawl(site, on_page=None):
    from website_test_pipeline.crawler import crawl
    return crawl(site.page(), "https://fake.test/", 3, 20, on_page=on_page)


def _creds(monkeypatch):
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_EMAIL", "a@b.c")
    monkeypatch.setenv("AUTH_FAKE_TEST_DEFAULT_PW", "right")


def test_without_the_hook_a_crawl_never_sees_the_area_behind_the_login(browser):
    urls = _crawl(_GatedSite(browser))
    assert "https://fake.test/account" in urls and "https://fake.test/orders" not in urls


def test_the_crawl_signs_in_when_it_meets_a_wall_and_then_finds_what_is_behind_it(browser, tmp_path, monkeypatch):
    from website_test_pipeline.authflow import wall_handler
    _creds(monkeypatch)
    settings = _settings(tmp_path)
    urls = _crawl(_GatedSite(browser), wall_handler(settings, interactive=False))
    assert "https://fake.test/orders" in urls
    assert has_session(settings)                                             # kept for explore / verify / the tests


def test_the_crawl_acts_once_and_never_when_a_login_was_already_handled_or_auth_is_off(browser, tmp_path, monkeypatch):
    from website_test_pipeline.authflow import wall_handler
    _creds(monkeypatch)
    for kwargs, settings_over in (({"already_signed_in": True}, {}), ({}, {"auth_mode": "none"})):
        folder = tmp_path / str(len(kwargs))
        folder.mkdir()
        settings = _settings(folder, **settings_over)
        urls = _crawl(_GatedSite(browser), wall_handler(settings, interactive=False, **kwargs))
        assert "https://fake.test/orders" not in urls and not has_session(settings)


def test_a_wall_with_no_details_and_no_display_is_left_alone_and_the_crawl_carries_on(browser, tmp_path, monkeypatch):
    from website_test_pipeline.authflow import wall_handler
    for name in ("AUTH_FAKE_TEST_DEFAULT_EMAIL", "AUTH_FAKE_TEST_DEFAULT_PW"):
        monkeypatch.delenv(name, raising=False)
    settings = _settings(tmp_path)
    urls = _crawl(_GatedSite(browser), wall_handler(settings, interactive=False))
    assert "https://fake.test/account" in urls and "https://fake.test/orders" not in urls


def test_a_failing_page_hook_never_stops_the_crawl():
    from website_test_pipeline.crawler import crawl

    class _Loc:
        def evaluate_all(self, js): return []

    class _P:
        def __init__(self): self.gotos = []
        def set_default_navigation_timeout(self, ms): pass
        def goto(self, url, **kw): self.gotos.append(url)
        def locator(self, sel): return _Loc()

    page = _P()
    assert crawl(page, "https://x.test/", 1, 5, on_page=lambda p, u: 1 / 0) == ["https://x.test/"]
    page = _P()
    crawl(page, "https://x.test/", 1, 5, on_page=lambda p, u: True)
    assert page.gotos == ["https://x.test/", "https://x.test/"]              # True means: load it again, signed in
