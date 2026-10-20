"""Catch a broken .env before a run spends minutes on it. Two real failures this exists for: a typo in a numeric
setting (`EXECUTE_CONCURRENCY=1rerun t` reached pytest as `-n "1rerun t"`), and a MODEL_NAME the endpoint does not
serve (every model call 404'd, page after page)."""
from __future__ import annotations
import json, os, urllib.request

INT_SETTINGS = ("EXECUTE_CONCURRENCY", "GENERATE_CONCURRENCY")


def check_env(environ=os.environ) -> list[str]:
    """Problems in the settings that are read raw (not through config.py's validated fields). Empty = fine."""
    problems = []
    for name in INT_SETTINGS:
        value = (environ.get(name) or "").strip()
        if value and not (value.isdigit()):
            problems.append(f"{name}={value!r} is not a whole number (e.g. 1 or 4)")
    url = (environ.get("API_URL") or "").strip()
    if url and (not url.startswith(("http://", "https://")) or any(c.isspace() for c in url)):
        problems.append(f"API_URL={url!r} is not a single http(s) URL")
    return problems


def models_url(api_url: str) -> str:
    return api_url.split("/chat/completions")[0].rstrip("/") + "/models"


def _fetch_model_ids(url: str, api_key: str, timeout: float) -> list[str]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return [m["id"] for m in json.load(response).get("data", []) if isinstance(m, dict) and m.get("id")]


def check_model(settings, fetch=_fetch_model_ids, timeout: float = 10.0) -> str | None:
    """A message when the endpoint answers and does not serve settings.model; None when it does, or when the
    endpoint cannot say (down, no /models route, auth) - that is not evidence the model is wrong."""
    try:
        ids = fetch(models_url(settings.api_url), settings.api_key, timeout)
    except Exception:
        return None
    if not ids or settings.model in ids:
        return None
    return f"MODEL_NAME={settings.model!r} is not served by {settings.api_url}; it serves: {', '.join(ids)}"
