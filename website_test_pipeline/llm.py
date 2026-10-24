from __future__ import annotations
import json, logging, time, uuid, re
from typing import Any
import urllib.error, urllib.request
from .config import Settings
from .providers import REJECTED_FIELDS, ModelError, get_provider      # ModelError lives with the providers; re-exported here for callers

TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 524, 530}

# When a provider says "too many requests, try again in N seconds", EVERY worker thread of this process must wait, not
# just the one that was told: four threads each retrying after 3 s keep the limit tripped and burn the whole retry budget
# (found live: three pages of a run got no test at all because the endpoint was throttling). Seconds, monotonic clock.
_COOLDOWN = {"until": 0.0}
MAX_COOLDOWN_S = 90.0


def retry_after_seconds(headers) -> float | None:
    """The server's own "wait this long" (Retry-After, in seconds), or None when it gave none we can read."""
    try:
        value = headers.get("Retry-After") if headers is not None else None
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and seconds >= 0 else None


def note_cooldown(seconds: float) -> None:
    _COOLDOWN["until"] = max(_COOLDOWN["until"], time.monotonic() + min(max(seconds, 0.0), MAX_COOLDOWN_S))


def wait_out_cooldown() -> float:
    remaining = _COOLDOWN["until"] - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
        return remaining
    return 0.0

# HTTP statuses that mean "the model service is not available", as opposed to "it answered badly".
UNAVAILABLE = {401, 403, 429, 500, 502, 503, 504, 524, 530}


def is_unavailable(exc: BaseException) -> bool:
    """True when a failed model call says the service is down, unreachable, timing out or refusing us (so trying the
    next sentence or stage is pointless), False when the model answered but the answer was unusable."""
    if isinstance(exc, ModelError):
        return exc.status in UNAVAILABLE
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


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
    if 'reasoning without final content' in text: return 'RESPONSE CONTRACT FAILURE: the model returned reasoning but no final answer; thinking mode should be disabled.'
    if 'no usable content' in text or 'non-json' in text: return 'RESPONSE CONTRACT FAILURE: the server responded, but not with usable model output.'
    return 'UNCLASSIFIED MODEL FAILURE: inspect the bounded response body and request metadata.'

class ModelClient:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.s = settings; self.log = logger

    def generate(self, prompt: str, system: str, images: list[str] | None = None) -> str:
        """Ask the model. `images` are data URLs (data:image/jpeg;base64,...) sent with the prompt for a vision-capable
        model; a server that does not accept images answers 400/415/422, which callers treat as "no vision"."""
        provider = get_provider(getattr(self.s, "model_provider", None))
        for problem in provider.problems(self.s):
            raise ModelError(problem)
        url, headers, payload = provider.request(self.s, prompt, system, images)
        for attempt in range(1, self.s.model_retries + 2):
            waited = wait_out_cooldown()
            if waited > 0.5: self.log.info("model rate limit: waited %.0fs before asking again", waited)
            request_id = uuid.uuid4().hex[:10]; started = time.monotonic()
            req = urllib.request.Request(url, data=payload, headers={"User-Agent": "website-test-pipeline/0.1", **headers})
            try:
                with urllib.request.urlopen(req, timeout=self.s.model_timeout_ms / 1000) as response:
                    status = response.status; raw = response.read().decode("utf-8", "replace")
                self.log.info("model response request=%s attempt=%s/%s status=%s ms=%s bytes=%s", request_id, attempt, self.s.model_retries + 1, status, int((time.monotonic()-started)*1000), len(raw))
                if status >= 400: raise ModelError(f"API error: {status}", status=status, body=raw[:1000])
                try: data = json.loads(raw)
                except json.JSONDecodeError as exc: raise ModelError("API returned non-JSON success response", body=raw[:1000]) from exc
                if data.get("error"): raise ModelError(f"Model error: {str(data['error'])[:500]}", body=raw[:1000])
                content = provider.reply_text(data, raw)
                if "</think>" in content:
                    content = content.split("</think>", 1)[1]          # reasoning that leaked into the reply
                if not content.strip(): raise ModelError("Model reply held only reasoning", body=raw[:1000])
                return content
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:1000]
                error = ModelError(f"API error: {exc.code}; {diagnose_error(exc.code, body)}", status=exc.code, body=body)
                self.log.warning("model failure request=%s attempt=%s status=%s diagnosis=%s detail=%s", request_id, attempt, exc.code, diagnose_error(exc.code, body), body.replace("\n", " ")[:300])
                if exc.code in (400, 422) and "chat_template_kwargs" in body and "chat_template_kwargs" not in REJECTED_FIELDS.get(self.s.api_url, ()):
                    REJECTED_FIELDS.setdefault(self.s.api_url, set()).add("chat_template_kwargs")
                    self.log.info("model endpoint does not accept chat_template_kwargs; sending without it from now on")
                    return self.generate(prompt, system, images)
                if exc.code in (429, 503):
                    told = retry_after_seconds(exc.headers)
                    note_cooldown(told if told is not None else self.s.retry_base_ms / 1000 * 2 ** (attempt - 1))
                if exc.code not in TRANSIENT or attempt > self.s.model_retries: raise error
            except (urllib.error.URLError, TimeoutError, ModelError) as exc:
                status = getattr(exc, "status", None); transient = isinstance(exc, ModelError) and status in TRANSIENT or not isinstance(exc, ModelError)
                self.log.warning("model failure request=%s attempt=%s diagnosis=%s message=%s", request_id, attempt, diagnose_error(status, getattr(exc, 'body', ''), str(exc)), exc)
                if not transient or attempt > self.s.model_retries: raise exc
            time.sleep(self.s.retry_base_ms / 1000 * 2 ** (attempt - 1))
        raise ModelError("Model retries exhausted")
