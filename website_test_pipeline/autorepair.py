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

from .validator import _norm, _ID_FRAGMENT

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
            segment = ast.get_source_segment(source, node)  # handles utf-8 byte offsets
            if not segment or not _targets_strictmode(segment, ambiguous):
                continue
            # already scoped? (offset-free check - ast col_offsets are utf-8 bytes and
            # break on non-ASCII source, so match on the segment text instead)
            if re.search(re.escape(segment) + r"\s*\.\s*(?:first|last)\b|"
                         + re.escape(segment) + r"\s*\.\s*nth\s*\(", source):
                continue
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


def _loc_key(segment: str) -> str:
    """Normalise a locator expression for comparison - drop all whitespace and a
    trailing .first / .last / .nth(...)."""
    s = re.sub(r"\s+", "", segment)
    s = re.sub(r"\.(?:first|last)$", "", s)
    return re.sub(r"\.nth\([^)]*\)$", "", s)


def _repair_select_value_assert(source: str) -> tuple[str, int]:
    """`select_option(label=/text=...)` already fails loudly if the option is
    missing, so a following `expect(sel).to_have_value("<x>")` adds no signal and
    breaks the instant the option's value attribute differs from its visible label
    (a country <option> reads "Aruba" but its value is "223"; Drupal / WP term ids
    are environment-specific). The value attribute is unknowable from the page, so
    downgrade ANY such assertion to `not_to_have_value("")` - a real 'something got
    selected' check. (Only fires when the same locator was selected by label/text.)"""
    count = 0
    for _ in range(20):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        select_locs: set[str] = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "select_option"
                    and any(k.arg in {"label", "text"} for k in node.keywords)):
                seg = ast.get_source_segment(source, node.func.value)
                if seg:
                    select_locs.add(_loc_key(seg))
        if not select_locs:
            break
        hit = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "to_have_value" and len(node.args) == 1
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.strip()):   # any non-empty asserted value
                continue
            expect_call = node.func.value
            if not (isinstance(expect_call, ast.Call) and isinstance(expect_call.func, ast.Name)
                    and expect_call.func.id == "expect" and expect_call.args):
                continue
            recv = ast.get_source_segment(source, expect_call.args[0])
            if not recv or _loc_key(recv) not in select_locs:
                continue
            old = ast.get_source_segment(source, node)
            if old:
                hit = (old, old.rsplit(".to_have_value", 1)[0] + '.not_to_have_value("")')
            break
        if hit is None or hit[0] == hit[1]:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


_LONG_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def _repair_fragile_heading(source: str) -> tuple[str, int]:
    """`get_by_role('heading', name='<long sentence>')` breaks on whitespace /
    line-break drift between the crawl snapshot and the live render, and the
    validator rejects it. Rewrite the name to `re.compile(r'<longest word>')` -
    exactly the fix ASSERTION_RULES tells the model to make itself. qwen keeps
    shipping the exact-string form on the Arabic wizard pages and burning the
    retry budget on it."""
    count = 0
    for _ in range(20):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        hit = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get_by_role" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).strip().lower() == "heading"):
                continue
            name_kw = next((k for k in node.keywords if k.arg == "name"), None)
            if name_kw is None or not (isinstance(name_kw.value, ast.Constant)
                                       and isinstance(name_kw.value.value, str)):
                continue
            literal = name_kw.value.value
            if len(literal.split()) <= 6 and len(literal) <= 70:
                continue
            words = _LONG_WORD.findall(literal)
            if not words:
                continue
            token = max(words, key=len)
            old = ast.get_source_segment(source, name_kw.value)
            if not old:
                continue
            hit = (old, f"re.compile(r'{token}')")
            break
        if hit is None or hit[0] == hit[1]:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


