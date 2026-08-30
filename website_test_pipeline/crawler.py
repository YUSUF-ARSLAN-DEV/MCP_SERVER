from __future__ import annotations
from collections import deque
from urllib.parse import urljoin, urlsplit
from .urls import canonicalize


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme.lower(), pa.netloc.lower()) == (pb.scheme.lower(), pb.netloc.lower())


def crawl(page, seed: str, max_depth: int, max_pages: int, log=None, nav_timeout_ms: int | None = None) -> list[str]:
    """Breadth-first walk from ``seed``, following only same-origin links.

    Returns the discovered URLs in visit order (seed first), canonicalized and
    deduplicated, capped at ``max_pages``. Nodes are expanded while their depth is
    below ``max_depth``; a page that fails to load is skipped, not fatal.
    """
    start = canonicalize(seed)
    if nav_timeout_ms:
        page.set_default_navigation_timeout(nav_timeout_ms)
    discovered = [start]
    seen = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue and len(discovered) < max_pages:
        url, depth = queue.popleft()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if log:
                log.warning("crawl: skipped %s (%s)", url, exc)
            continue
        if depth >= max_depth:
            continue
        try:
            hrefs = page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
        except Exception:
            hrefs = []
        for raw in hrefs:
            try:
                candidate = canonicalize(urljoin(url, raw))
            except ValueError:
                continue  # mailto:, javascript:, tel:, and other non-http(s) links
            if candidate in seen or not _same_origin(start, candidate):
                continue
            seen.add(candidate)
            discovered.append(candidate)
            queue.append((candidate, depth + 1))
            if len(discovered) >= max_pages:
                break
    return discovered[:max_pages]
