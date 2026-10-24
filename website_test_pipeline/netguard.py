"""Keep a browser run on the public internet.

The API only checks the address a person TYPED. A public site can still redirect to http://169.254.169.254/ (a cloud
metadata service), or load a script that calls http://localhost:8000/, and the run's browser - which lives on the server's
network - would fetch it. When the server was not told to allow private targets (WTP_BLOCK_PRIVATE=1, set by the API for
every run) each request the browser makes is checked by the address it resolves to, and a private one is refused.

Known limit: the check resolves the name itself, so a name that changes its answer between that lookup and the browser's
own (DNS rebinding) is not caught; network-level egress rules are the complete answer, and the deployment notes say so.
"""
from __future__ import annotations
import ipaddress
import os
import socket
import threading
from urllib.parse import urlsplit

_ALWAYS_FINE = {"data", "blob", "about", "chrome-error"}
_cache: dict[str, bool] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("WTP_BLOCK_PRIVATE", "").strip().lower() in {"1", "true", "yes"}


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def host_allowed(host: str, resolver=None) -> bool:
    """True when every address the host resolves to is public. A name that does not resolve is let through: the request
    fails by itself and nothing was reached."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return False
    with _lock:
        if host in _cache:
            return _cache[host]
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".localdomain", ".home.arpa")):
            verdict = False
            addresses = []
        else:
            try:
                addresses = (resolver or _resolve)(host)
            except OSError:
                addresses = []
            verdict = None
    else:
        verdict = None
    if verdict is None:
        verdict = all(is_public(a) for a in addresses)
    with _lock:
        _cache[host] = verdict
    return verdict


def _resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def url_allowed(url: str, resolver=None) -> bool:
    parts = urlsplit(url)
    if parts.scheme.lower() in _ALWAYS_FINE:
        return True
    if parts.scheme.lower() not in {"http", "https", "ws", "wss"}:
        return False
    return host_allowed(parts.hostname or "", resolver)


def _handler(route) -> None:
    if url_allowed(route.request.url):
        route.fallback()
    else:
        route.abort("blockedbyclient")


def guard(context) -> None:
    """Refuse every request this browser context makes to a private address."""
    context.route("**/*", _handler)


def install_global() -> None:
    """Guard every context created from here on - however the code creates it (a run, a probe, a generated test)."""
    from playwright.sync_api import Browser
    if getattr(Browser, "_wtp_guarded", False):
        return
    original_context, original_page = Browser.new_context, Browser.new_page

    def new_context(self, *args, **kwargs):
        context = original_context(self, *args, **kwargs)
        guard(context)
        return context

    def new_page(self, *args, **kwargs):
        page = original_page(self, *args, **kwargs)
        guard(page.context)
        return page

    Browser.new_context, Browser.new_page, Browser._wtp_guarded = new_context, new_page, True


def maybe_install() -> None:
    if enabled():
        try:
            install_global()
        except Exception:        # no Playwright here (a unit-test run): nothing to guard
            pass
