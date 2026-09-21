import json
import logging
import socket
from types import SimpleNamespace

import pytest

from website_test_pipeline import pipeline
from website_test_pipeline.pipeline import model_reachable, parse_stage_list, run_chain

LOG = logging.getLogger("chain")


@pytest.fixture
def stages(monkeypatch, tmp_path):
    """Every stage replaced by a recorder; `codes` sets what each returns. Nothing touches a network or a browser."""
    calls, codes = [], {"intents": 0, "expand": 0, "verify": 0, "flowgen": 0, "execute": 0}
    made = {"clients": 0}

    def fake(name):
        def run(*args, **kwargs):
            calls.append(name)
            if isinstance(codes[name], Exception):
                raise codes[name]
            return codes[name]
        return run

    monkeypatch.setattr(pipeline, "run_intents", fake("intents"))
    monkeypatch.setattr(pipeline, "run_expand", fake("expand"))
    monkeypatch.setattr(pipeline, "run_verify", fake("verify"))
    monkeypatch.setattr(pipeline, "run_flowgen", fake("flowgen"))
    monkeypatch.setattr(pipeline, "run_execute", fake("execute"))
    monkeypatch.setattr(pipeline, "model_reachable", lambda url, timeout=3.0: True)
    settings = SimpleNamespace(api_url="https://model.example/v1/chat", artifacts_dir=tmp_path / "a", flows_file=tmp_path / "f.json",
                               root=tmp_path, tests_dir=tmp_path / "t")

    def factory():
        made["clients"] += 1
        return object()

    return SimpleNamespace(calls=calls, codes=codes, settings=settings, factory=factory, made=made)


def _log_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("FLOWS RUN")]


# ------------------------------------------------------------------ the happy path

def test_every_stage_runs_in_order_and_the_summary_says_so(stages, caplog):
    caplog.set_level(logging.INFO)
    assert run_chain(stages.settings, ["u"], LOG, client_factory=stages.factory) == 0
    assert stages.calls == ["intents", "expand", "verify", "flowgen", "execute"]
    text = "\n".join(_log_lines(caplog))
    for stage in ("intents", "expand", "verify", "flowgen", "execute"):
        assert f"FLOWS RUN {stage}" in text
    assert stages.made["clients"] == 1                                    # one model client shared by both model stages


def test_skip_and_only_choose_the_stages_and_unknown_names_are_refused(stages):
    assert run_chain(stages.settings, [], LOG, skip=["intents", "expand"], client_factory=stages.factory) == 0
    assert stages.calls == ["verify", "flowgen", "execute"] and stages.made["clients"] == 0
    stages.calls.clear()
    assert run_chain(stages.settings, [], LOG, only=["verify"], client_factory=stages.factory) == 0 and stages.calls == ["verify"]
    stages.calls.clear()
    assert run_chain(stages.settings, [], LOG, skip=["nope"]) == 2 and stages.calls == []
    assert run_chain(stages.settings, [], LOG, skip=list(pipeline.STAGES)) == 2 and stages.calls == []


def test_stage_lists_are_parsed_leniently():
    assert parse_stage_list("intents, Expand;verify") == ["intents", "expand", "verify"] and parse_stage_list("") == []


# ------------------------------------------------------------------ working without the model

