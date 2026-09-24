"""A popup that asks a person for login / sign-up details while the agent waits.

It mirrors what the agent sees (a screenshot of the page, refreshed every second), tells the person what to
type into each field in plain words, and hands the values back to the caller - nothing else. It never logs,
stores or prints a value: writing them anywhere is the caller's decision (see docs/CREDENTIAL_POPUP_DESIGN.md).
The mirror only ever shows the PAGE; values are typed into this window, not the page, so a screenshot cannot
contain them.
"""
from __future__ import annotations
import io
import sys
from dataclasses import dataclass, field

MIRROR_SIZE = (640, 400)


@dataclass
class AuthRequest:
    url: str
    kind: str                                          # "login" | "signup"
    fields: list[dict] = field(default_factory=list)   # PageInventory.auth[i]["fields"]
    notice: str = ""                                   # shown in red, e.g. the site's error after a failed attempt


@dataclass
class AuthAnswer:
    values: dict[str, str]                             # field key -> typed text
    remember: bool = False                             # the person ticked "remember these in .env"


def field_key(index: int, fld: dict) -> str:
    return fld.get("name") or f"field{index}"


def field_guidance(fld: dict, kind: str) -> str:
    """One plain-language line for a field, built from the page's own label so it works in any language."""
    label = (fld.get("label") or fld.get("name") or "").strip()
    ftype = (fld.get("type") or "text").lower()
    auto = (fld.get("autocomplete") or "").lower()
    if ftype == "password":
        text = ("Choose a password for the new account." if kind == "signup" or auto == "new-password"
                else "Your password for this site.")
    elif ftype == "email" or auto in {"email", "username"}:
        text = ("An email address for the new account." if kind == "signup"
                else "The email or username you sign in with.")
    elif ftype == "tel":
        text = "A phone number."
    elif ftype == "number":
        text = "A number."
    else:
        text = "Type what the site asks for here."
    if label:
        text += f' The page calls it "{label}".'
    if fld.get("required"):
        text += " Required."
    return text


def should_ask(auth_mode: str, kind: str, has_session: bool, interactive: bool) -> tuple[bool, str]:
    """(ask?, reason). Password walls only; never when told not to, never twice, never unattended."""
    if auth_mode == "none":
        return False, "AUTH_MODE=none"
    if kind not in {"login", "signup"}:
        return False, f"not a login or sign-up form ({kind})"
    if has_session:
        return False, "already signed in for this site"
    if not interactive:
        return False, "not an interactive run (no terminal or display)"
    return True, "login wall needs credentials"


def is_interactive() -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


