import io

import pytest
from PIL import Image

from website_test_pipeline.authpopup import (
    AuthAnswer, AuthRequest, CredentialPopup, field_guidance, field_key, should_ask,
)
from website_test_pipeline.config import Settings

LOGIN_FIELDS = [
    {"type": "email", "name": "email", "label": "Email", "required": True},
    {"type": "password", "name": None, "label": "كلمة المرور", "autocomplete": "current-password", "required": True},
]


def test_guidance_is_plain_language_and_quotes_the_pages_own_label():
    assert field_guidance(LOGIN_FIELDS[0], "login") == 'The email or username you sign in with. The page calls it "Email". Required.'
    arabic = field_guidance(LOGIN_FIELDS[1], "login")
    assert arabic.startswith("Your password for this site.") and "كلمة المرور" in arabic


def test_guidance_changes_wording_for_a_signup_and_for_unlabelled_fields():
    assert "Choose a password" in field_guidance({"type": "password"}, "signup")
    assert "Choose a password" in field_guidance({"type": "password", "autocomplete": "new-password"}, "login")
    assert field_guidance({"type": "text"}, "login") == "Type what the site asks for here."


def test_field_key_falls_back_to_position_when_a_field_has_no_name():
    assert field_key(0, {"name": "email"}) == "email"
    assert field_key(3, {"name": None}) == "field3"


@pytest.mark.parametrize("mode,kind,session,interactive,ask", [
    ("auto", "login", False, True, True),
    ("auto", "signup", False, True, True),
    ("none", "login", False, True, False),
    ("auto", "login", True, True, False),
    ("auto", "login", False, False, False),
    ("auto", "newsletter", False, True, False),
])
def test_should_ask_only_for_an_unauthenticated_password_wall_in_an_interactive_run(mode, kind, session, interactive, ask):
    decision, reason = should_ask(mode, kind, session, interactive)
    assert decision is ask and reason


def test_auth_mode_defaults_to_auto_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert Settings().auth_mode == "auto"
    monkeypatch.setenv("AUTH_MODE", "NONE")
    assert Settings().auth_mode == "none"
    monkeypatch.setenv("AUTH_MODE", "always")
    with pytest.raises(ValueError):
        Settings()


def _tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception as exc:                      # no display on this machine
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.withdraw()
    return root


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (900, 560), "white").save(buf, "PNG")
    return buf.getvalue()


def test_popup_returns_what_was_typed_and_masks_the_password():
    root = _tk_root()
    try:
        popup = CredentialPopup(root, AuthRequest("https://x.test/login", "login", LOGIN_FIELDS), _png, timeout_s=60)
        assert popup.entries["email"].cget("show") == ""
        assert popup.entries["field1"].cget("show") == "*"
        popup.entries["email"].insert(0, "me@x.test")
        popup.entries["field1"].insert(0, "s3cret")
        popup.remember.set(True)
        popup.submit()
        assert popup.result == AuthAnswer({"email": "me@x.test", "field1": "s3cret"}, True)
    finally:
        root.destroy()


def test_skipping_and_running_out_of_time_both_mean_no_credentials():
    root = _tk_root()
    try:
        popup = CredentialPopup(root, AuthRequest("https://x.test/login", "login", LOGIN_FIELDS), None, timeout_s=0)
        assert popup.result is None                # timeout_s=0 skipped on the first tick
        popup.entries["email"].insert(0, "typed but skipped")
        popup.skip()
        assert popup.result is None
    finally:
        root.destroy()