def test_an_unreachable_model_skips_its_stages_at_once_and_the_rest_still_run(stages, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(pipeline, "model_reachable", lambda url, timeout=3.0: False)
    assert run_chain(stages.settings, ["u"], LOG, client_factory=stages.factory) == 0
    assert stages.calls == ["verify", "flowgen", "execute"] and stages.made["clients"] == 0
    text = "\n".join(_log_lines(caplog)) + "\n" + "\n".join(r.getMessage() for r in caplog.records)
    assert "skipped: model not reachable" in text and "not reachable from here" in text and "model.example" in text


def test_reachability_is_checked_once_not_per_stage(stages, monkeypatch):
    checks = []
    monkeypatch.setattr(pipeline, "model_reachable", lambda url, timeout=3.0: checks.append(url) or False)
    run_chain(stages.settings, [], LOG, client_factory=stages.factory)
    assert len(checks) == 1


def test_a_model_stage_that_could_not_finish_does_not_stop_the_chain(stages, caplog):
    caplog.set_level(logging.INFO)
    stages.codes["expand"] = 1                                            # e.g. the model answered badly for one sentence
    assert run_chain(stages.settings, [], LOG, client_factory=stages.factory) == 0
    assert stages.calls[-3:] == ["verify", "flowgen", "execute"]
    assert "did not complete" in "\n".join(_log_lines(caplog))


def test_no_explored_pages_stops_the_chain_with_a_clear_instruction(stages, caplog):
    caplog.set_level(logging.INFO)
    stages.codes["intents"] = 2
    assert run_chain(stages.settings, [], LOG, client_factory=stages.factory) == 2
    assert stages.calls == ["intents"] and "run `explore` first" in "\n".join(_log_lines(caplog))


# ------------------------------------------------------------------ what the exit code means

def test_failing_generated_tests_give_exit_code_1_but_everything_ran(stages):
    stages.codes["execute"] = 1
    assert run_chain(stages.settings, [], LOG, skip=["intents", "expand"]) == 1
    assert stages.calls == ["verify", "flowgen", "execute"]


def test_an_unreadable_flows_file_and_a_crash_are_reported_and_do_not_hide_the_other_stages(stages, caplog):
    caplog.set_level(logging.INFO)
    stages.codes["flowgen"] = 1
    stages.codes["verify"] = RuntimeError("playwright is not installed")
    assert run_chain(stages.settings, [], LOG, skip=["intents", "expand"]) == 2
    assert stages.calls == ["verify", "flowgen", "execute"]               # the crash did not stop the later stages
    text = "\n".join(_log_lines(caplog))
    assert "crashed: playwright is not installed" in text and "flows file could not be read" in text


def test_no_flows_yet_is_not_a_failure(stages, caplog):
    caplog.set_level(logging.INFO)
    stages.codes["verify"] = stages.codes["flowgen"] = 2
    assert run_chain(stages.settings, [], LOG, skip=["intents", "expand", "execute"]) == 0
    assert "nothing to verify" in "\n".join(_log_lines(caplog))


def test_the_coverage_line_ends_the_summary_when_there_is_data(stages, caplog, tmp_path):
    caplog.set_level(logging.INFO)
    stages.settings.artifacts_dir.mkdir()
    (stages.settings.artifacts_dir / "p.inventory.json").write_text(json.dumps(
        {"url": "https://x.test/en", "controls": [{"tag": "button", "name": "Go", "region": "other"}]}), encoding="utf-8")
    stages.settings.flows_file.write_text(json.dumps({"version": 1, "flows": []}), encoding="utf-8")
    run_chain(stages.settings, [], LOG, skip=["intents", "expand"])
    assert any("COVERAGE  pages visited 0/1" in line for line in _log_lines(caplog))


# ------------------------------------------------------------------ reachability

def test_model_reachable_tells_an_open_port_from_a_closed_one():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert model_reachable(f"http://127.0.0.1:{port}/v1/chat") is True
    finally:
        server.close()
    assert model_reachable(f"http://127.0.0.1:{port}/v1/chat", timeout=0.5) is False     # nothing listens there any more


def test_model_reachable_never_raises_on_nonsense():
    for bad in ("", "not a url", "http://", "https://nonexistent.invalid/x"):
        assert model_reachable(bad, timeout=0.5) is False


def test_when_the_site_cannot_be_reached_verify_and_execute_are_skipped_and_it_is_not_a_failure(stages, caplog):
    caplog.set_level(logging.INFO)
    stages.codes["verify"] = 3                                             # run_verify: no flow could be reached
    assert run_chain(stages.settings, [], LOG, skip=["intents", "expand"]) == 0
    assert stages.calls == ["verify", "flowgen"]                           # flowgen works from saved files; execute would only fail
    text = "\n".join(_log_lines(caplog))
    assert "could not be reached (nothing was recorded)" in text and "execute  skipped: the site could not be reached" in text


# ------------------------------------------------------------------ a model that is up but not answering (gateway timeouts)

def test_gateway_and_network_failures_count_as_the_model_being_unavailable_but_a_bad_answer_does_not():
    import urllib.error
    from website_test_pipeline.llm import ModelError, is_unavailable
    for status in (401, 429, 502, 503, 504, 524, 530):
        assert is_unavailable(ModelError("x", status=status)), status
    assert not is_unavailable(ModelError("reasoning without final content"))          # answered, but unusably
    assert not is_unavailable(ModelError("bad", status=400)) and not is_unavailable(ValueError("no JSON"))
    assert is_unavailable(urllib.error.URLError("dns")) and is_unavailable(TimeoutError()) and is_unavailable(ConnectionResetError())


def test_a_model_that_times_out_stops_the_model_stages_and_the_rest_of_the_chain_still_runs(stages, caplog):
    caplog.set_level(logging.INFO)
    stages.codes["intents"] = 4
    assert run_chain(stages.settings, ["u"], LOG, client_factory=stages.factory) == 0
    assert stages.calls == ["intents", "verify", "flowgen", "execute"]                # expand was not even attempted
    text = "\n".join(_log_lines(caplog))
    assert "the model is unavailable (nothing was changed)" in text and "expand   skipped: the model was unavailable" in text


def test_intents_returns_4_when_the_model_is_unavailable_and_1_for_a_bad_answer(tmp_path):
    from website_test_pipeline.intents import run_intents
    from website_test_pipeline.llm import ModelError
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "x.inventory.json").write_text(json.dumps(
        {"url": "https://x.test/en", "controls": [{"tag": "button", "name": "Go", "region": "other"}]}), encoding="utf-8")
    settings = SimpleNamespace(intents_file=tmp_path / "i.json", flows_file=tmp_path / "f.json", artifacts_dir=tmp_path / "artifacts",
                               urls_file=tmp_path / "u.txt", model="m")

    class _Client:
        def __init__(self, exc): self.exc = exc
        def generate(self, prompt, system): raise self.exc

    assert run_intents(settings, ["https://x.test/en"], _Client(ModelError("gateway", status=524)), LOG) == 4
    assert run_intents(settings, ["https://x.test/en"], _Client(ModelError("reasoning without final content")), LOG) == 1
    assert not settings.intents_file.exists()


