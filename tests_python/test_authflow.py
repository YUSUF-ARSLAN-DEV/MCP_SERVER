import subprocess
from types import SimpleNamespace

import pytest

from website_test_pipeline.authflow import (
    authenticate_wall, ensure_ignored, has_session, perform_auth, remember_credentials, save_session, session_path,
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
    env.write_text("SEED_URL=https://x.test\nAUTH_SAT_STG_ALJAZEERA_TV_PW=old\n", encoding="utf-8")
    wall = {"fields": [{"name": "email", "type": "email"}, {"name": "pw", "type": "password"}]}
    names = remember_credentials(env, repo, "sat-stg.aljazeera.tv", wall, AuthAnswer({"email": "a@b.c", "pw": "new"}, True))
    assert names == ["AUTH_SAT_STG_ALJAZEERA_TV_EMAIL", "AUTH_SAT_STG_ALJAZEERA_TV_PW"]
    text = env.read_text(encoding="utf-8")
    assert "SEED_URL=https://x.test" in text and "PW=old" not in text
    assert "AUTH_SAT_STG_ALJAZEERA_TV_PW=new" in text and "AUTH_SAT_STG_ALJAZEERA_TV_EMAIL=a@b.c" in text