def _select_option_texts(inventory) -> dict[str, set[str]]:
    """{normalised select id/name/selector -> {observed option texts}} for every
    <select> whose options the explorer captured."""
    out: dict[str, set[str]] = {}
    for control in getattr(inventory, "controls", None) or []:
        if control.get("tag") != "select":
            continue
        opts = {_norm(str(o)) for o in (control.get("options") or []) if str(o).strip()}
        if len(opts) < 2:
            continue
        for key in ("selector", "id", "field_name"):
            if control.get(key):
                out[_norm(str(control[key]))] = opts
    return out


def _repair_bad_select_label(source: str, inventory) -> tuple[str, int]:
    """`select_option(label='X')` where X is not one of the <select>'s observed
    option texts -> `select_option(index=1)`. The model guesses channel / plan /
    category labels it never saw; Playwright then times out with 'did not find
    some options'. Picking the first real option by position always resolves."""
    if inventory is None:
        return source, 0
    opt_map = _select_option_texts(inventory)
    if not opt_map:
        return source, 0
    count = 0
    for _ in range(20):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        hit = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "select_option"):
                continue
            kw = next((k for k in node.keywords if k.arg in {"label", "text"}), None)
            if kw is None or not (isinstance(kw.value, ast.Constant)
                                  and isinstance(kw.value.value, str)):
                continue
            recv = ast.get_source_segment(source, node.func.value) or ""
            targets = re.findall(r"#([A-Za-z0-9_-]+)", recv)
            targets += [m.group(2) for m in re.finditer(r"name\s*=\s*(['\"])([^'\"]+)\1", recv)]
            opts = next((opt_map[_norm(t)] for t in targets if _norm(t) in opt_map), None)
            if opts is None:
                continue
            label = _norm(kw.value.value)
            if not label or any(label == o or (len(o) >= 3 and (label in o or o in label)) for o in opts):
                continue
            recv_seg = ast.get_source_segment(source, node.func.value)
            old = ast.get_source_segment(source, node)
            if not recv_seg or not old:
                continue
            hit = (old, f"{recv_seg}.select_option(index=1)")
            break
        if hit is None or hit[0] == hit[1]:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


_MENU_TOKENS = re.compile(r"multiselect|dropdown-menu|ui-menu|select2|chosen-drop|\blistbox\b|option-\d", re.I)


def _repair_close_menu(source: str) -> tuple[str, int]:
    """A test that clicks a widget which opens a menu/listbox, then clicks
    something else: the still-open menu overlay intercepts the second click and it
    times out. Insert `page.keyboard.press('Escape')` right after the
    menu-opening step. (LOCATOR_RULES already tells the model to do this; qwen
    ignores it on the jQuery-UI multiselect pages.)"""
    count = 0
    for _ in range(10):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        assigned: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned[node.targets[0].id] = ast.get_source_segment(source, node.value) or ""

        def _expand(text: str) -> str:  # splice in any `x = page.locator(...)` bodies the line references
            extra = " ".join(v for k, v in assigned.items() if re.search(rf"\b{re.escape(k)}\b", text))
            return text + " " + extra

        hit = None
        for func in ast.walk(tree):
            if not (isinstance(func, ast.FunctionDef) and func.name.startswith("test_")):
                continue
            stmts = list(func.body)
            for i, stmt in enumerate(stmts):
                seg = ast.get_source_segment(source, stmt) or ""
                if ".click(" not in seg or not _MENU_TOKENS.search(_expand(seg)):
                    continue
                # already followed by an Escape? (the guard must look at the NEXT
                # statement, not this one - the press is inserted as a sibling)
                nxt = ast.get_source_segment(source, stmts[i + 1]) if i + 1 < len(stmts) else ""
                if nxt and "keyboard.press" in nxt and "Escape" in nxt:
                    continue
                rest = [ast.get_source_segment(source, s) or "" for s in stmts[i + 1:]]
                later = "\n".join(rest)
                if ".click(" not in later:
                    continue
                # don't close the menu if the very next click is INTO it (the model
                # is still selecting an option) - only Escape before a click elsewhere
                first_click = next((r for r in rest if ".click(" in r), "")
                if _MENU_TOKENS.search(_expand(first_click)):
                    continue
                indent = " " * stmt.col_offset
                hit = (seg, seg + f'\n{indent}page.keyboard.press("Escape")')
                break
            if hit:
                break
        if hit is None or hit[0] == hit[1]:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


