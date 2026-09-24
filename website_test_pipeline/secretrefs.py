"""References to secrets in flow steps: `{env:AUTH_SITE_DEFAULT_PASSWORD}`, never the secret itself.

A flow step that types a login detail carries only the NAME of the .env variable that holds it. The name is what
flows.json, the plain-language journey and the generated test contain; the value is read from the environment at the
moment it is typed. See docs/CREDENTIAL_POPUP_DESIGN.md (step 6).
"""
from __future__ import annotations
import os
import re

_REF = re.compile(r"^\{env:([A-Z][A-Z0-9_]*)\}$")


class MissingSecret(RuntimeError):
    """A step refers to an environment variable that is not set."""


def make_ref(name: str) -> str:
    return "{env:" + name + "}"


def ref_name(value) -> str | None:
    match = _REF.match(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def is_ref(value) -> bool:
    return ref_name(value) is not None


def resolve(value, environ=None) -> str:
    """The text to type for a step value: the environment variable's content for a reference, else the value."""
    name = ref_name(value)
    if name is None:
        return str(value)
    environ = os.environ if environ is None else environ
    if not environ.get(name):
        raise MissingSecret(f"{name} is not set - add it to .env (run `auth` to be asked for it)")
    return environ[name]


def secret(name: str) -> str:
    """What a generated test calls to type a login detail: the value of .env variable `name`, read at run time."""
    return resolve(make_ref(name))


def needs_fresh_session(flow: dict) -> bool:
    """A flow that signs in must start signed OUT: a saved session would already be past the login."""
    return any(is_ref(step.get("value")) for step in flow.get("steps") or [])
