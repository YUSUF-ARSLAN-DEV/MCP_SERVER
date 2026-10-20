"""The model API is pluggable: the pipeline asks ModelClient.generate() and a provider speaks whichever API the
endpoint has. These tests run a real HTTP server that answers in each API's shape, so the whole path is covered."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from website_test_pipeline.envcheck import check_model
from website_test_pipeline.llm import ModelClient, ModelError
from website_test_pipeline.providers import Anthropic, OpenAICompatible, Provider, get_provider, names, register

PNG = "data:image/png;base64,iVBORw0KGgo="


def _settings(**over):
    base = dict(api_url="https://m.test/v1/chat/completions", api_key="k", model="m1", model_provider="openai",
                model_retries=1, retry_base_ms=1, model_timeout_ms=3000, model_thinking="off", model_max_tokens=3072)
    return SimpleNamespace(**{**base, **over})


def test_the_registry_knows_both_apis_and_refuses_an_unknown_one():
    assert {"openai", "anthropic"} <= set(names())
    assert get_provider(None).name == "openai" and get_provider("ANTHROPIC").name == "anthropic"
    with pytest.raises(ModelError, match="unknown MODEL_PROVIDER"):
        get_provider("skynet")


def test_openai_request_shape_is_unchanged():
    url, headers, body = OpenAICompatible().request(_settings(), "hello", "be brief", [PNG])
    sent = json.loads(body)
    assert url == "https://m.test/v1/chat/completions" and headers["Authorization"] == "Bearer k"
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["messages"][1]["content"][1] == {"type": "image_url", "image_url": {"url": PNG}}
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_anthropic_request_puts_system_aside_and_sends_images_as_base64_blocks():
    url, headers, body = Anthropic().request(_settings(model_provider="anthropic", api_url="https://api.anthropic.com"), "hello", "be brief", [PNG])
    sent = json.loads(body)
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "k" and headers["anthropic-version"] and "Authorization" not in headers
    assert sent["system"] == "be brief" and sent["model"] == "m1" and sent["max_tokens"] == 3072
    assert all(m["role"] != "system" for m in sent["messages"])
    assert sent["messages"][0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


@pytest.mark.parametrize("given,expected", [
    ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
    ("https://api.anthropic.com/", "https://api.anthropic.com/v1/messages"),
    ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
    ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/messages"),
    ("https://gw.test/proxy/v1/messages", "https://gw.test/proxy/v1/messages"),
])
def test_anthropic_accepts_the_host_the_v1_root_or_the_full_url(given, expected):
    url, _, _ = Anthropic().request(_settings(api_url=given), "p", "s", None)
    assert url == expected


def test_anthropic_needs_a_key_and_a_bad_image_is_refused():
    assert Anthropic().problems(_settings(api_key=""))
    assert Anthropic().problems(_settings()) == [] and OpenAICompatible().problems(_settings(api_key="")) == []
    with pytest.raises(ModelError, match="data URL"):
        Anthropic().request(_settings(), "p", "s", ["https://x.test/a.png"])


def test_anthropic_reply_joins_text_blocks_and_flags_reasoning_only_or_empty():
    reply = Anthropic().reply_text
    assert reply({"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, "") == "ab"
    with pytest.raises(ModelError, match="reasoning without final content"):
        reply({"content": [{"type": "thinking", "thinking": "hmm"}]}, "")
    with pytest.raises(ModelError, match="no usable content"):
        reply({"content": []}, "")


def test_a_new_api_is_one_subclass_and_one_register_call():
    class Echo(Provider):
        name = "echo-test"

        def request(self, s, prompt, system, images):
            return "http://echo.test", {}, prompt.encode()

        def reply_text(self, data, raw):
            return data["said"]
    register(Echo())
    assert get_provider("echo-test").reply_text({"said": "hi"}, "") == "hi"


class _Handler(BaseHTTPRequestHandler):
    seen = []
    fail_first = False

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.seen.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.path.startswith("/v1beta/models"):
            return self._send(200, {"models": [{"name": "models/served-1", "supportedGenerationMethods": ["generateContent"]},
                                               {"name": "models/served-2"}, {"name": "models/embed-only", "supportedGenerationMethods": ["embedContent"]}]})
        self._send(200, {"data": [{"id": "served-1"}, {"id": "served-2"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if _Handler.fail_first:
            _Handler.fail_first = False
            return self._send(503, {"error": "busy"})
        if ":generateContent" in self.path:
            self._send(200, {"candidates": [{"content": {"parts": [{"text": "pondering", "thought": True}, {"text": "gemini says hi"}]}}]})
        elif self.path.endswith("/v1/messages"):
            self._send(200, {"type": "message", "content": [{"type": "text", "text": "claude says hi"}]})
        else:
            self._send(200, {"choices": [{"message": {"content": "openai says hi"}}]})


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.seen, _Handler.fail_first = [], False
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_model_client_talks_openai_to_a_real_server_and_retries_a_503(server):
    _Handler.fail_first = True
    client = ModelClient(_settings(api_url=server + "/v1/chat/completions"), logging.getLogger("t"))
    assert client.generate("p", "s") == "openai says hi"
    assert [r[0] for r in _Handler.seen] == ["POST", "POST"]                      # the 503, then the retry


def test_model_client_talks_anthropic_to_a_real_server_with_the_right_headers(server):
    client = ModelClient(_settings(model_provider="anthropic", api_url=server), logging.getLogger("t"))
    assert client.generate("p", "s", images=[PNG]) == "claude says hi"
    method, path, headers, body = _Handler.seen[0]
    assert path == "/v1/messages" and headers["x-api-key"] == "k" and body["system"] == "s" and "authorization" not in headers


def test_switching_provider_is_only_a_setting_the_caller_does_not_change(server):
    for provider, url, expected in (("openai", server + "/v1/chat/completions", "openai says hi"), ("anthropic", server, "claude says hi")):
        client = ModelClient(_settings(model_provider=provider, api_url=url), logging.getLogger("t"))
        assert client.generate("same call", "same system") == expected


def test_model_check_asks_each_provider_where_it_lists_models(server):
    assert check_model(_settings(model="ghost", api_url=server + "/v1/chat/completions")).endswith("served-1, served-2")
    assert check_model(_settings(model="ghost", model_provider="anthropic", api_url=server)).endswith("served-1, served-2")
    models = [r for r in _Handler.seen if r[0] == "GET"]
    assert models[0][1] == "/v1/models" and "authorization" in models[0][2]
    assert models[1][1] == "/v1/models" and models[1][2].get("x-api-key") == "k"
    assert check_model(_settings(model="served-2", api_url=server + "/v1/chat/completions")) is None


# ---------------------------------------------------------------- Google (Gemini)

def _google(**over):
    return _settings(**{"model_provider": "google", "api_url": "", "model": "gemini-2.5-flash", **over})


def test_google_request_puts_the_model_in_the_url_the_key_in_a_header_and_system_aside():
    url, headers, body = get_provider("google").request(_google(), "hello", "be brief", [PNG])
    sent = json.loads(body)
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert headers["x-goog-api-key"] == "k" and "k" not in url and "Authorization" not in headers
    assert sent["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert sent["contents"][0]["parts"][0] == {"text": "hello"}
    assert sent["contents"][0]["parts"][1] == {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}
    assert sent["generationConfig"]["maxOutputTokens"] == 3072 and sent["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_google_leaves_thinking_alone_for_models_that_cannot_turn_it_off():
    for model, on in (("gemini-2.5-pro", False), ("models/gemini-2.5-flash-lite", True), ("gemini-2.0-flash", False)):
        _, _, body = get_provider("google").request(_google(model=model), "p", "s", None)
        assert ("thinkingConfig" in json.loads(body)["generationConfig"]) is on
    _, _, body = get_provider("google").request(_google(model_thinking="default"), "p", "s", None)
    assert "thinkingConfig" not in json.loads(body)["generationConfig"]


@pytest.mark.parametrize("given", ["", "https://generativelanguage.googleapis.com/", "https://generativelanguage.googleapis.com/v1beta"])
def test_google_accepts_the_default_host_or_a_v1beta_root(given):
    url, _, _ = get_provider("google").request(_google(api_url=given, model="models/gemini-x"), "p", "s", None)
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent"


def test_google_needs_a_key_skips_thought_parts_and_says_why_a_reply_is_empty():
    google = get_provider("google")
    assert google.problems(_google(api_key="")) and google.problems(_google()) == []
    assert google.reply_text({"candidates": [{"content": {"parts": [{"text": "hmm", "thought": True}, {"text": "answer"}]}}]}, "") == "answer"
    with pytest.raises(ModelError, match="reason=SAFETY"):
        google.reply_text({"candidates": [{"finishReason": "SAFETY"}]}, "")
    with pytest.raises(ModelError, match="reason=PROHIBITED_CONTENT"):
        google.reply_text({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "")
    with pytest.raises(ModelError, match="reasoning without final content"):
        google.reply_text({"candidates": [{"content": {"parts": [{"text": "hmm", "thought": True}]}}]}, "")


def test_model_client_talks_gemini_to_a_real_server(server):
    client = ModelClient(_google(api_url=server), logging.getLogger("t"))
    assert client.generate("p", "s", images=[PNG]) == "gemini says hi"
    method, path, headers, body = _Handler.seen[0]
    assert path == "/v1beta/models/gemini-2.5-flash:generateContent" and headers["x-goog-api-key"] == "k"


def test_model_check_lists_gemini_models_without_the_models_prefix_and_only_those_that_generate(server):
    assert check_model(_google(model="ghost", api_url=server)).endswith("served-1, served-2")
    assert check_model(_google(model="models/served-1", api_url=server)) is None