def _readonly_tokens(inventory) -> set[str]:
    tokens: set[str] = set()
    groups = [getattr(inventory, "controls", None) or []]
    for entry in getattr(inventory, "revealed", None) or []:
        groups.append(entry.get("controls") or [])
    for group in groups:
        for control in group:
            if not control.get("readonly"):
                continue
            for key in ("selector", "id", "field_name", "name"):
                if control.get(key):
                    tokens.add(_norm(str(control[key])))
    return {t for t in tokens if t}


def _repair_readonly_fill(source: str, inventory) -> tuple[str, int]:
    """`action_evidence(page, l, lambda: fld.fill(v), lambda: expect(fld).to_have_value(v), dir)`
    where `fld` is a readonly display field (the wizard's default-password box, a
    generated code) - the fill times out 'element is not editable'. Replace the
    whole step with an observation that the field is visible."""
    if inventory is None:
        return source, 0
    readonly = _readonly_tokens(inventory)
    if not readonly:
        return source, 0
    count = 0
    for _ in range(15):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        assigned: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned[node.targets[0].id] = ast.get_source_segment(source, node.value) or ""
        hit = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "action_evidence"):
                continue
            seg = ast.get_source_segment(source, node) or ""
            m = re.search(r"lambda:\s*(?P<loc>[A-Za-z_][\w.\"'\[\]()=\-\s]*?)\.(?:fill|type)\(", seg)
            if not m:
                continue
            loc = m.group("loc").strip()
            expanded = loc + " " + assigned.get(loc, "")
            frags = [mm.group(1) or mm.group(2) for mm in _ID_FRAGMENT.finditer(expanded)]
            frags += [mm.group(2) for mm in re.finditer(r"name\s*=\s*(['\"])([^'\"]+)\1", expanded)]
            if not any(_norm(f) in readonly for f in frags if f):
                continue
            lbl = re.search(r"action_evidence\(\s*page\s*,\s*(['\"][^'\"]*['\"])", seg)
            if not lbl:
                continue
            indent = " " * node.col_offset
            new = (f"observation_evidence(page, {lbl.group(1)}, "
                   f"lambda: expect({loc}).to_be_visible(), evidence_dir)")
            hit = (seg, new)
            break
        if hit is None or hit[0] == hit[1]:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


_MS_OPTION = re.compile(r"ui-multiselect[\w-]*option-\d|ui-multiselect[\w-]*optionlabel", re.I)


_MS_OPTION_ID = re.compile(r"""["'#]?(ui-multiselect-[\w-]*?option-\d+)["']?""")


def _repair_multiselect_option_click(source: str) -> tuple[str, int]:
    """`page.locator('#ui-multiselect-x-option-1').click()` clicks the 1px sr-only
    checkbox itself and times out 'element is not visible'. The clickable element
    is its <label>. Rewrite the locator to `label[for="..."]`."""
    count = 0
    for _ in range(20):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        # every `x = page.locator("...")` and the raw string literals in .locator("...").click()
        literal_nodes: list[ast.Constant] = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "locator" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                literal_nodes.append(node.args[0])
        hit = None
        for lit in literal_nodes:
            sel = lit.value
            m = _MS_OPTION_ID.search(sel)
            if not m or sel.strip().startswith("label"):
                continue
            old_arg = ast.get_source_segment(source, lit)
            if not old_arg:
                continue
            hit = (old_arg, f'"label[for=\'{m.group(1)}\']"')
            break
        if hit is None or hit[0] == hit[1]:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


