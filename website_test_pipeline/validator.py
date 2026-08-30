import ast
import re

class SpecError(ValueError): pass

def validate_python_spec(source: str, url: str) -> None:
    if re.search(r"\.\s*(click|fill|select_option|check|uncheck|press)\s*\(", source) and "action_evidence" not in source:
        raise SpecError("action requires action_evidence")
    try: tree = ast.parse(source)
    except SyntaxError as exc: raise SpecError(f"syntax error at line {exc.lineno}: {exc.msg}") from exc
    if not re.search(r"assert\s+", source): raise SpecError("no assertion found")
    if url not in source: raise SpecError("target URL missing from spec")
    if re.search(r"getByText|:has-text|text=", source): raise SpecError("unstable text selector found")
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) and any(alias.name in {"os", "subprocess", "socket"} for alias in node.names) for node in ast.walk(tree)):
        raise SpecError("unsafe system import found")
