from __future__ import annotations
import json, logging, time, uuid, re
from typing import Any
import urllib.error, urllib.request
from .config import Settings

TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 524, 530}

class ModelError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message); self.status = status; self.body = body

def diagnose_error(status: int | None, body: str = "", message: str = "") -> str:
    text = f"{message} {body}".lower()
    if status in {401, 403}:
        if '1010' in text or 'cloudflare' in text:
            return 'ACCESS BLOCKED: Cloudflare rejected this client/request (code 1010). This is an upstream firewall or bot/access policy, not a prompt-parsing error.'
        return 'AUTHORIZATION FAILURE: the API key is missing, invalid, expired, or lacks permission for this endpoint/model.'
    if status == 429: return 'RATE LIMITED: the model endpoint is throttling requests.'
    if status in {524, 504}: return 'UPSTREAM TIMEOUT: the gateway connected, but the model server did not respond before its deadline.'
    if status in {530, 502, 503, 500}: return 'UPSTREAM AVAILABILITY FAILURE: the gateway/model service is unavailable or its tunnel is unhealthy.'
    if 'context' in text or 'token' in text and 'limit' in text: return 'REQUEST TOO LARGE: prompt or requested output exceeds the model context limit.'
    if 'no usable content' in text or 'non-json' in text: return 'RESPONSE CONTRACT FAILURE: the server responded, but not with usable model output.'
    return 'UNCLASSIFIED MODEL FAILURE: inspect the bounded response body and request metadata.'

class ModelClient:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.s = settings; self.log = logger

    def generate(self, prompt: str, system: str) -> str:
        if not self.s.api_key and self.s.api_url.startswith("https://llm-1.d4done.com"):
            raise ModelError("API_KEY is required for the configured model endpoint")
        payload = json.dumps({"model": self.s.model, "messages": [{"role":"system","content":system},{"role":"user","content":prompt}], "temperature": 0.1, "max_tokens": 3072, "stream": False}).encode()
        for attempt in range(1, self.s.model_retries + 2):
            request_id = uuid.uuid4().hex[:10]; started = time.monotonic()
            req = urllib.request.Request(self.s.api_url, data=payload, headers={"Content-Type":"application/json", **({"Authorization": f"Bearer {self.s.api_key}"} if self.s.api_key else {})})
            try:
                with urllib.request.urlopen(req, timeout=self.s.model_timeout_ms / 1000) as response:
                    status = response.status; raw = response.read().decode("utf-8", "replace")
                self.log.info("model response request=%s attempt=%s/%s status=%s ms=%s bytes=%s", request_id, attempt, self.s.model_retries + 1, status, int((time.monotonic()-started)*1000), len(raw))
                if status >= 400: raise ModelError(f"API error: {status}", status=status, body=raw[:1000])
                try: data = json.loads(raw)
                except json.JSONDecodeError as exc: raise ModelError("API returned non-JSON success response", body=raw[:1000]) from exc
                if data.get("error"): raise ModelError(f"Model error: {str(data['error'])[:500]}", body=raw[:1000])
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or data.get("output_text") or data.get("response") or data.get("content")
                if isinstance(content, list): content = "".join(x if isinstance(x, str) else x.get("text", "") for x in content)
                if not isinstance(content, str) or not content.strip(): raise ModelError(f"Model response had no usable content; keys={','.join(data.keys())}", body=raw[:1000])
                return content
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:1000]
                error = ModelError(f"API error: {exc.code}; {diagnose_error(exc.code, body)}", status=exc.code, body=body)
                self.log.warning("model failure request=%s attempt=%s status=%s diagnosis=%s detail=%s", request_id, attempt, exc.code, diagnose_error(exc.code, body), body.replace("\n", " ")[:300])
                if exc.code not in TRANSIENT or attempt > self.s.model_retries: raise error
            except (urllib.error.URLError, TimeoutError, ModelError) as exc:
                status = getattr(exc, "status", None); transient = isinstance(exc, ModelError) and status in TRANSIENT or not isinstance(exc, ModelError)
                self.log.warning("model failure request=%s attempt=%s diagnosis=%s message=%s", request_id, attempt, diagnose_error(status, getattr(exc, 'body', ''), str(exc)), exc)
                if not transient or attempt > self.s.model_retries: raise exc
            time.sleep(self.s.retry_base_ms / 1000 * 2 ** (attempt - 1))
        raise ModelError("Model retries exhausted")
