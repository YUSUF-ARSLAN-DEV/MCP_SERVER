import ast

import pytest

from website_test_pipeline import secretrefs
from website_test_pipeline.authflow import account_available, bind_credentials
from website_test_pipeline.flowgen import emit_flow_spec
from website_test_pipeline.intents import prompt_for, RULES, unsuitable_reason
from website_test_pipeline.secretrefs import MissingSecret, is_ref, make_ref, needs_fresh_session, ref_name, resolve

START = "https://x.test/login"
USER_VAR, PASS_VAR = "AUTH_X_TEST_DEFAULT_USERNAME", "AUTH_X_TEST_DEFAULT_PASSWORD"
WALL = {"kind": "login", "group": 0, "selector": "#login", "fields": [
    {"type": "text", "name": "username", "label": "Username", "required": True},
    {"type": "password", "name": "password", "label": "Password", "required": True}]}
INVENTORY = {"url": START, "auth": [WALL], "headings": [], "forms": [], "embeds": [], "revealed": [], "accessibility": "",
             "controls": [{"tag": "input", "role": "text", "name": "Username", "selector": "#username", "hidden": False},
                          {"tag": "input", "role": "password", "name": "Password", "selector": "#password", "hidden": False},
                          {"tag": "button", "role": "button", "name": "Login", "selector": None, "hidden": False}]}


# ------------------------------------------------------------------ references

def test_a_reference_names_the_variable_and_only_a_reference_is_one():
    assert make_ref(PASS_VAR) == "{env:AUTH_X_TEST_DEFAULT_PASSWORD}"
    assert ref_name("{env:AUTH_X_TEST_DEFAULT_PASSWORD}") == PASS_VAR
    for plain in ("hunter2", "{env:lower}", "{env:}", None, "x {env:A}"):
        assert not is_ref(plain)


def test_resolve_reads_the_environment_and_refuses_a_missing_variable():
    assert resolve(make_ref(PASS_VAR), {PASS_VAR: "s3cret"}) == "s3cret"
    assert resolve("plain text", {}) == "plain text"
    with pytest.raises(MissingSecret, match=PASS_VAR):
        resolve(make_ref(PASS_VAR), {})


def test_only_a_flow_that_signs_in_needs_a_fresh_session():
    assert needs_fresh_session({"steps": [{"kind": "fill", "value": make_ref(USER_VAR)}]})
    assert not needs_fresh_session({"steps": [{"kind": "fill", "value": "tomsmith"}, {"kind": "click"}]})


# ------------------------------------------------------------------ binding the login fields

def _steps():
    return [{"kind": "fill", "page": "/login", "selector": "#username", "name": "Username", "value": None},
            {"kind": "fill", "page": "/login", "selector": "#password", "name": "Password", "value": None},
            {"kind": "click", "page": "/login", "selector": None, "name": "Login"}]


def test_login_fields_get_references_when_the_details_are_in_env():
    steps = _steps()
    assert bind_credentials(steps, [INVENTORY], "x.test", "default", {USER_VAR: "u", PASS_VAR: "p"}) == ""
    assert steps[0]["value"] == make_ref(USER_VAR) and steps[1]["value"] == make_ref(PASS_VAR)
    assert steps[2].get("value") is None                                  # the button is untouched
    assert "u" not in {steps[0]["value"], steps[1]["value"]}               # a reference, never the value


def test_a_login_flow_cannot_be_built_without_the_details_and_names_what_to_set():
    steps = _steps()
    reason = bind_credentials(steps, [INVENTORY], "x.test", "default", {USER_VAR: "u"})
    assert PASS_VAR in reason and USER_VAR not in reason and steps[1]["value"] is None


def test_ordinary_forms_are_left_alone():
    steps = [{"kind": "fill", "page": "/search", "selector": "#q", "name": "Search", "value": "acme"}]
    assert bind_credentials(steps, [INVENTORY], "x.test", "default", {}) == "" and steps[0]["value"] == "acme"


def test_a_second_account_uses_its_own_variables():
    steps = _steps()
    env = {"AUTH_X_TEST_ADMIN_USERNAME": "a", "AUTH_X_TEST_ADMIN_PASSWORD": "b"}
    assert bind_credentials(steps, [INVENTORY], "x.test", "admin", env) == ""
    assert ref_name(steps[1]["value"]) == "AUTH_X_TEST_ADMIN_PASSWORD"


# ------------------------------------------------------------------ the generated test

