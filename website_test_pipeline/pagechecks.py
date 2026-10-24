"""Page health: deterministic checks that need no model.

A smoke test that clicks a button only proves the click worked. These checks look at what the browser itself
reports about the page, so each one is a reproducible fact with a stated cause: a script threw, a request
came back 4xx/5xx, an image did not load, the page is blank, a control has no accessible name.

`PageHealth` is attached to a page BEFORE navigation (the browser only reports what it sees while a listener
is attached), `measure()` reads the settled DOM once, and `freeze()` stops recording before the explorer's
interaction probes navigate away and add noise. `health_findings()` turns the recorded dict into Findings; it
is a pure function over data, so it is testable without a browser.
"""
from __future__ import annotations
from urllib.parse import urlsplit

MAX_ITEMS = 15      # per list - enough to show the problem, small enough to keep the report readable

# One pass over the settled DOM. Returns only counts and short samples, never page content in bulk.
_MEASURE_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const short = el => (el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 110);
  const name = el => (el.getAttribute('aria-label') || '').trim()
    || (el.getAttribute('aria-labelledby') ? 'x' : '')
    || (el.innerText || el.textContent || '').trim()
    || (el.getAttribute('title') || '').trim()
    || (el.querySelector('img[alt]:not([alt=""])') ? 'x' : '')
    || (el.value && ['button', 'submit', 'reset'].includes(el.type) ? el.value : '');
  const imgs = [...document.images].filter(vis);
  const brokenImgs = imgs.filter(i => i.complete && i.naturalWidth === 0 && i.currentSrc && !i.currentSrc.startsWith('data:'));
  const noAlt = imgs.filter(i => !i.hasAttribute('alt') && i.getAttribute('role') !== 'presentation');
  const noNameCtl = [...document.querySelectorAll('button,[role=button],a[href]')].filter(vis).filter(e => !name(e));
  const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]),select,textarea')].filter(vis);
  const unlabeled = fields.filter(f => !(f.getAttribute('aria-label') || f.getAttribute('aria-labelledby') || f.getAttribute('title')
    || (f.id && document.querySelector('label[for="' + CSS.escape(f.id) + '"]')) || f.closest('label')));
  const body = (document.body && document.body.innerText || '').trim();
  return {
    title: document.title || '',
    h1_count: document.querySelectorAll('h1').length,
    body_text_chars: body.length,
    has_lang: !!document.documentElement.getAttribute('lang'),
    has_viewport_meta: !!document.querySelector('meta[name=viewport]'),
    broken_images: brokenImgs.slice(0, %(n)d).map(i => i.currentSrc.slice(0, 200)),
    broken_image_count: brokenImgs.length,
    images_without_alt: noAlt.slice(0, %(n)d).map(i => (i.currentSrc || '').slice(0, 200)),
    images_without_alt_count: noAlt.length,
    controls_without_name: noNameCtl.slice(0, %(n)d).map(short),
    controls_without_name_count: noNameCtl.length,
    fields_without_label: unlabeled.slice(0, %(n)d).map(short),
    fields_without_label_count: unlabeled.length,
  };
}
""" % {"n": MAX_ITEMS}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _same_site(a: str, b: str) -> bool:
    """True when two hosts are the same or one is a subdomain of the other (www. and cdn. count as the site)."""
    a, b = _host(a), _host(b)
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a)
                              or a.removeprefix("www.") == b.removeprefix("www."))


class PageHealth:
    def __init__(self, page, url: str):
        self.url = url
        self.recording = True
        self.main_status: int | None = None
        self.console_errors: list[dict] = []
        self.page_errors: list[dict] = []
        self.failed_requests: list[dict] = []
        self.bad_responses: list[dict] = []
        self.measured: dict = {}
        self._handlers = (("console", self._on_console), ("pageerror", self._on_pageerror),
                          ("requestfailed", self._on_requestfailed), ("response", self._on_response))
        for event, handler in self._handlers:
            page.on(event, handler)
        self._main_frame = page.main_frame

    def freeze(self) -> None:
        self.recording = False

    def detach(self, page) -> None:
        """Stop listening. A short-lived collector (one per click) must not leave its handlers on the page."""
        self.recording = False
        for event, handler in self._handlers:
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass

    def _add(self, bucket: list[dict], item: dict) -> None:
        if self.recording and len(bucket) < MAX_ITEMS and item not in bucket:
            bucket.append(item)

    def _on_console(self, msg) -> None:
        if msg.type != "error":
            return
        where = (msg.location or {}).get("url", "") or ""
        self._add(self.console_errors, {"text": (msg.text or "")[:200], "source": where[:200],
                                        "same_site": _same_site(where, self.url) if where else True})

    def _on_pageerror(self, err) -> None:
        name = getattr(err, "name", "") or "Error"
        self._add(self.page_errors, {"text": f"{name}: {getattr(err, 'message', err)}"[:200]})

    def _on_requestfailed(self, req) -> None:
        reason = (req.failure or "")
        # A request the page itself cancelled (navigating away, a replaced image) is not a failure.
        if "ERR_ABORTED" in str(reason):
            return
        self._add(self.failed_requests, {"url": req.url[:200], "type": req.resource_type, "reason": str(reason)[:80],
                                         "same_site": _same_site(req.url, self.url)})

    def _on_response(self, resp) -> None:
        try:
            req = resp.request
            if req.is_navigation_request() and req.frame == self._main_frame and self.main_status is None and self.recording:
                self.main_status = resp.status
            if resp.status >= 400:
                self._add(self.bad_responses, {"url": resp.url[:200], "status": resp.status, "type": req.resource_type,
                                               "same_site": _same_site(resp.url, self.url)})
        except Exception:
            return

    def measure(self, page) -> None:
        try:
            self.measured = page.evaluate(_MEASURE_JS)
        except Exception:
            self.measured = {}

    def as_dict(self) -> dict:
        return {"main_status": self.main_status, "console_errors": self.console_errors, "page_errors": self.page_errors,
                "failed_requests": self.failed_requests, "bad_responses": self.bad_responses, "measured": self.measured}


def sweep_findings(inv: dict):
    """Findings from the control sweep (sweep.py): a same-site link that returns an error, a click that throws a
    script error, a click that lands on an error page. Only what the browser reported - never a judgement that a
    control 'should' have done something."""
    from .findings import Finding
    url, out = inv.get("url", ""), []

    def add(sev: str, kind: str, summary: str, repro: str, action: str) -> None:
        out.append(Finding(test=f"sweep: {url}", scope="page", url=url, severity=sev, kind=kind, summary=summary,
                           repro=repro, next_action=action))

    broken = [i for i in inv.get("swept") or [] if i.get("outcome") == "broken"]
    for item in broken[:5]:
        add("P2", "broken_link", f'the link "{item.get("name") or item.get("href")}" returns HTTP {item.get("status")}: {item.get("href")}',
            f'open {item.get("href")}', "fix the link target, or remove the link")
    if len(broken) > 5:
        add("P2", "broken_link", f"{len(broken) - 5} more broken link(s) on this page (first 5 listed separately)", f"open {url}",
            "check every link on the page")
    for item in inv.get("swept") or []:
        if item.get("kind") == "button" and item.get("outcome") == "navigates" and (item.get("status") or 0) >= 400:
            add("P1", "click_error_page", f'clicking "{item.get("name")}" lands on an error page (HTTP {item["status"]}): {item.get("to")}',
                f'open {url} and click "{item.get("name")}"', "fix the destination; a visitor who clicks this sees an error")
        if item.get("errors"):
            add("P2", "click_script_error", f'using "{item.get("name")}" threw a script error: "{item["errors"][0]}"',
                f'open {url}, use "{item.get("name")}" with the console open', "fix the script error")
    return out


def health_findings(inv: dict):
    """Findings for one explored page's recorded health. P1 = the page itself is broken; P2 = something on it
    is broken; P3 = a quality problem a person should fix when convenient. Third-party noise (an ad or analytics
    request blocked in a headless browser) is never above P3, and only the site's own failures are P1/P2."""
    from .findings import Finding
    health = inv.get("health") or {}
    if not health:
        return []
    url, out = inv.get("url", ""), []
    m = health.get("measured") or {}

    def add(sev: str, kind: str, summary: str, action: str) -> None:
        out.append(Finding(test=f"page health: {url}", scope="page", url=url, severity=sev, kind=kind, summary=summary,
                           repro=f"open {url} and check the browser developer console and network tab", next_action=action))

    status = health.get("main_status")
    if status and status >= 400:
        add("P1", "page_http_error", f"the page itself answered HTTP {status}", "check the route, redirect or server; a user sees an error page")
    if m and m.get("body_text_chars", 1) < 20 and not (inv.get("controls") or inv.get("headings")):
        add("P1", "blank_page", "the page rendered with almost no visible text and no controls",
            "open the page: a script may have failed before drawing it")
    for e in health.get("page_errors", [])[:3]:
        add("P2", "script_error", f'an uncaught script error was thrown: "{e["text"]}"', "fix the script error; it can break features on this page")
    own_bad = [r for r in health.get("bad_responses", []) if r.get("same_site")]
    for r in own_bad[:3]:
        sev = "P2" if r["status"] >= 500 or r.get("type") in {"script", "stylesheet", "xhr", "fetch", "document"} else "P3"
        add(sev, "failed_request", f'{r.get("type", "request")} {r["url"]} returned HTTP {r["status"]}', "fix or remove the reference")
    for r in [r for r in health.get("failed_requests", []) if r.get("same_site")][:3]:
        add("P2", "failed_request", f'{r.get("type", "request")} {r["url"]} could not load ({r.get("reason")})', "check the file exists and the server allows it")
    if m.get("broken_image_count"):
        add("P2", "broken_image", f'{m["broken_image_count"]} visible image(s) did not load, e.g. {m["broken_images"][0]}', "fix the image URL or remove the element")
    own_console = [e for e in health.get("console_errors", []) if e.get("same_site")]
    if own_console:
        add("P3", "console_error", f'{len(own_console)} console error(s) from the site, e.g. "{own_console[0]["text"]}"', "open the console and fix the cause")
    if m:
        if not (m.get("title") or "").strip():
            add("P3", "missing_title", "the page has no <title>", "add a descriptive title; it appears in tabs, history and search results")
        if m.get("h1_count") == 0 and (inv.get("headings") or []):
            add("P3", "missing_h1", "the page has headings but no top-level <h1>", "add one <h1> that names the page")
        if m.get("controls_without_name_count"):
            add("P3", "unnamed_control", f'{m["controls_without_name_count"]} button(s)/link(s) have no accessible name, e.g. {m["controls_without_name"][0]}',
                "add visible text or an aria-label so screen-reader users know what it does")
        if m.get("fields_without_label_count"):
            add("P3", "unlabeled_field", f'{m["fields_without_label_count"]} form field(s) have no label, e.g. {m["fields_without_label"][0]}',
                "associate a <label> or add aria-label")
        if m.get("images_without_alt_count"):
            add("P3", "image_without_alt", f'{m["images_without_alt_count"]} image(s) have no alt attribute, e.g. {m["images_without_alt"][0]}',
                "add alt text (or alt=\"\" for decorative images)")
        if not m.get("has_lang"):
            add("P3", "missing_lang", "<html> has no lang attribute", "set lang so screen readers pick the right pronunciation")
    return out