def _repair_multiselect_option_visibility(source: str) -> tuple[str, int]:
    """jQuery-UI multiselect renders its real option checkboxes as 1px sr-only
    nodes - `expect(page.locator('#ui-multiselect-x-option-1')).to_be_visible()`
    (or .to_be_checked()) always fails 'Actual value: hidden'. The honest check is
    that the node exists once. Downgrade to .to_have_count(1)."""
    count = 0
    for _ in range(20):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        assigned: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned[node.targets[0].id] = ast.get_source_segment(source, node.value) or ""
        hit = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"to_be_visible", "to_be_checked", "to_be_hidden"}):
                continue
            recv = ast.get_source_segment(source, node.func.value) or ""
            m = re.fullmatch(r"expect\(\s*([A-Za-z_]\w*)\s*\)", recv.strip())
            if m and m.group(1) in assigned:
                recv = recv + " " + assigned[m.group(1)]
            if not _MS_OPTION.search(recv):
                continue
            old = ast.get_source_segment(source, node)
            if not old:
                continue
            new = old.rsplit(f".{node.func.attr}", 1)[0] + ".to_have_count(1)"
            hit = (old, new)
            break
        if hit is None or hit[0] == hit[1]:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


_MAP_CONTENT = re.compile(r"\.(?:gm-style|leaflet-container)\b|canvas['\"]\s*\)")

def _repair_map_settle(source: str, inventory) -> tuple[str, int]:
    """A test that asserts a map's rendered content (.gm-style / .leaflet-container
    / canvas) needs a settle before the screenshot or it captures a blank grey
    box. Inject page.wait_for_timeout(3000) after _open(page) when the model left
    it out."""
    if not any((e or {}).get("kind") == "map" for e in (getattr(inventory, "embeds", None) or [])):
        return source, 0
    count = 0
    for _ in range(10):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            break
        hit = None
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
                continue
            seg = ast.get_source_segment(source, node) or ""
            if "wait_for_timeout" in seg or not _MAP_CONTENT.search(seg):
                continue
            m = re.search(r"\n([ \t]+)_open\(page\)[^\n]*", seg)
            if not m:
                continue
            hit = (seg, seg[:m.end()] + f"\n{m.group(1)}page.wait_for_timeout(3000)" + seg[m.end():])
            break
        if hit is None:
            break
        updated = source.replace(hit[0], hit[1], 1)
        if updated == source:
            break
        source = updated
        count += 1
    return source, count


_ICON_GLYPHS = r"[\ue000-\uf8ff\s]*"      # private-use codepoints: icon fonts (Font Awesome, ...) and whitespace


def _known_control_names(inventory) -> set[str]:
    names: set[str] = set()
    groups = [getattr(inventory, "controls", None) or []]
    for entry in getattr(inventory, "revealed", None) or []:
        groups.append(entry.get("controls") or [])
    for group in groups:
        for control in group:
            if control.get("name"):
                names.add(str(control["name"]))
    return names


def _repair_icon_glyph_names(source: str, inventory) -> tuple[str, int]:
    """A button drawn as `<i class="fa fa-sign-in"> Login</i>` has the accessible name "<icon glyph> Login", so
    get_by_role('button', name='Login', exact=True) matches nothing. Rewrite the name of an exact match on a control
    the explorer really saw into a regex that also allows the icon glyph and spaces around it. Only names present in
    the inventory are touched, so a hallucinated name is still caught by the validator."""
    known = _known_control_names(inventory)
    if not known:
        return source, 0
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0
    edits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get_by_role"):
            continue
        keywords = {k.arg: k.value for k in node.keywords}
        exact, name = keywords.get("exact"), keywords.get("name")
        if not (isinstance(exact, ast.Constant) and exact.value is True
                and isinstance(name, ast.Constant) and isinstance(name.value, str) and name.value in known
                and '"' not in name.value and "\n" not in name.value):
            continue
        edits.append((name.lineno, name.col_offset, name.end_lineno, name.end_col_offset, name.value))
    if not edits:
        return source, 0
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    raw = source.encode("utf-8")
    for l1, c1, l2, c2, value in sorted(edits, reverse=True):
        start, end = offsets[l1 - 1] + c1, offsets[l2 - 1] + c2
        pattern = "^" + _ICON_GLYPHS + re.escape(value) + _ICON_GLYPHS + "$"
        raw = raw[:start] + f're.compile(r"{pattern}")'.encode("utf-8") + raw[end:]
    updated = raw.decode("utf-8")
    if not re.search(r"^\s*import re\b|^\s*from re import", updated, re.M):
        updated = "import re\n" + updated
    return updated, len(edits)


