"""Evidence pack for flow proposal: the per-page inventories compacted into one
deterministic site map (pages, what each can do, and which page leads where).

No model is involved. The pack is what a model later reads to propose flows, so it
holds facts only - nothing here guesses intent.
"""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

_ACTION_TAGS = {"a", "button", "input", "select", "textarea"}
_MAX_ACTIONS = 15
_MAX_HEADINGS = 6


def _path(url: str) -> str:
    parts = urlsplit(url or "")
    return (parts.path or "/") + (("?" + parts.query) if parts.query else "")


def _same_page_target(base: str, href: str) -> str | None:
    """Absolute same-origin path for a real link; None for anchors, mailto, js, other sites."""
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    absolute = urljoin(base, href)
    if urlsplit(absolute).netloc != urlsplit(base).netloc:
        return None
    return _path(absolute.split("#")[0])


def load_inventories(artifacts_dir: Path) -> list[dict]:
    out = []
    for f in sorted(Path(artifacts_dir).glob("*.inventory.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _kind(control: dict) -> str:
    tag = control.get("tag")
    if tag == "a":
        return "link"
    if tag == "select":
        return "select"
    if tag in {"input", "textarea"}:
        return control.get("type") or "input"
    return "button"


def _page_summary(inv: dict) -> dict:
    url = inv.get("url", "")
    actions = []
    for c in inv.get("controls") or []:
        if c.get("region") == "chrome" or c.get("hidden") or c.get("disabled"):
            continue
        if c.get("tag") not in _ACTION_TAGS or not (c.get("name") or c.get("selector")):
            continue
        # a <select>'s accessible name is every option concatenated - its selector reads better
        label = (c.get("selector") if c.get("tag") == "select" else None) or c.get("name") or c.get("selector") or ""
        entry = {"kind": _kind(c), "name": label[:40]}
        if c.get("tag") == "a":
            target = _same_page_target(url, c.get("href") or "")
            if target is None:
                continue
            entry["to"] = target
        if c.get("tag") == "select":
            entry["options"] = len(c.get("options") or [])
        actions.append(entry)
    reveals = []
    for r in inv.get("revealed") or []:
        item = {"trigger": (r.get("trigger") or "")[:40], "effect": r.get("effect")}
        if r.get("effect") == "navigates":
            item["to"] = _path(r.get("to") or "")
        elif r.get("effect") == "reveals":
            item["controls"] = len(r.get("controls") or [])
        reveals.append(item)
    probe = inv.get("primary_flow")
    return {
        "path": _path(url),
        "url": url,
        "title": (inv.get("title") or "")[:80],
        "headings": [h["text"][:60] for h in (inv.get("headings") or [])
                     if h.get("text") and not h.get("hidden") and not h.get("in_feed")][:_MAX_HEADINGS],
        "actions": actions[:_MAX_ACTIONS],
        "forms": [{"fields": sorted({f for f in (fm.get("fields") or []) if f})[:8]}
                  for fm in inv.get("forms") or []],
        "embeds": sorted({e.get("kind") or "embed" for e in inv.get("embeds") or []}),
        "reveals": reveals,
        "probe": ({"action": probe.get("action"), "effect": probe.get("effect"),
                   "to": _path(probe["to"]) if probe.get("to") else None} if probe else None),
    }


def _shared_nav(inventories: list[dict]) -> list[dict]:
    """Chrome links that appear on at least half the pages - shown once, not per page."""
    seen: dict[tuple, int] = {}
    for inv in inventories:
        keys = set()
        for c in inv.get("controls") or []:
            if c.get("tag") == "a" and c.get("region") == "chrome" and c.get("name"):
                target = _same_page_target(inv.get("url", ""), c.get("href") or "")
                if target:
                    keys.add((c["name"][:40], target))
        for key in keys:
            seen[key] = seen.get(key, 0) + 1
    need = max(1, (len(inventories) + 1) // 2)
    return [{"name": n, "to": t} for (n, t), count in sorted(seen.items()) if count >= need]


def build_site_map(inventories: list[dict]) -> dict:
    pages = [_page_summary(inv) for inv in inventories]
    known = {p["path"] for p in pages}
    edges, seen = [], set()
    for p in pages:
        targets = [(a["to"], a["name"]) for a in p["actions"] if a.get("to")]
        targets += [(r["to"], r["trigger"]) for r in p["reveals"] if r.get("to")]
        if p["probe"] and p["probe"].get("to"):
            targets.append((p["probe"]["to"], p["probe"]["action"] or "probe"))
        for to, via in targets:
            key = (p["path"], to)
            if key in seen or to == p["path"]:
                continue
            seen.add(key)
            edges.append({"from": p["path"], "to": to, "via": via, "explored": to.split("?")[0] in {k.split("?")[0] for k in known}})
    return {"pages": pages, "nav": _shared_nav(inventories), "edges": edges}


def render_site_map(site_map: dict, max_chars: int = 8000) -> str:
    lines = [f'SITE MAP ({len(site_map["pages"])} pages explored)']
    if site_map["nav"]:
        lines.append("NAV (on most pages): " + "; ".join(f'{n["name"]} -> {n["to"]}' for n in site_map["nav"]))
    for p in site_map["pages"]:
        lines.append(f'PAGE {p["path"]} "{p["title"]}"')
        if p["headings"]:
            lines.append("  headings: " + " | ".join(p["headings"]))
        if p["actions"]:
            lines.append("  actions: " + "; ".join(
                f'{a["kind"]} "{a["name"]}"' + (f' -> {a["to"]}' if a.get("to") else "")
                + (f' ({a["options"]} options)' if a.get("options") else "") for a in p["actions"]))
        for f in p["forms"]:
            lines.append("  form fields: " + ", ".join(f["fields"]))
        if p["embeds"]:
            lines.append("  embeds: " + ", ".join(p["embeds"]))
        for r in p["reveals"]:
            what = (f'navigates to {r["to"]}' if r.get("to") else f'reveals {r["controls"]} control(s)'
                    if r.get("controls") else str(r.get("effect")))
            lines.append(f'  clicking "{r["trigger"]}" {what}')
        if p["probe"]:
            lines.append(f'  probe: {p["probe"]["action"]} -> {p["probe"]["effect"]}'
                         + (f' {p["probe"]["to"]}' if p["probe"].get("to") else ""))
    if site_map["edges"]:
        lines.append("LINKS: " + "; ".join(f'{e["from"]} -> {e["to"]} via "{e["via"]}"'
                                          + ("" if e["explored"] else " [not explored]") for e in site_map["edges"]))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0] + "\n... (truncated)"
