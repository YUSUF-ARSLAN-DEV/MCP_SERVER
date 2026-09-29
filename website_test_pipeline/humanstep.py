"""A step only a person can do - today an image CAPTCHA: a window shows the code and asks for it.

A flow that submits a form with a CAPTCHA cannot finish on its own, so it used to stop as "needs a person". With
`--ask-human` (env WTP_HUMAN=ask) and a real terminal + display, the run now pauses at the submit, opens a small
always-on-top window with the CAPTCHA image, waits for the person to type the code, fills it in, and carries on.
Without that (unattended, CI, or the flag off) nothing pops up: the step is skipped and reported as needing a person.

Nothing here is specific to one site. The CAPTCHA input is found from the page's own signals (a text field whose
name / id / label / hint says captcha, or that sits beside an image that does); the code is a one-time value, so it
is never logged, stored or put in a report.
"""
from __future__ import annotations
import io
import os
import re
from dataclasses import dataclass, field

CAPTCHA_RE = re.compile(r"captcha|recaptcha|hcaptcha|security code|verification code|code in the image|characters shown|"
                        r"are you human|not a robot", re.I)
IMAGE_SIZE = (420, 140)          # the CAPTCHA is shown large: it has to be readable

# One DOM pass: the visible text field that looks like a CAPTCHA answer, its image and its reload control.
_FIND_JS = r"""
(source) => {
  const re = new RegExp(source, 'i');
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                       return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = e => { const l = e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
                         return (l ? l.innerText : (e.getAttribute('aria-label') || '')).trim(); };
  const selectorOf = e => e.id ? '#' + CSS.escape(e.id)
        : (e.getAttribute('name') ? e.tagName.toLowerCase() + '[name="' + e.getAttribute('name') + '"]' : null);
  const inputs = [...document.querySelectorAll('input[type=text], input:not([type]), input[type=search], input[type=tel]')]
      .filter(i => shown(i) && !i.disabled && !i.readOnly);
  const describe = input => {
    const root = input.closest('form') || document.body;
    const img = [...root.querySelectorAll('img')].find(i => shown(i) && re.test([i.alt, i.src, i.title, i.className].join(' ')));
    const selector = selectorOf(input);
    if (!selector) return null;
    const reload = [...root.querySelectorAll('button, a, [role=button]')].find(b =>
        shown(b) && /reload|refresh|different|new code|another/i.test([b.getAttribute('aria-label'), b.title, b.className, b.innerText].join(' ')));
    const imgSel = img ? (img.id ? '#' + CSS.escape(img.id) : 'img[src="' + img.getAttribute('src') + '"]') : null;
    const reloadSel = reload ? (reload.id ? '#' + CSS.escape(reload.id) :
        (reload.getAttribute('aria-label') ? reload.tagName.toLowerCase() + '[aria-label="' + reload.getAttribute('aria-label') + '"]' : null)) : null;
    return {input: selector, image: imgSel, reload: reloadSel, label: labelOf(input) || input.placeholder || input.name || 'the code'};
  };
  // 1. a field that says it is the CAPTCHA answer (name / id / class / hint / label)
  for (const input of inputs) {
    if (re.test([input.id, input.name, input.className, input.placeholder, input.title, labelOf(input)].join(' '))) {
      const found = describe(input); if (found) return found;
    }
  }
  // 2. otherwise the field nearest after a CAPTCHA image in the same form
  for (const img of document.querySelectorAll('img')) {
    if (!shown(img) || !re.test([img.alt, img.src, img.title, img.className].join(' '))) continue;
    const root = img.closest('form') || document.body;
    const after = inputs.find(i => root.contains(i) && (img.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_FOLLOWING));
    const near = after || [...inputs].reverse().find(i => root.contains(i));
    if (near) { const found = describe(near); if (found) return found; }
  }
  return null;
}
"""


@dataclass
class HumanRequest:
    url: str
    label: str                       # what the page calls the CAPTCHA field ("What code is in the image?")
    notice: str = ""                 # shown in red, e.g. "that code was wrong"
    fields: list[dict] = field(default_factory=list)      # the form's other fields (see find_form_fields)


@dataclass
class HumanAnswer:
    code: str                        # the CAPTCHA code (never logged or stored)
    values: dict[str, str] = field(default_factory=dict)  # form field selector -> what to put in it


