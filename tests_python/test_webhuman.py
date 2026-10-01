import json
import threading
import time
from pathlib import Path

import pytest

from website_test_pipeline import authpopup, humanstep, webhuman
from website_test_pipeline.authflow import ensure_ignored
from website_test_pipeline.authpopup import AuthRequest
from website_test_pipeline.humanstep import HumanRequest

PNG = b"\x89PNG-fake-image"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HUMAN_CHANNEL", "web")
    monkeypatch.setenv("WTP_HUMAN_DIR", str(tmp_path / "human"))
    return tmp_path / "human"


def person(folder: Path, payload: dict, seq: int = 1, before_answer=None):
    """A person on the web page: waits for request <seq>, then answers it."""
    def run():
        request = folder / f"request-{seq}.json"
        while not request.is_file():
            time.sleep(0.02)
        if before_answer:
            before_answer(folder)
        (folder / f"answer-{seq}.json").write_text(json.dumps(payload), encoding="utf-8")
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_the_web_channel_is_only_on_when_asked_for_and_given_a_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("HUMAN_CHANNEL", raising=False)
    monkeypatch.setenv("WTP_HUMAN_DIR", str(tmp_path))
    assert webhuman.active() is False
    monkeypatch.setenv("HUMAN_CHANNEL", "web")
    monkeypatch.delenv("WTP_HUMAN_DIR")
    assert webhuman.active() is False
    monkeypatch.setenv("WTP_HUMAN_DIR", str(tmp_path))
    assert webhuman.active() is True


def test_a_person_on_the_web_page_counts_as_someone_there(folder):
    assert authpopup.is_interactive() is True
    assert humanstep.can_ask() == (True, "a person can answer on the web page")


def test_a_captcha_is_asked_with_the_prefilled_form_and_the_answer_comes_back(folder):
    fields = [{"selector": "#name", "label": "Name", "type": "text"},
              {"selector": "#country", "label": "Country", "type": "select", "options": ["Pick", "Egypt"], "placeholder": "Pick"}]
    person(folder, {"action": "submit", "code": " Xk3 9 ", "values": {"#name": "Sam", "#country": "Egypt"}})
    answer = humanstep.ask_code(HumanRequest("https://x.test/form", "the code", fields=fields), lambda: PNG)

    assert (answer.code, answer.values) == ("Xk3 9", {"#name": "Sam", "#country": "Egypt"})
    request = json.loads((folder / "request-1.json").read_text(encoding="utf-8"))
    assert request["kind"] == "captcha" and request["label"] == "the code" and request["has_image"] is True
    assert [f["default"] for f in request["fields"]] == ["Test", "Egypt"]
    assert (folder / "request-1.png").read_bytes() == PNG
    assert json.loads((folder / "closed-1.json").read_text(encoding="utf-8")) == {"outcome": "submitted"}
    assert not (folder / "answer-1.json").exists()                 # what a person typed is read once, then gone


def test_skipping_returns_nothing(folder):
    person(folder, {"action": "skip"})
    assert humanstep.ask_code(HumanRequest("https://x.test/", "code")) is None
    assert json.loads((folder / "closed-1.json").read_text(encoding="utf-8")) == {"outcome": "skipped"}


def test_a_submit_without_a_code_is_a_skip(folder):
    person(folder, {"action": "submit", "code": "  ", "values": {}})
    assert humanstep.ask_code(HumanRequest("https://x.test/", "code")) is None


def test_nobody_answering_times_out_and_says_so(folder):
    assert webhuman.ask_code_web(HumanRequest("https://x.test/", "code"), timeout=1) is None
    assert json.loads((folder / "closed-1.json").read_text(encoding="utf-8")) == {"outcome": "timeout"}


def test_asking_for_a_different_image_reloads_the_captcha_and_refreshes_the_picture(folder):
    reloads, frames = [], iter([b"first", b"second", b"third", b"fourth", b"fifth"])
    def press_refresh_then_answer_later(d):
        (d / "refresh-1.flag").write_text("")
        time.sleep(1.3)                                   # long enough for the run to notice the button first

    person(folder, {"action": "submit", "code": "abc"}, before_answer=press_refresh_then_answer_later)
    answer = webhuman.ask_code_web(HumanRequest("https://x.test/", "code"), lambda: next(frames), lambda: reloads.append(1))
    assert answer.code == "abc" and reloads == [1]
    assert (folder / "request-1.png").read_bytes() != b"first"


def test_a_half_written_answer_is_waited_for_not_crashed_on(folder):
    def half(d):
        (d / "answer-1.json").write_text('{"action": "sub', encoding="utf-8")
    person(folder, {"action": "submit", "code": "ok"}, before_answer=half)
    assert webhuman.ask_code_web(HumanRequest("https://x.test/", "code")).code == "ok"


def test_each_request_gets_the_next_number(folder):
    person(folder, {"action": "skip"}, seq=1)
    humanstep.ask_code(HumanRequest("https://x.test/", "code"))
    person(folder, {"action": "skip"}, seq=2)
    humanstep.ask_code(HumanRequest("https://x.test/", "code"))
    assert (folder / "request-2.json").is_file() and (folder / "closed-2.json").is_file()


def test_sign_in_details_are_asked_with_plain_guidance_and_never_remembered(folder):
    fields = [{"type": "email", "name": "email", "label": "Email", "required": True},
              {"type": "password", "name": "", "label": "Password", "required": True}]
    person(folder, {"action": "submit", "values": {"email": "a@b.test", "field1": "pw"}, "remember": True})
    answer = authpopup.ask_credentials(AuthRequest("https://x.test/login", "login", fields, "That did not work."), lambda: PNG)

    assert answer.values == {"email": "a@b.test", "field1": "pw"} and answer.remember is False
    request = json.loads((folder / "request-1.json").read_text(encoding="utf-8"))
    assert request["kind"] == "credentials" and request["wall_kind"] == "login" and request["notice"] == "That did not work."
    assert [(f["key"], f["type"]) for f in request["fields"]] == [("email", "email"), ("field1", "password")]
    assert 'The page calls it "Email"' in request["fields"][0]["guidance"]
    assert "pw" not in json.dumps(request)


def test_skipping_the_sign_in_returns_nothing(folder):
    person(folder, {"action": "skip"})
    assert authpopup.ask_credentials(AuthRequest("https://x.test/login", "login", [])) is None


def test_a_path_outside_the_repository_needs_no_gitignore_rule(tmp_path):
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    assert ensure_ignored(data / "runs" / "x" / "auth" / "state.json", repo) is True
    assert not (repo / ".gitignore").exists()
