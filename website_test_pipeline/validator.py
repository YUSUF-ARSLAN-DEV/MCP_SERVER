import ast
import re

class SpecError(ValueError): pass

def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()

def _allowed_tokens(inventory) -> set[str]:
    tokens: set[str] = set()
    for control in getattr(inventory, "controls", None) or []:
        for key in ("selector", "testid", "id", "field_name", "name"):
            value = control.get(key)
            if value: tokens.add(_norm(str(value)))
    for form in getattr(inventory, "forms", None) or []:
        if form.get("selector"): tokens.add(_norm(str(form["selector"])))
        for field in form.get("fields") or []:
            if field: tokens.add(_norm(str(field)))
    for heading in getattr(inventory, "headings", None) or []:
        if heading.get("text"): tokens.add(_norm(str(heading["text"])))
    return {token for token in tokens if token}

def _locator_literals(source: str) -> list[str]:
    literals: list[str] = []
    literals += [m.group(2) for m in re.finditer(r"\.locator\(\s*(['\"])(.+?)\1", source, re.S)]
    literals += [m.group(2) for m in re.finditer(r"\.get_by_(?:label|placeholder|test_id|alt_text|title)\(\s*(['\"])(.+?)\1", source, re.S)]
    literals += [m.group(2) for m in re.finditer(r"\.get_by_role\(\s*['\"][^'\"]+['\"][^)]*?\bname\s*=\s*(['\"])(.+?)\1", source, re.S)]
    return literals

def _known(literal: str, tokens: set[str]) -> bool:
    normalized = _norm(literal)
    if not normalized: return True
    return any(token == normalized or (len(token) >= 3 and (token in normalized or normalized in token)) for token in tokens)

def validate_python_spec(source: str, url: str, inventory=None) -> None:
    if re.search(r"\.\s*(click|fill|select_option|check|uncheck|press)\s*\(", source) and "action_evidence" not in source:
        raise SpecError("action requires action_evidence")
    try: tree = ast.parse(source)
    except SyntaxError as exc: raise SpecError(f"syntax error at line {exc.lineno}: {exc.msg}") from exc
    if not re.search(r"assert\s+", source) and "expect(" not in source: raise SpecError("no assertion found")
    if url not in source: raise SpecError("target URL missing from spec")
    if re.search(r"getByText|:has-text|text=", source): raise SpecError("unstable text selector found")
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) and any(alias.name in {"os", "subprocess", "socket"} for alias in node.names) for node in ast.walk(tree)):
        raise SpecError("unsafe system import found")
    if inventory is not None:
        tokens = _allowed_tokens(inventory)
        unknown = sorted({literal for literal in _locator_literals(source) if not _known(literal, tokens)})
        if unknown:
            raise SpecError(f"selector not in observed inventory: {unknown[0]!r}")
