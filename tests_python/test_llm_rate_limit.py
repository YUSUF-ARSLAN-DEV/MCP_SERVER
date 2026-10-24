import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from website_test_pipeline import llm
from website_test_pipeline.llm import ModelClient, note_cooldown, retry_after_seconds, wait_out_cooldown


def test_retry_after_is_read_in_seconds_and_ignored_when_unreadable():
    assert retry_after_seconds({"Retry-After": "7"}) == 7.0
    assert retry_after_seconds({"Retry-After": "0.5"}) == 0.5
    assert retry_after_seconds({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert retry_after_seconds({}) is None and retry_after_seconds(None) is None


def test_one_cooldown_makes_every_caller_wait_and_is_capped():
    llm._COOLDOWN["until"] = 0.0
    note_cooldown(0.3)
    started = time.monotonic()
    waited = wait_out_cooldown()
    assert 0.2 <= waited <= 0.5 and time.monotonic() - started >= 0.25
    assert wait_out_cooldown() == 0.0
    note_cooldown(10_000)
    assert llm._COOLDOWN["until"] - time.monotonic() <= llm.MAX_COOLDOWN_S + 1
    llm._COOLDOWN["until"] = 0.0


class _Throttle(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        _Throttle.calls.append(time.monotonic())
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if len(_Throttle.calls) == 1:
            self.send_response(429); self.send_header("Retry-After", "1"); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"{}")
            return
        body = b'{"choices":[{"message":{"content":"ok"}}]}'
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)


def test_a_429_with_retry_after_waits_that_long_not_the_tiny_base_backoff():
    llm._COOLDOWN["until"] = 0.0
    _Throttle.calls = []
    httpd = HTTPServer(("127.0.0.1", 0), _Throttle)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        settings = SimpleNamespace(api_url=f"http://127.0.0.1:{httpd.server_port}/v1/chat/completions", api_key="k", model="m",
                                   model_provider="openai", model_retries=2, retry_base_ms=100, model_timeout_ms=5000,
                                   model_thinking="off", model_max_tokens=100)
        assert ModelClient(settings, logging.getLogger("t")).generate("hi", "sys") == "ok"
    finally:
        httpd.shutdown()
    assert len(_Throttle.calls) == 2 and _Throttle.calls[1] - _Throttle.calls[0] >= 0.9     # the server said 1 s; base backoff was 0.1 s
