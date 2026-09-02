import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

def canonicalize(raw: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"unsupported URL: {raw}")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))

# Path segments that mean "a real value was never substituted here" - a crawler
# that follows a templated href lands on pages like /map-search-results/-1/null
# which render an empty shell and generate only shallow visibility tests.
_DEGENERATE_SEGMENTS = {
    "null", "undefined", "undefined", "nan", "none", "(null)", "nil",
    "[object object]", "[object%20object]", "false",
}
_PLACEHOLDER_RE = re.compile(r"[{}<>\[\]]|\$\{|^:[a-z_]+$|^%7b|%7d$", re.I)

def is_degenerate(url: str) -> bool:
    """True when a path segment is an unsubstituted placeholder / sentinel."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    for segment in parts.path.split("/"):
        s = unquote(segment).strip().lower()
        if not s:
            continue
        if s in _DEGENERATE_SEGMENTS:
            return True
        if re.fullmatch(r"-\d+", s):  # negative id sentinel: /-1/
            return True
        if _PLACEHOLDER_RE.search(s):
            return True
    return False

def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())

def read_urls(path) -> list[str]:
    result, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        url = canonicalize(line)
        if is_degenerate(url) or url in seen:
            continue
        seen.add(url); result.append(url)
    return result

def merge_extra_urls(discovered: list[str], seed: str, seeds_file, log=None) -> list[str]:
    """Fold operator-supplied same-origin URLs (EXTRA_URLS env + seeds.txt) into
    the crawl result, preserving order and dropping duplicates / degenerates."""
    raw = os.getenv("EXTRA_URLS", "")
    if seeds_file and Path(seeds_file).exists():
        raw = raw + "\n" + Path(seeds_file).read_text(encoding="utf-8")
    try:
        seed_origin = _origin(canonicalize(seed))
    except ValueError:
        seed_origin = None
    extra: list[str] = []
    for token in re.split(r"[\s,]+", raw):
        token = token.strip()
        if not token or token.startswith("#"):
            continue
        try:
            url = canonicalize(token)
        except ValueError:
            continue
        if is_degenerate(url):
            continue
        if seed_origin and _origin(url) != seed_origin:
            continue
        extra.append(url)
    merged = list(dict.fromkeys(discovered + extra))
    added = [u for u in extra if u not in discovered]
    if added and log:
        log.info("merged %d operator-supplied URL(s) into crawl output", len(added))
    return merged
