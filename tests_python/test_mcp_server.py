import asyncio
import threading

import pytest

pytest.importorskip("mcp.server.mcpserver")

from mcp_adapter.client import PipelineError
from mcp_adapter.server import build_server, client_from_env


class FakeClient:
    def __init__(self):
        self.started = None

    def models(self):
        return [{"id": "m1", "name": "Cerebras", "provider": "openai", "model": "qwen-3.8-27b", "key_set": True,
                 "api_url": "https://api.cerebras.ai/v1/chat/completions", "created_at": "t"}]

    def start_run(self, url, **kw):
        self.started = (url, kw)
        return {"id": "abc", "status": "queued", "site": "x.test"}

    def run(self, job_id):
        if job_id == "nope":
            raise PipelineError("GET /api/jobs/nope -> 404: No such run.")
        return {"id": job_id, "url": "https://x.test/", "status": "running", "stage": "explore", "progress_pct": 40,
                "message": "m", "error": "", "finished_at": None, "internal": "left out"}

    def runs(self, limit):
        return [{"id": "abc", "url": "u", "status": "done", "stage": "report", "progress_pct": 100, "created_at": "t", "x": 1}]

    def report(self, job_id):
        return {"verdict": "GO", "confidence": {"level": "Full"}}

    def cancel(self, job_id):
        return {"id": job_id, "status": "cancelled", "other": 1}


def _in_thread(coro_fn):
    """Another test module can leave an asyncio loop running on the main thread; a fresh thread has none."""
    box = {}

    def work():
        try:
            box["value"] = asyncio.run(coro_fn())
        except BaseException as exc:
            box["error"] = exc
    thread = threading.Thread(target=work)
    thread.start(); thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _call(client, name, **args):
    server = build_server(client)

    async def go():
        result = await server.call_tool(name, args)
        return result
    return _in_thread(go)


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content) + str(getattr(result, "structured_content", "") or "")


def test_the_server_offers_exactly_the_six_tools_and_marks_the_read_only_ones():
    server = build_server(FakeClient())
    tools = {t.name: t for t in _in_thread(server.list_tools)}
    assert set(tools) == {"list_models", "start_run", "run_status", "list_runs", "get_report", "cancel_run"}
    assert tools["run_status"].annotations.read_only_hint is True and tools["get_report"].annotations.read_only_hint is True
    assert not (tools["start_run"].annotations and tools["start_run"].annotations.read_only_hint)   # starting a run is not read-only
    assert tools["cancel_run"].annotations.destructive_hint is True


def test_list_models_never_returns_anything_beyond_the_safe_fields():
    text = _text(_call(FakeClient(), "list_models"))
    assert "qwen-3.8-27b" in text and "api_url" not in text and "created_at" not in text


def test_start_run_never_asks_a_person_and_maps_defaults_to_the_server_defaults():
    client = FakeClient()
    _call(client, "start_run", url="https://x.test/")
    url, kw = client.started
    assert url == "https://x.test/" and kw["ask_human"] is False
    assert kw["model_id"] is None and kw["max_pages"] is None and kw["probe_max"] is None


def test_start_run_passes_a_chosen_model_and_limits_through():
    client = FakeClient()
    _call(client, "start_run", url="https://x.test/", model_id="m1", max_pages=3, probe_max=0)
    assert client.started[1] | {} == {"model_id": "m1", "max_pages": 3, "probe_max": 0, "ask_human": False, "reuse_workspace": False, "requirements": ""}


def test_run_status_returns_only_the_fields_an_agent_needs():
    text = _text(_call(FakeClient(), "run_status", job_id="abc"))
    assert "explore" in text and "internal" not in text


def test_an_api_refusal_becomes_a_readable_tool_error_not_a_crash():
    result = None
    try:
        result = _call(FakeClient(), "run_status", job_id="nope")
    except Exception as exc:                       # the SDK may raise ToolError or return an error result
        assert "No such run" in str(exc) or "No such run" in str(exc.__cause__)
    else:
        assert getattr(result, "is_error", False) and "No such run" in _text(result)


def test_get_report_and_cancel_run_reach_the_client():
    assert "GO" in _text(_call(FakeClient(), "get_report", job_id="abc"))
    assert "cancelled" in _text(_call(FakeClient(), "cancel_run", job_id="abc"))


def test_the_client_comes_from_the_environment_and_defaults_to_the_local_api():
    client = client_from_env({"PIPELINE_USERNAME": "u", "PIPELINE_PASSWORD": "p"})
    assert str(client._http.base_url).rstrip("/") == "http://127.0.0.1:8000" and client.username == "u"


def test_start_run_can_carry_user_stories_as_text():
    client = FakeClient()
    _call(client, "start_run", url="https://x.test/", requirements="US-01: A visitor can search.")
    assert client.started[1]["requirements"] == "US-01: A visitor can search."
