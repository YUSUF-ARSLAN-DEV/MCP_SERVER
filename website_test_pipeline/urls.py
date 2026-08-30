from urllib.parse import urlsplit, urlunsplit

def canonicalize(raw: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"unsupported URL: {raw}")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))

def read_urls(path) -> list[str]:
    result, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        url = canonicalize(line)
        if url not in seen:
            seen.add(url); result.append(url)
    return result
