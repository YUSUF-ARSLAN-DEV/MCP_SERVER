"""The protections that sit around every request, whatever the route: headers, cross-site writes, oversized bodies.

The routes already check who is asking and what they may see; this is what a browser and a proxy are told about the
responses, and what is refused before a route runs at all.
"""
from __future__ import annotations
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# The page is one file with its script and style inline and loads nothing from elsewhere, so everything else is closed:
# no other site's script can run in it, it cannot be framed (clickjacking), and a form can only post back to itself.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_BODY_BYTES = 48_000_000          # five 6 MB documents as base64, plus the rest of a run request


def allowed_origins(env: dict | None = None) -> set[str]:
    """Extra origins that may write (ALLOWED_ORIGINS, comma separated): needed only when a proxy rewrites the Host header."""
    raw = (os.environ if env is None else env).get("ALLOWED_ORIGINS", "")
    return {o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip()}


def is_same_origin(origin: str, host: str, extra: set[str]) -> bool:
    """A browser's Origin header names the page that made the request; a write is accepted only from this server's own pages."""
    if origin.rstrip("/").lower() in extra:
        return True
    try:
        return bool(host) and urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


def is_secure_request(request: Request, trust_proxy: bool) -> bool:
    if request.url.scheme == "https":
        return True
    return trust_proxy and request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def install(app: FastAPI, settings) -> None:
    extra = allowed_origins()

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and origin != "null" and not is_same_origin(origin, request.headers.get("host", ""), extra):
                return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
            if origin == "null":                       # a sandboxed or file:// page: never one of ours
                return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
            try:
                length = int(request.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                return JSONResponse({"detail": "That request is too large."}, status_code=413)
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if request.url.path.startswith("/api/"):
            headers.setdefault("Cache-Control", "no-store")            # runs, reports and evidence are one person's, never a cache's
        if settings.cookie_secure or is_secure_request(request, settings.trust_proxy):
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response