def _login_flow():
    return {"id": "x-test-login--sign-in", "goal": "A visitor signs in with the saved account and reaches the secure area.",
            "source": "intent", "status": "verified", "start_url": START,
            "steps": [{"kind": "fill", "selector": "#username", "name": "Username", "value": make_ref(USER_VAR)},
                      {"kind": "fill", "selector": "#password", "name": "Password", "value": make_ref(PASS_VAR)},
                      {"kind": "click", "selector": None, "name": "Login"}],
            "outcome": {"effect": "navigates", "to": "/secure"},
            "observed": {"effect": "navigates", "url": "https://x.test/secure", "new_headings": ["Secure Area"],
                         "new_controls": [], "results": [],
                         "step_effects": ["no-visible-change", "no-visible-change", "navigates"],
                         "step_urls": [START, START, "https://x.test/secure"]}}


def test_a_sign_in_flow_becomes_a_spec_that_reads_env_and_starts_logged_out():
    source, reason = emit_flow_spec(_login_flow(), [INVENTORY])
    assert reason == "", reason
    ast.parse(source)
    assert f"secret('{PASS_VAR}')" in source and f"secret('{USER_VAR}')" in source
    assert "import os" not in source and "from website_test_pipeline.secretrefs import secret" in source
    assert "logged_out_page" in source and "page = logged_out_page" in source
    assert "{env:" not in source                                           # the placeholder never reaches the test
    assert 'not_to_have_value("")' in source                               # nothing compares against a secret


def test_a_flow_without_a_login_is_emitted_exactly_as_before():
    flow = _login_flow()
    flow["steps"] = [{"kind": "click", "selector": None, "name": "Login"}]
    flow["observed"]["step_effects"], flow["observed"]["step_urls"] = ["navigates"], ["https://x.test/secure"]
    source, reason = emit_flow_spec(flow, [INVENTORY])
    assert reason == "" and "logged_out_page" not in source and "import os" not in source and "(page: Page," in source


# ------------------------------------------------------------------ what the AI may write once an account exists

def test_signing_in_is_allowed_only_when_an_account_exists_and_payment_never_is():
    sentence = "A visitor enters a username and password to log in and reaches the secure area."
    assert unsuitable_reason(sentence) == "needs credentials or payment details"
    assert unsuitable_reason(sentence, allow_account=True) == ""
    assert unsuitable_reason("A visitor logs in and pays by credit card for a plan.", allow_account=True) == "needs payment details"


def test_the_prompt_lets_the_ai_sign_in_only_when_an_account_exists():
    plain, allowed = prompt_for("MAP", [], "", False), prompt_for("MAP", [], "", True)
    assert "Never a journey that needs an account" in plain and "signs in with the saved account" not in plain
    assert "signs in with the saved account" in allowed and "never a username, email, password" in allowed
    assert RULES in plain


def test_account_available_needs_a_session_or_env_details_and_respects_auth_mode(tmp_path, monkeypatch):
    from types import SimpleNamespace
    settings = SimpleNamespace(auth_mode="auto", auth_account="default", site="x.test", workspace=tmp_path / "w", root=tmp_path)
    monkeypatch.delenv(USER_VAR, raising=False)
    monkeypatch.delenv(PASS_VAR, raising=False)
    assert not account_available(settings, [INVENTORY])
    monkeypatch.setenv(USER_VAR, "u")
    monkeypatch.setenv(PASS_VAR, "p")
    assert account_available(settings, [INVENTORY])
    assert not account_available(SimpleNamespace(**{**vars(settings), "auth_mode": "none"}), [INVENTORY])
    monkeypatch.delenv(USER_VAR)
    monkeypatch.delenv(PASS_VAR)
    (tmp_path / "w" / "auth").mkdir(parents=True)
    (tmp_path / "w" / "auth" / "state.default.json").write_text("{}", encoding="utf-8")
    assert account_available(settings, [])                                  # a saved session is enough


# ------------------------------------------------------------------ the sign-in edge in the site map

def test_the_site_map_links_the_login_page_to_the_page_a_sign_in_lands_on(tmp_path):
    from types import SimpleNamespace
    from website_test_pipeline.authflow import login_edges, save_landing
    from website_test_pipeline.sitemap import build_site_map
    settings = SimpleNamespace(workspace=tmp_path / "w", auth_account="default")
    inventories = [{**INVENTORY, "url": "https://x.test/"}, {"url": "https://x.test/inventory.html", "controls": [], "headings": [],
                                                              "forms": [], "revealed": [], "embeds": [], "accessibility": ""}]
    assert login_edges(settings, inventories) == []                             # nothing recorded yet
    save_landing(settings, "https://x.test/inventory.html", "https://x.test/")
    edges = login_edges(settings, inventories)
    assert edges == [{"from": "/", "to": "/inventory.html", "via": "sign in", "explored": True}]
    site_map = build_site_map(inventories, edges)
    assert {"from": "/", "to": "/inventory.html", "via": "sign in", "explored": True} in site_map["edges"]
    assert build_site_map(inventories)["edges"] == []                           # without it the pages look unconnected