def _recorded_roles(inventory) -> dict[str, set[str]]:
    """{control name -> ARIA roles the explorer recorded for it}. Non-ARIA 'roles' (an <input type>, 'select-one')
    are left out: they are not something get_by_role() understands."""
    from .validator import _ARIA_ROLES
    roles: dict[str, set[str]] = {}
    groups = [getattr(inventory, "controls", None) or []]
    for entry in getattr(inventory, "revealed", None) or []:
        groups.append(entry.get("controls") or [])
    for group in groups:
        for control in group:
            name, role = control.get("name"), control.get("role")
            if name and role in _ARIA_ROLES:
                roles.setdefault(str(name), set()).add(role)
    return roles


def _repair_role_from_inventory(source: str, inventory) -> tuple[str, int]:
    """The model guesses a role from the tag (<a> -> 'link'), but the page said otherwise: `<a role="button"
    aria-label="Cart, empty">` is a button, and get_by_role('link', name='Cart, empty') finds nothing. When a control
    the explorer saw has exactly one recorded ARIA role and the spec asks for a different one, use the recorded one."""
    recorded = _recorded_roles(inventory)
    if not recorded:
        return source, 0
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0
    edits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get_by_role"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            continue
        name = next((k.value for k in node.keywords if k.arg == "name"), None)
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            continue
        roles = recorded.get(name.value)
        asked = node.args[0]
        if roles and len(roles) == 1 and asked.value not in roles:
            edits.append((asked.lineno, asked.col_offset, asked.end_lineno, asked.end_col_offset, next(iter(roles))))
    if not edits:
        return source, 0
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    raw = source.encode("utf-8")
    for l1, c1, l2, c2, role in sorted(edits, reverse=True):
        start, end = offsets[l1 - 1] + c1, offsets[l2 - 1] + c2
        raw = raw[:start] + repr(role).encode("utf-8") + raw[end:]
    return raw.decode("utf-8"), len(edits)


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
        source, n = _repair_select_value_assert(source)
        if n:
            applied.append(f"downgraded {n} opaque select-value assertion(s) to not_to_have_value('')")
        source, n = _repair_bad_select_label(source, inventory)
        if n:
            applied.append(f"repointed {n} guessed select_option(label=) to index=1")
        source, n = _repair_fragile_heading(source)
        if n:
            applied.append(f"rewrote {n} fragile exact-heading name(s) to re.compile()")
        source, n = _repair_role_from_inventory(source, inventory)
        if n:
            applied.append(f"corrected {n} guessed role(s) to the role the page really has")
        source, n = _repair_missing_first(source, inventory)
        if n:
            applied.append(f"added .first to {n} strict-mode locator(s)")
        source, n = _repair_icon_glyph_names(source, inventory)
        if n:
            applied.append(f"made {n} exact role name(s) tolerant of icon-font glyphs")
        source, n = _repair_multiselect_option_click(source)
        if n:
            applied.append(f"retargeted {n} multiselect-option click(s) to the visible <label>")
        source, n = _repair_close_menu(source)
        if n:
            applied.append(f"inserted Escape after {n} menu-opening step(s)")
        source, n = _repair_multiselect_option_visibility(source)
        if n:
            applied.append(f"downgraded {n} sr-only multiselect-option visibility assert(s) to to_have_count(1)")
        source, n = _repair_readonly_fill(source, inventory)
        if n:
            applied.append(f"converted {n} .fill() on a readonly field to a visibility check")
        source, n = _repair_map_settle(source, inventory)
        if n:
            applied.append(f"added a map settle wait to {n} test(s)")
        if applied:
            ast.parse(source)
    except Exception:
        return original, []
    return source, applied
