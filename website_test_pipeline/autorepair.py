"""Mechanical repair of the model's spec before it reaches the validator.

qwen3-coder keeps making the same three shape-level mistakes - a `:has-text()`
locator, an `expect(a) and expect(b)` verify chain, a strict-mode locator with no
`.first`. Each is a deterministic rewrite. Doing it here saves a whole
model round-trip (and on the hard Arabic pages, stops the retry budget being
burned on the same violation five times and shipping a skip stub).

`repair_spec` is conservative: every rewrite is independently guarded, and if the
result does not parse the original source is returned untouched.
"""
from __future__ import annotations
import ast
import re

from .validator import _norm

_TAG_ROLE = {
    "button": "button", "a": "link", "nav": "navigation",
    "h1": "heading", "h2": "heading", "h3": "heading",
    "h4": "heading", "h5": "heading", "h6": "heading",
}

# .locator("<prefix>:has-text('<text>')") - capture the leading tag/role and the text.
_HAS_TEXT = re.compile(
    r"""\.locator\(\s*(['"])(?P<prefix>[^'"]*?):has-text\(\s*(['"])(?P<text>(?:(?!\3).)*)\3\s*\)(?P<suffix>[^'"]*?)\1\s*\)"""
)
_REPEATED_ATTR = re.compile(r"\[\s*(?:name|type|value)\s*[*^$|~]?=", re.I)
_LOCATOR_ATTRS = {"locator", "get_by_role"}


def _role_for_prefix(prefix: str) -> str | None:
    m = re.match(r"\s*([a-zA-Z][a-zA-Z0-9]*)", prefix)
    if m and m.group(1).lower() in _TAG_ROLE:
        return _TAG_ROLE[m.group(1).lower()]
    m = re.search(r"""\[\s*role\s*=\s*['"]?([a-zA-Z]+)""", prefix)
    if m:
        return m.group(1).lower()
    return None


def _repair_has_text(source: str) -> tuple[str, int]:
    count = 0

    def sub(m: re.Match) -> str:
        nonlocal count
        if m.group("suffix").strip():
            return m.group(0)  # extra selector after :has-text - too complex to convert
        role = _role_for_prefix(m.group("prefix"))
        if not role:
            return m.group(0)
        text = m.group("text")
        q = "'" if '"' in text and "'" not in text else '"'
        if q in text:
            return m.group(0)
        count += 1
        return f".get_by_role({q}{role}{q}, name={q}{text}{q}, exact=True)"

    return _HAS_TEXT.sub(sub, source), count


def _repair_bool_chain(source: str) -> tuple[str, int]:
    """`lambda: expect(a).to_be_visible() and expect(b)...` -> `lambda: [expect(a)..., expect(b)...]`."""
    count = 0
    for _ in range(20):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        target = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Lambda) and isinstance(node.body, ast.BoolOp)
                    and all(isinstance(v, ast.Call) for v in node.body.values)):
                target = node
                break
        if target is None:
            break
        old = ast.get_source_segment(source, target)
        if old is None:
            break
        new_node = ast.Lambda(
            args=target.args,
            body=ast.List(elts=target.body.values, ctx=ast.Load()),
        )
        new = ast.unparse(new_node)
        updated = source.replace(old, new, 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


def _ambiguous_tokens(inventory) -> set[str]:
    tokens: set[str] = set()
    for control in getattr(inventory, "controls", None) or []:
        if control.get("ambiguous"):
            for key in ("selector", "id", "field_name", "name"):
                if control.get(key):
                    tokens.add(_norm(str(control[key])))
    return tokens


def _targets_strictmode(segment: str, ambiguous: set[str]) -> bool:
    if _REPEATED_ATTR.search(segment) and "#" not in segment:
        return True
    targets = re.findall(r"#([A-Za-z0-9_-]+)", segment)
    targets += [m.group(2) for m in re.finditer(r"name\s*=\s*(['\"])([^'\"]+)\1", segment)]
    return any(_norm(t) in ambiguous for t in targets if t)


def _repair_missing_first(source: str, inventory) -> tuple[str, int]:
    ambiguous = _ambiguous_tokens(inventory)
    count = 0
    for _ in range(30):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        hit = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _LOCATOR_ATTRS):
                continue
            segment = ast.get_source_segment(source, node)
            if not segment or not _targets_strictmode(segment, ambiguous):
                continue
            tail = source[_seg_end(source, node):][:16]
            if re.match(r"\s*\.\s*(?:first|last)\b|\s*\.\s*nth\s*\(", tail):
                continue  # already scoped
            hit = segment
            break
        if hit is None:
            break
        updated = source.replace(hit, hit + ".first", 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


def _seg_end(source: str, node: ast.AST) -> int:
    lines = source.splitlines(keepends=True)
    offset = sum(len(l) for l in lines[: (node.end_lineno or 1) - 1])
    return offset + (node.end_col_offset or 0)


def repair_spec(source: str, inventory=None) -> tuple[str, list[str]]:
    """Return (possibly rewritten source, list of human-readable repairs applied).
    On any parse failure of the rewritten source, return the original untouched."""
    original = source
    applied: list[str] = []
    try:
        source, n = _repair_has_text(source)
        if n:
            applied.append(f"rewrote {n} :has-text() locator(s) to get_by_role")
        source, n = _repair_bool_chain(source)
        if n:
            applied.append(f"rewrote {n} and/or assertion chain(s) to a list")
        source, n = _repair_missing_first(source, inventory)
        if n:
            applied.append(f"added .first to {n} strict-mode locator(s)")
        if applied:
            ast.parse(source)
    except Exception:
        return original, []
    return source, applied
