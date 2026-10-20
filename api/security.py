"""What stands between a stranger and the server: which URLs may be tested, who may use the API, how often."""
from __future__ import annotations
import hashlib
import hmac
import ipaddress
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
_BLOCKED_NAMES = ("localhost", ".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


_HINT = "If this is a staging site on your own network, the person running the server can allow it with ALLOW_PRIVATE_TARGETS=1."


class UrlRejected(ValueError):
    """The URL may not be tested; the message is safe to show the person."""


def _resolve(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as exc:
        raise UrlRejected(f"The address '{host}' could not be found.") from exc


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(raw: str, resolver=_resolve, allow_private: bool = False) -> str:
    """The URL to test, cleaned - or UrlRejected. Only public web addresses pass: a visitor-supplied URL must never make
    the server browse its own network (localhost, private ranges, the cloud metadata address 169.254.169.254).
    allow_private is for an operator testing staging sites on their own network (ALLOW_PRIVATE_TARGETS): the address
    check is skipped, the scheme and host checks are not."""
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise UrlRejected("Enter a web address (up to 2048 characters).")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise UrlRejected("That is not a valid web address.") from None
    if parts.scheme.lower() not in {"http", "https"}:
        raise UrlRejected("Only http and https addresses can be tested.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlRejected("That web address has no site name.")
    if not allow_private and (host == "localhost" or host.endswith(_BLOCKED_NAMES)):
        raise UrlRejected(f"Addresses on this server's own network cannot be tested ({host}). {_HINT}")
    try:
        addresses = [str(ipaddress.ip_address(host))]       # the host is itself an IP address
    except ValueError:
        addresses = resolver(host)
    if not addresses:
        raise UrlRejected(f"The address '{host}' could not be found.")
    private = [a for a in addresses if not _is_public(a)]
    if private and not allow_private:
        raise UrlRejected(f"Addresses on a private or internal network cannot be tested. {host} resolves to "
                          f"{', '.join(private)}. {_HINT}")
    netloc = f"[{host}]" if ":" in host else host         # credentials in the URL are dropped
    if port:
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def validate_model_url(raw: str, allow_private: bool, resolver=_resolve) -> str:
    """A model server's address. Public addresses only, like a site to test - unless the operator allowed private ones
    (a model running on this machine or this network)."""
    if not allow_private:
        return validate_url(raw, resolver)
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise UrlRejected("Enter a web address (up to 2048 characters).")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise UrlRejected("That is not a valid web address.") from None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise UrlRejected("Use an http or https address with a host name.")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return urlunsplit((parts.scheme.lower(), host + (f":{port}" if port else ""), parts.path or "/", parts.query, ""))


# ---- who may use the API ------------------------------------------------------------------------------------------
SESSION_TTL_S = 7 * 24 * 3600


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(secret: str, now: float | None = None) -> str:
    expires = str(int((now or time.time()) + SESSION_TTL_S))
    return f"{expires}.{_sign(secret, expires)}"


def verify_token(secret: str, token: str | None, now: float | None = None) -> bool:
    try:
        expires, signature = (token or "").split(".", 1)
        return hmac.compare_digest(signature, _sign(secret, expires)) and int(expires) > (now or time.time())
    except ValueError:
        return False


def code_matches(expected: str, given: str | None) -> bool:
    return bool(expected) and hmac.compare_digest(expected.encode(), (given or "").encode())


class SlidingWindow:
    """At most `limit` events per `window_s` for each key (in memory: enough for one server)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self._events: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        events = self._events[key]
        while events and now - events[0] > self.window_s:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True