# Every visible field of the form that holds the CAPTCHA, in page order, with the page's own label, its options (a
# <select>) and what it holds now - so the window can show the whole form, not just the code.
_FIELDS_JS = r"""
(captchaSelector) => {
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                       return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = e => { const l = e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
                         return ((l ? l.innerText : '') || e.getAttribute('aria-label') || e.placeholder || e.name || '').trim(); };
  const selectorOf = e => e.id ? '#' + CSS.escape(e.id)
        : (e.getAttribute('name') ? e.tagName.toLowerCase() + '[name="' + e.getAttribute('name') + '"]' : null);
  const captcha = document.querySelector(captchaSelector);
  const form = captcha && (captcha.closest('form') || document.body);
  if (!form) return [];
  const out = [];
  for (const e of form.querySelectorAll('input, select, textarea')) {
    const type = (e.type || e.tagName).toLowerCase();
    if (e === captcha || ['hidden', 'submit', 'button', 'image', 'reset', 'file', 'password'].includes(type)) continue;
    if (!shown(e) || e.disabled || e.readOnly) continue;
    const selector = selectorOf(e); if (!selector) continue;
    const field = {selector, label: labelOf(e), type, required: e.required || e.getAttribute('aria-required') === 'true'};
    if (e.tagName === 'SELECT') {
      field.type = 'select';
      field.options = [...e.options].filter(o => !o.disabled).map(o => o.text.trim());
      field.current = e.selectedIndex >= 0 ? e.options[e.selectedIndex].text.trim() : '';
      field.placeholder = e.options.length && e.options[0].value === '' ? e.options[0].text.trim() : '';
    } else if (type === 'checkbox' || type === 'radio') {
      field.current = e.checked ? 'yes' : '';
    } else {
      field.current = e.value || '';
    }
    out.push(field);
  }
  return out;
}
"""


def find_form_fields(page, info: dict) -> list[dict]:
    """The other visible fields of the CAPTCHA's form: [{selector, label, type, required, options?, current}]."""
    try:
        return page.evaluate(_FIELDS_JS, info["input"]) or []
    except Exception:
        return []


def default_value(fld: dict) -> str:
    """What to show in a field that is still empty: something plausible for its kind, so the whole form is populated."""
    if fld.get("type") == "select":
        current, placeholder = fld.get("current") or "", fld.get("placeholder") or ""
        real = [o for o in fld.get("options") or [] if o and o != placeholder and not o.strip().startswith("-")]
        return current if current and current != placeholder and not current.strip().startswith("-") else (real[0] if real else current)
    if fld.get("type") in {"checkbox", "radio"}:
        return fld.get("current") or ""
    if fld.get("current"):
        return fld["current"]
    kind, label = fld.get("type") or "text", (fld.get("label") or "").lower()
    if kind == "email" or "email" in label or "e-mail" in label:
        return "qa-test@example.com"
    if kind == "tel" or "mobile" in label or "phone" in label:
        return "0500000000"
    if kind == "number":
        return "1"
    return "Test"


def find_captcha(page) -> dict | None:
    """{"input", "image", "reload", "label"} for the CAPTCHA answer field on the page, or None. Never raises."""
    try:
        return page.evaluate(_FIND_JS, CAPTCHA_RE.pattern)
    except Exception:
        return None


def field_is_empty(page, info: dict) -> bool:
    try:
        return not page.locator(info["input"]).first.input_value(timeout=1500)
    except Exception:
        return False


def submits_this_form(page, control, info: dict) -> bool:
    """Is `control` a submit button of the form that holds the CAPTCHA field? (Clicking anything else - a menu, the
    'show me a different code' button - must not ask for a code.)"""
    try:
        return bool(control.evaluate(
            "(e, sel) => { const f = e.form || e.closest('form'); const i = document.querySelector(sel);"
            " const submit = e.type === 'submit' || (e.tagName === 'BUTTON' && (!e.type || e.type === 'submit'));"
            " return !!(f && i && f.contains(i) && submit); }", info["input"]))
    except Exception:
        return False


# ------------------------------------------------------------------ when to ask

def human_mode() -> str:
    """"ask" only when the person opted in with --ask-human; everything else is "skip"."""
    return "ask" if os.environ.get("WTP_HUMAN", "").strip().lower() == "ask" else "skip"


