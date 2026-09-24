import json
import logging
from types import SimpleNamespace

from website_test_pipeline.llm import ModelClient


class _Response:
    def __init__(self, content):
        self.status, self._body = 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _client(monkeypatch, content, **settings):
    sent = []
    base = dict(api_url="https://m.test/v1/chat/completions", api_key="k", model="m", model_retries=0, retry_base_ms=1, model_timeout_ms=1000)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: sent.append(json.loads(req.data)) or _Response(content))
    return ModelClient(SimpleNamespace(**{**base, **settings}), logging.getLogger("t")), sent


def test_thinking_off_is_the_default_and_a_reasoning_model_can_be_left_alone(monkeypatch):
    client, sent = _client(monkeypatch, "ok")
    client.generate("p", "s")
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False} and sent[0]["max_tokens"] == 3072
    client, sent = _client(monkeypatch, "ok", model_thinking="default", model_max_tokens=8192)
    client.generate("p", "s")
    assert "chat_template_kwargs" not in sent[0] and sent[0]["max_tokens"] == 8192


def test_reasoning_that_leaked_into_the_reply_is_cut_off_before_the_answer(monkeypatch):
    client, _ = _client(monkeypatch, 'The user wants JSON. Simple.</think>{"intents": []}')
    assert client.generate("p", "s") == '{"intents": []}'
