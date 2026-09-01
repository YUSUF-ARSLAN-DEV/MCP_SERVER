import ast
import re

class SpecError(ValueError): pass

# Roles Playwright accepts for get_by_role (ARIA 1.2 + a few document-structure roles).
_ARIA_ROLES = {
    "alert", "alertdialog", "application", "article", "banner", "blockquote", "button",
    "caption", "cell", "checkbox", "code", "columnheader", "combobox", "complementary",
    "contentinfo", "definition", "deletion", "dialog", "document", "emphasis", "feed",
    "figure", "form", "generic", "grid", "gridcell", "group", "heading", "img", "insertion",
    "link", "list", "listbox", "listitem", "log", "main", "marquee", "math", "menu",
    "menubar", "menuitem", "menuitemcheckbox", "menuitemradio", "meter", "navigation",
    "none", "note", "option", "paragraph", "presentation", "progressbar", "radio",
    "radiogroup", "region", "row", "rowgroup", "rowheader", "scrollbar", "search",
    "searchbox", "separator", "slider", "spinbutton", "status", "strong", "subscript",
    "superscript", "switch", "tab", "table", "tablist", "tabpanel", "term", "textbox",
    "time", "timer", "toolbar", "tooltip", "tree", "treegrid", "treeitem",
}
# Locator factories Playwright actually exposes (get_by_text is handled/blocked separately).
_GET_BY = {"role", "text", "label", "placeholder", "alt_text", "title", "test_id"}

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

# Tag names that occur dozens of times on a page - a bare page.locator("<tag>") is meaningless.
_AMBIGUOUS_TAGS = {"div", "span", "a", "p", "li", "ul", "ol", "button", "img", "label",
                   "input", "i", "b", "em", "strong", "td", "tr", "th", "section"}

def _bare_tag(selector: str) -> bool:
    return selector.strip().lower() in _AMBIGUOUS_TAGS

# #id selectors that look auto-generated - they change on every page load.
_VOLATILE_ID = re.compile(
    r"#[A-Za-z][\w-]*?\d{4,}(?:\s|$|\[|:|>|\.)"      # #newsletter-email-768180
    r"|#(?:radix|mui|headlessui|rc|ember|downshift)[-_:]"
    r"|#[A-Fa-f0-9]{8,}(?:\s|$|\[|:|>|\.)"
)

_EVIDENCE_VERIFY_ARG = {"observation_evidence": 2, "action_evidence": 3}

def _empty_verify(tree: ast.AST) -> str | None:
    """A verify callback that asserts nothing - `lambda: None`, `lambda: True`, ..."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        idx = _EVIDENCE_VERIFY_ARG.get(node.func.id)
        if idx is None or len(node.args) <= idx:
            continue
        verify = node.args[idx]
        if isinstance(verify, ast.Lambda) and isinstance(verify.body, ast.Constant):
            return (f"{node.func.id}() verify callback asserts nothing (lambda: {verify.body.value!r}) - "
                    "it must call expect(...) on something")
    return None

def _locator_misuse(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr, args = node.func.attr, node.args
        if attr == "to_have_url" and args:
            arg = args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "*" in arg.value:
                return ("to_have_url() does not understand globs - use "
                        "expect(page).to_have_url(re.compile(r\"/x\")) for a pattern, or "
                        "page.wait_for_url(\"**/x**\") outside the verify callback")
        if attr == "to_have_count" and args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, int) and args[0].value >= 2:
            return (f"expect(...).to_have_count({args[0].value}) hard-codes a guessed element count - assert "
                    "individual elements (.first .to_be_visible()) or use to_have_count(0)/(1)")
        if attr == "get_by_role" and args and isinstance(args[0], ast.Constant):
            role = str(args[0].value).strip().lower()
            if role and role not in _ARIA_ROLES:
                return f"{role!r} is not a valid ARIA role for get_by_role - use a real role or an id/attribute locator"
        if attr.startswith("get_by_") and attr[len("get_by_"):] not in _GET_BY and attr != "get_by_text":
            return f"page.{attr}() is not a Playwright locator method - use get_by_role / get_by_label / a selector"
        if attr == "locator" and args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            literal = args[0].value
            if _bare_tag(literal):
                return (f"page.locator({literal!r}) matches every <{literal.strip()}> on the page - "
                        "qualify it with an id, attribute, or role")
            if _VOLATILE_ID.search(literal + " "):
                return (f"page.locator({literal!r}) targets an auto-generated id that changes every page load - "
                        "locate by get_by_role / get_by_placeholder / get_by_label instead")
    return None

def validate_python_spec(source: str, url: str, inventory=None) -> None:
    if re.search(r"\.\s*(click|fill|select_option|check|uncheck|press)\s*\(", source) and "action_evidence" not in source:
        raise SpecError("action requires action_evidence")
    try: tree = ast.parse(source)
    except SyntaxError as exc: raise SpecError(f"syntax error at line {exc.lineno}: {exc.msg}") from exc
    if not re.search(r"assert\s+", source) and "expect(" not in source: raise SpecError("no assertion found")
    if url not in source: raise SpecError("target URL missing from spec")
    text_selector = re.search(r"get_by_text|getByText|:has-text|text=", source)
    if text_selector: raise SpecError(f"unstable text selector {text_selector.group(0)!r} - use page.get_by_role(<role>, name=<accessible name from ACCESSIBILITY SIGNALS>) or an id/attribute locator instead")
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) and any(alias.name in {"os", "subprocess", "socket"} for alias in node.names) for node in ast.walk(tree)):
        raise SpecError("unsafe system import found")
    missing_evidence = _tests_without_evidence(tree)
    if missing_evidence:
        raise SpecError(f"test captures no evidence (needs action_evidence/observation_evidence): {missing_evidence[0]}")
    misuse = _locator_misuse(tree)
    if misuse:
        raise SpecError(misuse)
    empty = _empty_verify(tree)
    if empty:
        raise SpecError(empty)
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