def can_ask() -> tuple[bool, str]:
    """(ask?, reason). Needs the opt-in, a terminal and a display; never unattended."""
    if human_mode() != "ask":
        return False, "not asked for (run with --ask-human to be prompted)"
    from .authpopup import is_interactive
    if not is_interactive():
        return False, "no terminal or display to show a window on"
    return True, "a person can answer"


# ------------------------------------------------------------------ the window

class CodePopup:
    """The window. Built on a Tk root the caller owns, so tests can drive it without a mainloop. It shows every field
    of the form (prefilled, editable), then the CAPTCHA image with its code box; Continue hands back all of it."""

    def __init__(self, root, request: HumanRequest, get_image=None, reload=None, timeout_s: int = 180, refresh_ms: int = 1500):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.request, self.get_image, self.reload, self.refresh_ms = root, request, get_image, reload, refresh_ms
        self.remaining = timeout_s
        self.result: HumanAnswer | None = None
        self._photo = None
        self.widgets: dict[str, tuple[str, object]] = {}          # selector -> (kind, widget or variable)
        root.title("A person is needed")
        root.protocol("WM_DELETE_WINDOW", self.skip)
        wrap = IMAGE_SIZE[0] + 60
        tk.Label(root, text="This form needs a person. Check the details, then type the code from the image.",
                 font=("Segoe UI", 11, "bold"), wraplength=wrap, justify="left").pack(padx=12, pady=(12, 2), anchor="w")
        tk.Label(root, text=request.url, fg="#555", wraplength=wrap).pack(padx=12, anchor="w")
        if request.notice:
            tk.Label(root, text=request.notice, fg="#b3261a", wraplength=wrap, justify="left").pack(padx=12, pady=(6, 0), anchor="w")
        form = tk.Frame(root)
        form.pack(padx=12, pady=(8, 0), fill="x")
        for row, fld in enumerate(request.fields):
            label = (fld.get("label") or fld.get("selector") or "field") + (" *" if fld.get("required") and "*" not in (fld.get("label") or "") else "")
            tk.Label(form, text=label, font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w", pady=2)
            value = default_value(fld)
            if fld.get("type") == "select":
                var = tk.StringVar(master=root, value=value)
                ttk.Combobox(form, textvariable=var, values=fld.get("options") or [], state="readonly", width=34).grid(row=row, column=1, padx=6, pady=2, sticky="w")
                self.widgets[fld["selector"]] = ("select", var)
            elif fld.get("type") in {"checkbox", "radio"}:
                var = tk.BooleanVar(master=root, value=bool(value))
                tk.Checkbutton(form, variable=var).grid(row=row, column=1, padx=6, pady=2, sticky="w")
                self.widgets[fld["selector"]] = ("check", var)
            else:
                entry = tk.Entry(form, width=37)
                entry.insert(0, value)
                entry.grid(row=row, column=1, padx=6, pady=2, sticky="w")
                self.widgets[fld["selector"]] = ("text", entry)
        self.image_label = tk.Label(root, text="(the image is loading)", bg="#eee", width=52, height=7)
        self.image_label.pack(padx=12, pady=8)
        if reload:
            tk.Button(root, text="Show me a different code", command=self.new_code).pack(padx=12, anchor="w")
        tk.Label(root, text=f'Type the code from the image (the page calls it "{request.label}").', fg="#555",
                 wraplength=wrap, justify="left").pack(padx=12, pady=(8, 0), anchor="w")
        self.entry = tk.Entry(root, width=30, font=("Consolas", 14))
        self.entry.pack(padx=12, pady=4, anchor="w")
        self.entry.bind("<Return>", lambda _event: self.submit())
        self.countdown = tk.Label(root, text="", fg="#b3261a")
        self.countdown.pack(padx=12, anchor="w")
        buttons = tk.Frame(root)
        buttons.pack(padx=12, pady=10, anchor="e")
        tk.Button(buttons, text="Skip", command=self.skip).pack(side="right", padx=(6, 0))
        tk.Button(buttons, text="Continue", command=self.submit).pack(side="right")
        self.entry.focus_set()
        self._tick()
        self.refresh_image()

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for selector, (kind, widget) in self.widgets.items():
            got = widget.get()
            out[selector] = ("yes" if got else "") if kind == "check" else str(got)
        return out

    def submit(self) -> None:
        code = self.entry.get().strip()
        self.result = HumanAnswer(code, self.values()) if code else None
        self.root.quit()

    def skip(self) -> None:
        self.result = None
        self.root.quit()

    def new_code(self) -> None:
        try:
            self.reload()
        except Exception:
            pass
        self.entry.delete(0, "end")
        self.root.after(600, self._show_image)

    def _tick(self) -> None:
        self.countdown.config(text=f"Skipping automatically in {self.remaining}s")
        if self.remaining <= 0:
            self.skip()
            return
        self.remaining -= 1
        self.root.after(1000, self._tick)

    def _show_image(self) -> None:
        if not self.get_image:
            return
        try:
            from PIL import Image, ImageTk
            image = Image.open(io.BytesIO(self.get_image()))
            if image.width < IMAGE_SIZE[0]:                     # a small CAPTCHA is enlarged so it can be read
                scale = min(IMAGE_SIZE[0] // image.width, 3)
                image = image.resize((image.width * scale, image.height * scale))
            else:
                image.thumbnail(IMAGE_SIZE)
            self._photo = ImageTk.PhotoImage(image, master=self.root)
            self.image_label.config(image=self._photo, text="", width=image.width, height=image.height)
        except Exception:
            pass                        # a failed frame keeps the last one

    def refresh_image(self) -> None:
        self._show_image()
        self.root.after(self.refresh_ms, self.refresh_image)


def ask_code(request: HumanRequest, get_image=None, reload=None, timeout_s: int = 180) -> HumanAnswer | None:
    """Show the window and block until the person continues, skips, or the time runs out. None = no code."""
    import tkinter as tk
    root = tk.Tk()
    root.attributes("-topmost", True)
    popup = CodePopup(root, request, get_image, reload, timeout_s)
    try:
        root.mainloop()
    finally:
        root.destroy()
    return popup.result


# ------------------------------------------------------------------ the step

class HumanNeeded(RuntimeError):
    """The flow reached a step only a person can do, and nobody was there to do it."""


def solve_before_submit(page, control, log=None, asker=None) -> str:
    """Call just before a step that may submit a form. Returns:
      "none"     - no CAPTCHA here, it is already filled, or `control` does not submit its form: nothing to do
      "filled"   - a person typed the code and it has been filled in
      "skipped"  - asked for, but the person skipped or ran out of time
      "not-asked"- a CAPTCHA needs answering and this run cannot ask (see can_ask())
    `asker(request, get_image, reload)` is the window; a parameter so tests need no display."""
    info = find_captcha(page)
    if not info or not field_is_empty(page, info) or not submits_this_form(page, control, info):
        return "none"
    ok, reason = can_ask()
    if not ok:
        if log:
            log.info("human step: the form needs a CAPTCHA answered - %s", reason)
        return "not-asked"
    image = page.locator(info["image"]).first if info.get("image") else None
    reload_button = page.locator(info["reload"]).first if info.get("reload") else None
    fields = find_form_fields(page, info)
    request = HumanRequest(page.url, info.get("label") or "the code", fields=fields)
    answer = (asker or ask_code)(request,
                                 (lambda: image.screenshot(timeout=3000)) if image else None,
                                 (lambda: reload_button.click(timeout=2000)) if reload_button else None)
    if not answer or not getattr(answer, "code", ""):
        return "skipped"
    _fill_form(page, fields, answer.values)
    page.locator(info["input"]).first.fill(answer.code, timeout=3000)       # the code is never logged or stored
    return "filled"


def _fill_form(page, fields: list[dict], values: dict[str, str]) -> None:
    """Put what the person confirmed into every field of the form (a field that will not take it is left as it was)."""
    for fld in fields:
        wanted = values.get(fld["selector"])
        if wanted is None:
            continue
        control = page.locator(fld["selector"]).first
        try:
            if fld.get("type") == "select":
                if wanted and wanted != (fld.get("placeholder") or ""):
                    control.select_option(label=wanted, timeout=3000)
            elif fld.get("type") in {"checkbox", "radio"}:
                control.set_checked(bool(wanted), timeout=3000)
            elif wanted != (fld.get("current") or ""):
                control.fill(wanted, timeout=3000)
        except Exception:
            continue


def human_step(page, control, log=None) -> None:
    """For a generated test: answer the CAPTCHA before submitting, or skip the test as needing a person."""
    outcome = solve_before_submit(page, control, log)
    if outcome in {"not-asked", "skipped"}:
        import pytest
        pytest.skip("needs a person: the form has a CAPTCHA (run with --ask-human to be asked for the code)")