class CredentialPopup:
    """The window. Built on a Tk root the caller owns, so tests can drive it without a mainloop."""

    def __init__(self, root, request: AuthRequest, get_screenshot=None, timeout_s: int = 180, refresh_ms: int = 1000):
        import tkinter as tk
        self.root, self.request = root, request
        self.get_screenshot, self.refresh_ms = get_screenshot, refresh_ms
        self.remaining = timeout_s
        self.result: AuthAnswer | None = None
        self.entries: dict[str, object] = {}
        self._photo = None
        root.title("Sign-in needed")
        root.protocol("WM_DELETE_WINDOW", self.skip)
        what = "sign in" if request.kind == "login" else "create an account"
        tk.Label(root, text=f"The agent reached a page where it must {what}.", font=("Segoe UI", 11, "bold"),
                 wraplength=MIRROR_SIZE[0]).pack(padx=12, pady=(12, 2), anchor="w")
        tk.Label(root, text=request.url, fg="#555", wraplength=MIRROR_SIZE[0]).pack(padx=12, anchor="w")
        if request.notice:
            tk.Label(root, text=request.notice, fg="#b3261a", wraplength=MIRROR_SIZE[0], justify="left"
                     ).pack(padx=12, pady=(6, 0), anchor="w")
        self.mirror = tk.Label(root, text="(no preview)", bg="#eee", width=80, height=12)
        self.mirror.pack(padx=12, pady=8)
        form = tk.Frame(root)
        form.pack(padx=12, fill="x")
        for i, fld in enumerate(request.fields):
            key = field_key(i, fld)
            tk.Label(form, text=fld.get("label") or fld.get("name") or fld.get("type") or "field",
                     font=("Segoe UI", 9, "bold")).grid(row=2 * i, column=0, sticky="w")
            entry = tk.Entry(form, width=48, show="*" if (fld.get("type") or "").lower() == "password" else "")
            entry.grid(row=2 * i, column=1, padx=6, pady=(4, 0))
            tk.Label(form, text=field_guidance(fld, request.kind), fg="#555", wraplength=520, justify="left"
                     ).grid(row=2 * i + 1, column=0, columnspan=2, sticky="w")
            self.entries[key] = entry
        self.remember = tk.BooleanVar(master=root, value=False)
        tk.Checkbutton(root, text="Remember these in .env (stays on this computer, never committed)",
                       variable=self.remember).pack(padx=12, pady=(8, 0), anchor="w")
        self.countdown = tk.Label(root, text="", fg="#b3261a")
        self.countdown.pack(padx=12, anchor="w")
        buttons = tk.Frame(root)
        buttons.pack(padx=12, pady=10, anchor="e")
        tk.Button(buttons, text="Skip", command=self.skip).pack(side="right", padx=(6, 0))
        tk.Button(buttons, text="Continue", command=self.submit).pack(side="right")
        self._tick()
        self.refresh_mirror()

    def submit(self) -> None:
        self.result = AuthAnswer({k: e.get() for k, e in self.entries.items()}, bool(self.remember.get()))
        self.root.quit()

    def skip(self) -> None:
        self.result = None
        self.root.quit()

    def _tick(self) -> None:
        self.countdown.config(text=f"Skipping automatically in {self.remaining}s")
        if self.remaining <= 0:
            self.skip()
            return
        self.remaining -= 1
        self.root.after(1000, self._tick)

    def refresh_mirror(self) -> None:
        if not self.get_screenshot:
            return
        try:
            from PIL import Image, ImageTk
            image = Image.open(io.BytesIO(self.get_screenshot()))
            image.thumbnail(MIRROR_SIZE)
            self._photo = ImageTk.PhotoImage(image, master=self.root)
            self.mirror.config(image=self._photo, text="", width=image.width, height=image.height)
        except Exception:
            pass                       # a failed frame just keeps the last one
        self.root.after(self.refresh_ms, self.refresh_mirror)


def ask_credentials(request: AuthRequest, get_screenshot=None, timeout_s: int = 180) -> AuthAnswer | None:
    """Show the popup and block until the person continues, skips, or the time runs out. None = no credentials."""
    import tkinter as tk
    root = tk.Tk()
    root.attributes("-topmost", True)
    popup = CredentialPopup(root, request, get_screenshot, timeout_s)
    try:
        root.mainloop()
    finally:
        root.destroy()
    return popup.result


if __name__ == "__main__":     # a look at the popup on a local sample page - no site, no submit, no values printed
    from playwright.sync_api import sync_playwright
    SAMPLE = ('<main style="font-family:sans-serif;padding:40px"><h1>Sign in</h1><form>'
              '<label>Email <input type="email" required></label><br><br>'
              '<label>Password <input type="password" autocomplete="current-password" required></label>'
              '<br><br><button>Sign in</button></form></main>')
    FIELDS = [{"type": "email", "name": "email", "label": "Email", "required": True},
              {"type": "password", "name": "pw", "label": "Password", "autocomplete": "current-password", "required": True}]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 560})
        page.set_content(SAMPLE)
        answer = ask_credentials(AuthRequest("http://local-demo/sign-in", "login", FIELDS), lambda: page.screenshot(), 60)
        browser.close()
    print("skipped" if answer is None else {k: f"{len(v)} characters" for k, v in answer.values.items()},
          "| remember:", getattr(answer, "remember", None))
