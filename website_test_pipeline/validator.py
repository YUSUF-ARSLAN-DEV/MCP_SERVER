import ast
import re

class SpecError(ValueError): pass

def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()

def _allowed_tokens(inventory) -> set[str]:
    tokens: set[str] = set()
    for control in getattr(inventory, "controls", None) or []:
        for key in ("selector", "testid", "id", "field_name", "name", "href"):
            value = control.get(key)
            if value: tokens.add(_norm(str(value)))
    for form in getattr(inventory, "forms", None) or []:
        if form.get("selector"): tokens.add(_norm(str(form["selector"])))
        for field in form.get("fields") or []:
            if field: tokens.add(_norm(str(field)))
    for heading in getattr(inventory, "headings", None) or []:
        if heading.get("text"): tokens.add(_norm(str(heading["text"])))
    # the aria snapshot is authoritative for accessible names - harvest every quoted name
    for match in re.finditer(r'"([^"\n]+)"', getattr(inventory, "accessibility", "") or ""):
        tokens.add(_norm(match.group(1)))
    return {token for token in tokens if token}

# .locator("...") - CSS/text selector strings (single string literal, no line crossing)
def _css_locators(source: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"\.locator\(\s*(['\"])([^'\"\n]+)\1", source)]

# get_by_* helpers keyed on user-visible text / labels - high hallucination risk
def _text_locators(source: str) -> list[str]:
    out = [m.group(2) for m in re.finditer(r"\.get_by_(?:label|placeholder|test_id|alt_text|title)\(\s*(['\"])([^'\"\n]+)\1", source)]
    out += [m.group(2) for m in re.finditer(r"\.get_by_role\(\s*['\"][^'\"\n]+['\"][^)\n]*?\bname\s*=\s*(['\"])([^'\"\n]+)\1", source)]
    return out

# identifying fragments inside a CSS selector: #id and [attr=value]; a selector with none is purely structural
_ID_FRAGMENT = re.compile(r"#([A-Za-z0-9_-]+)|\[[A-Za-z-]+\s*[*^$|~]?=\s*['\"]?([^'\"\]]+)")

def _selector_fragments(literal: str) -> list[str]:
    return [(m.group(1) or m.group(2)).strip() for m in _ID_FRAGMENT.finditer(literal) if (m.group(1) or m.group(2))]

def _known(literal: str, tokens: set[str]) -> bool:
    normalized = _norm(literal)
    if not normalized: return True
    return any(token == normalized or (len(token) >= 3 and (token in normalized or normalized in token)) for token in tokens)

_EVIDENCE_CALLS = {"action_evidence", "observation_evidence"}

def _tests_without_evidence(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        calls = {
            child.func.id for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        if not (calls & _EVIDENCE_CALLS):
            offenders.append(node.name)
    return offenders

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
    missing_evidence = _tests_without_evidence(tree)
    if missing_evidence:
        raise SpecError(f"test captures no evidence (needs action_evidence/observation_evidence): {missing_evidence[0]}")
    if inventory is not None:
        tokens = _allowed_tokens(inventory)
        unknown: list[str] = []
        for literal in _css_locators(source):
            fragments = _selector_fragments(literal)
            if fragments and not any(_known(fragment, tokens) for fragment in fragments):
                unknown.append(literal)
        for literal in _text_locators(source):
            if not _known(literal, tokens):
                unknown.append(literal)
        if unknown:
            raise SpecError(f"selector not in observed inventory: {unknown[0]!r}")