def test_expand_stops_at_the_first_unavailable_model_instead_of_waiting_out_every_sentence(tmp_path):
    from website_test_pipeline.expand import run_expand
    from website_test_pipeline.intents import add_intent, save_intents
    from website_test_pipeline.llm import ModelError
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "x.inventory.json").write_text(json.dumps(
        {"url": "https://x.test/en", "controls": [{"tag": "button", "name": "Go", "region": "other"}]}), encoding="utf-8")
    doc = {"version": 1, "intents": []}
    for sentence in ("A visitor opens the first page and looks around it.", "A visitor opens the second page and reads about it."):
        add_intent(doc, sentence, "human", "t")
    settings = SimpleNamespace(intents_file=tmp_path / "i.json", flows_file=tmp_path / "f.json", ratings_file=tmp_path / "r.json",
                               artifacts_dir=tmp_path / "artifacts", urls_file=tmp_path / "u.txt", model="m")
    save_intents(settings.intents_file, doc)
    calls = []

    class _Client:
        def generate(self, prompt, system):
            calls.append(1)
            raise ModelError("gateway timeout", status=524)

    assert run_expand(settings, ["https://x.test/en"], _Client(), LOG) == 4
    assert len(calls) == 1                                                            # the second sentence was never tried
    stored = json.loads(settings.intents_file.read_text(encoding="utf-8"))["intents"]
    assert [i["status"] for i in stored] == ["new", "new"]                            # nothing was marked unbuildable
