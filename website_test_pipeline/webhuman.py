"""Ask a person through a web page instead of a desktop window (the hosted version has no display).

The run and the API talk through plain files in one folder, WTP_HUMAN_DIR (runs/<site>/human/):

    request-<n>.json   the run writes it: what it needs (a CAPTCHA code, or sign-in details) and which fields
    request-<n>.png    the CAPTCHA image / a screenshot of the page; the run refreshes it while it waits
    answer-<n>.json    the API writes it when the person answers: {"action": "submit"|"skip", "code", "values"}
    refresh-<n>.flag   the API writes it when the person asks for a different CAPTCHA image
    closed-<n>.json    the run writes it when it is finished with the request (answered, skipped, timed out)

Files, not a socket: the run is a separate process (and so are the generated tests), they can all find the folder from
one environment variable, and a request that nobody answers simply times out exactly like the desktop window did.
Switched on by HUMAN_CHANNEL=web. What a person types is read once and the answer file is deleted straight away; it is
never logged, and nothing is written to .env (the signed-in session saved under auth/ is what carries on).
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .authpopup import AuthAnswer, AuthRequest, field_guidance, field_key
from .humanstep import HumanAnswer, HumanRequest, default_value

DEFAULT_TIMEOUT_S = 300          # a person on a web page needs longer than one at a desk
SNAPSHOT_EVERY_S = 1.5


def human_dir() -> Path | None:
    value = os.environ.get("WTP_HUMAN_DIR", "").strip()
    return Path(value) if value else None


def active() -> bool:
    """Is a person reachable through the web page? HUMAN_CHANNEL=web and a folder to talk through."""
    return os.environ.get("HUMAN_CHANNEL", "").strip().lower() == "web" and human_dir() is not None


def disabled() -> bool:
    """HUMAN_CHANNEL=off: nobody may be asked, not even through a desktop window (a hosted run that was told not to)."""
    return os.environ.get("HUMAN_CHANNEL", "").strip().lower() == "off"


def timeout_s() -> int:
    try:
        return max(10, int(os.environ.get("WTP_HUMAN_TIMEOUT_S", "") or DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _write(path: Path, data: bytes, private: bool = False) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)                          # the other side never reads a half-written file


def _next_seq(folder: Path) -> int:
    seqs = [int(p.stem.split("-", 1)[1]) for p in folder.glob("request-*.json") if p.stem.split("-", 1)[1].isdigit()]
    return max(seqs, default=0) + 1


def _wait(folder: Path, seq: int, limit_s: int, snapshot=None, reload=None, poll_s: float = 0.5) -> tuple[str, dict]:
    """Block until the person answers. ("answer", data) | ("timeout", {}). Keeps the picture fresh and honours refresh."""
    answer_path, flag = folder / f"answer-{seq}.json", folder / f"refresh-{seq}.flag"
    deadline, next_snapshot = time.monotonic() + limit_s, 0.0
    while time.monotonic() < deadline:
        if answer_path.is_file():
            try:
                data = json.loads(answer_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = None                      # still being written: look again
            if isinstance(data, dict):
                answer_path.unlink(missing_ok=True)      # read once, then gone
                return "answer", data
        if flag.is_file():
            flag.unlink(missing_ok=True)
            if reload:
                try:
                    reload()
                except Exception:
                    pass
                time.sleep(0.6)
            next_snapshot = 0.0
        if snapshot and time.monotonic() >= next_snapshot:
            snapshot()
            next_snapshot = time.monotonic() + SNAPSHOT_EVERY_S
        time.sleep(poll_s)
    return "timeout", {}


def _open(kind: str, payload: dict, image_source, limit_s: int) -> tuple[Path, int, object]:
    folder = human_dir()
    folder.mkdir(parents=True, exist_ok=True)
    seq = _next_seq(folder)

    def snapshot() -> None:
        if image_source is None:
            return
        try:
            _write(folder / f"request-{seq}.png", image_source())
        except Exception:
            pass                                 # a failed frame keeps the last one

    snapshot()                                   # the picture first, so the page never shows a request without it
    body = {"seq": seq, "kind": kind, "timeout_s": limit_s, "has_image": image_source is not None,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **payload}
    _write(folder / f"request-{seq}.json", json.dumps(body, ensure_ascii=False).encode("utf-8"))
    return folder, seq, snapshot


def _close(folder: Path, seq: int, outcome: str) -> None:
    (folder / f"answer-{seq}.json").unlink(missing_ok=True)
    (folder / f"refresh-{seq}.flag").unlink(missing_ok=True)
    _write(folder / f"closed-{seq}.json", json.dumps({"outcome": outcome}).encode("utf-8"))


def _text_map(raw) -> dict[str, str]:
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def ask_code_web(request: HumanRequest, get_image=None, reload=None, timeout: int | None = None) -> HumanAnswer | None:
    """The CAPTCHA step: the form (prefilled), the image, a box for the code. None = skipped or timed out."""
    limit = timeout or timeout_s()
    fields = [{**f, "default": default_value(f)} for f in request.fields]
    folder, seq, snapshot = _open("captcha", {"url": request.url, "label": request.label, "notice": request.notice,
                                              "fields": fields, "can_reload": reload is not None}, get_image, limit)
    outcome, data = _wait(folder, seq, limit, snapshot if get_image else None, reload)
    code = str(data.get("code") or "").strip() if outcome == "answer" and data.get("action") == "submit" else ""
    _close(folder, seq, "submitted" if code else ("timeout" if outcome == "timeout" else "skipped"))
    return HumanAnswer(code, _text_map(data.get("values"))) if code else None


def ask_credentials_web(request: AuthRequest, get_screenshot=None, timeout: int | None = None) -> AuthAnswer | None:
    """The sign-in step: each field with plain guidance, a live picture of the page. None = skipped or timed out."""
    limit = timeout or timeout_s()
    fields = [{"key": field_key(i, f), "label": f.get("label") or f.get("name") or f.get("type") or "field",
               "type": (f.get("type") or "text").lower(), "required": bool(f.get("required")),
               "guidance": field_guidance(f, request.kind)} for i, f in enumerate(request.fields)]
    folder, seq, snapshot = _open("credentials", {"url": request.url, "wall_kind": request.kind, "notice": request.notice,
                                                  "fields": fields}, get_screenshot, limit)
    outcome, data = _wait(folder, seq, limit, snapshot if get_screenshot else None)
    submitted = outcome == "answer" and data.get("action") == "submit"
    _close(folder, seq, "submitted" if submitted else ("timeout" if outcome == "timeout" else "skipped"))
    return AuthAnswer(_text_map(data.get("values")), remember=False) if submitted else None
