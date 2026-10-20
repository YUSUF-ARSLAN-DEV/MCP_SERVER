"""Model providers: how to talk to a given kind of model API, so no part of the pipeline is tied to one server.

A provider knows four things about its API and nothing else: how to build the HTTP request for a prompt (with
optional images), how to pull the reply text out of the response, where to ask which models it serves, and what
settings it cannot work without. ModelClient (llm.py) owns everything that is the same for every API - retries,
backoff, logging, error diagnosis, stripping leaked reasoning.

Choose one with MODEL_PROVIDER: "openai" (default; the chat-completions shape that OpenAI itself, vLLM, LM Studio,
Ollama, llama.cpp and most hosted gateways speak), "anthropic" (Claude Messages API) or "google" (Gemini API).
A new API is one subclass plus register().
"""
from __future__ import annotations
import json
import re
from urllib.parse import quote

DEFAULT_PROVIDER = "openai"
ANTHROPIC_VERSION = "2023-06-01"


class ModelError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message); self.status = status; self.body = body


class Provider:
    name = ""

    def problems(self, s) -> list[str]:
        """Settings this API cannot work without, as sentences. Empty = fine."""
        return []

    def request(self, s, prompt: str, system: str, images: list[str] | None) -> tuple[str, dict, bytes]:
        """(url, headers, body) for one completion."""
        raise NotImplementedError

    def reply_text(self, data: dict, raw: str) -> str:
        """The reply text from a parsed 2xx response; ModelError when there is none."""
        raise NotImplementedError

    def models_request(self, s) -> tuple[str, dict]:
        """(url, headers) that list the models this endpoint serves."""
        raise NotImplementedError

    def model_ids(self, data: dict) -> list[str]:
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


class OpenAICompatible(Provider):
    """POST <API_URL> with {model, messages, ...}; API_URL is the full chat-completions URL."""
    name = "openai"

    def request(self, s, prompt, system, images):
        user = prompt if not images else [{"type": "text", "text": prompt},
                                          *({"type": "image_url", "image_url": {"url": u}} for u in images)]
        body = {"model": s.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.1, "max_tokens": getattr(s, "model_max_tokens", 3072), "stream": False}
        if getattr(s, "model_thinking", "off") == "off":
            body["chat_template_kwargs"] = {"enable_thinking": False}     # non-reasoning models; a reasoning one leaves it unset
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {s.api_key}"} if s.api_key else {})}
        return s.api_url, headers, json.dumps(body).encode()

    def reply_text(self, data, raw):
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if not content and (message.get("reasoning") or message.get("reasoning_content")):
            raise ModelError("Model returned reasoning without final content", body=raw[:1000])
        if isinstance(content, list):
            content = "".join(x if isinstance(x, str) else x.get("text", "") for x in content)
        if not isinstance(content, str) or not content.strip():
            raise ModelError(f"Model response had no usable content; keys={','.join(data.keys())}", body=raw[:1000])
        return content

    def models_request(self, s):
        base = s.api_url.split("/chat/completions")[0].rstrip("/")
        return base + "/models", ({"Authorization": f"Bearer {s.api_key}"} if s.api_key else {})


class Anthropic(Provider):
    """Claude's Messages API. API_URL may be the host, the /v1 root or the full .../v1/messages URL."""
    name = "anthropic"

    def problems(self, s):
        return [] if s.api_key else ["MODEL_PROVIDER=anthropic needs API_KEY (an Anthropic API key)"]

    @staticmethod
    def _root(api_url: str) -> str:
        url = (api_url or "https://api.anthropic.com").rstrip("/")
        for tail in ("/v1/messages", "/messages", "/v1"):
            if url.endswith(tail):
                return url[: -len(tail)]
        return url

    def _headers(self, s) -> dict:
        return {"Content-Type": "application/json", "x-api-key": s.api_key, "anthropic-version": ANTHROPIC_VERSION}

    @staticmethod
    def _image(data_url: str) -> dict:
        match = re.match(r"data:(image/[\w.+-]+);base64,(.+)$", data_url or "", re.S)
        if not match:
            raise ModelError("an image must be a base64 data URL (data:image/png;base64,...)")
        return {"type": "image", "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)}}

    def request(self, s, prompt, system, images):
        content = [{"type": "text", "text": prompt}, *(self._image(u) for u in images or [])]
        body = {"model": s.model, "system": system, "messages": [{"role": "user", "content": content}],
                "temperature": 0.1, "max_tokens": getattr(s, "model_max_tokens", 3072)}
        return self._root(s.api_url) + "/v1/messages", self._headers(s), json.dumps(body).encode()

    def reply_text(self, data, raw):
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if not text.strip():
            if any(isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"} for b in blocks):
                raise ModelError("Model returned reasoning without final content", body=raw[:1000])
            raise ModelError(f"Model response had no usable content; keys={','.join(data.keys())}", body=raw[:1000])
        return text

    def models_request(self, s):
        return self._root(s.api_url) + "/v1/models", self._headers(s)


class Google(Provider):
    """Gemini's generateContent API. API_URL may be left as the default host; MODEL_NAME is the Gemini model id and goes
    into the URL (a leading "models/" is tolerated). The key travels in a header, never in the URL."""
    name = "google"
    DEFAULT_ROOT = "https://generativelanguage.googleapis.com"

    def problems(self, s):
        return [] if s.api_key else ["MODEL_PROVIDER=google needs API_KEY (a Google AI Studio / Gemini API key)"]

    @classmethod
    def _root(cls, api_url: str) -> str:
        url = (api_url or cls.DEFAULT_ROOT).rstrip("/")
        for tail in ("/v1beta", "/v1"):
            if url.endswith(tail):
                return url[: -len(tail)]
        return url

    def _headers(self, s) -> dict:
        return {"Content-Type": "application/json", "x-goog-api-key": s.api_key}

    @staticmethod
    def _part(data_url: str) -> dict:
        match = re.match(r"data:([\w.+/-]+);base64,(.+)$", data_url or "", re.S)
        if not match:
            raise ModelError("an image must be a base64 data URL (data:image/png;base64,...)")
        return {"inlineData": {"mimeType": match.group(1), "data": match.group(2)}}

    def request(self, s, prompt, system, images):
        model = (s.model or "").removeprefix("models/")
        config = {"temperature": 0.1, "maxOutputTokens": getattr(s, "model_max_tokens", 3072)}
        if getattr(s, "model_thinking", "off") == "off" and re.search(r"gemini-2\.5-flash", model):
            config["thinkingConfig"] = {"thinkingBudget": 0}     # 2.5 Flash can skip thinking; Pro cannot, so it is left alone
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}, *(self._part(u) for u in images or [])]}],
                "generationConfig": config}
        return f"{self._root(s.api_url)}/v1beta/models/{quote(model, safe='')}:generateContent", self._headers(s), json.dumps(body).encode()

    def reply_text(self, data, raw):
        candidates = data.get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        if not text.strip():
            why = (data.get("promptFeedback") or {}).get("blockReason") or (candidates[0].get("finishReason") if candidates else "")
            if any(isinstance(p, dict) and p.get("thought") for p in parts):
                raise ModelError("Model returned reasoning without final content", body=raw[:1000])
            raise ModelError(f"Model response had no usable content; keys={','.join(data.keys())}" + (f"; reason={why}" if why else ""), body=raw[:1000])
        return text

    def models_request(self, s):
        return self._root(s.api_url) + "/v1beta/models?pageSize=1000", self._headers(s)

    def model_ids(self, data):
        return [m["name"].removeprefix("models/") for m in data.get("models", [])
                if isinstance(m, dict) and m.get("name") and "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])]


PROVIDERS: dict[str, Provider] = {}


def register(provider: Provider) -> Provider:
    PROVIDERS[provider.name] = provider
    return provider


register(OpenAICompatible())
register(Anthropic())
register(Google())


def names() -> list[str]:
    return sorted(PROVIDERS)


def get_provider(name: str | None) -> Provider:
    key = (name or DEFAULT_PROVIDER).strip().lower()
    if key not in PROVIDERS:
        raise ModelError(f"unknown MODEL_PROVIDER {key!r}; choose one of: {', '.join(names())}")
    return PROVIDERS[key]
