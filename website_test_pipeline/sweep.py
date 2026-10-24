"""The control sweep: touch every safe control on a page, no model involved.

A journey only exercises the controls a model happened to write a test for. The sweep goes through what the
explorer found and does the one safe thing each control invites, recording what the browser reported:

  link (same site)   requested, not followed: a 4xx/5xx is a broken link. External links are left alone.
  button             clicked on a freshly reloaded page; recorded as navigates / reveals / no_visible_change,
                     plus any script error or error page the click caused. Anything that reads as destructive,
                     a purchase or an auth step is skipped (explorer._is_probe_trigger).
  select / field     the first real option is chosen / a plausible value is typed; NEVER submitted.

An entry is "touched" when the control was actually exercised; a control the sweep could not reach is recorded
as not_reached and does not count. Each outcome is a fact the browser reported, so a result needs no judgement
about what the site "should" do. The caller bounds the work with `max_controls`.
"""
from __future__ import annotations
from urllib.parse import urldefrag, urljoin, urlsplit

from .coverage import actionable_controls
from .explorer import (_CONTROLS_JS, _CONTROL_SEL, _control_key, _flow_locator, _is_probe_trigger, _locator_for,
                       _plausible_value, _select_first_real)
from . import netguard
from .pagechecks import PageHealth, _same_site, redact
from .pageutils import dismiss_overlays, settle_page

MAX_LINKS = 60
_RESTRICTED = {401, 403, 405, 429, 501}   # the server refused a robot or a HEAD request: not a broken link
_SKIP_LINK_PREFIX = ("mailto:", "tel:", "javascript:", "#", "sms:", "data:", "blob:")
NOT_TOUCHED = {"not_reached", "unreachable"}


def touched(item: dict) -> bool:
    return item.get("outcome") not in NOT_TOUCHED


def _entry(control: dict, kind: str, outcome: str, **extra) -> dict:
    return {"name": (control.get("name") or "")[:60], "selector": control.get("selector"), "kind": kind,
            "outcome": outcome, **{k: (redact(v) if isinstance(v, str) else v) for k, v in extra.items()}}


def _errors(health: PageHealth) -> list[str]:
    return [e["text"] for e in health.page_errors][:3]


def sweep_links(page, url: str, controls: list[dict]) -> list[dict]:
    """Request each distinct same-site link once. Sequential and bounded: the sweep must not hammer a site."""
    out, seen = [], set()
    for control in controls:
        href = (control.get("href") or "").strip()
        if control.get("tag") != "a" or not href or href.lower().startswith(_SKIP_LINK_PREFIX):
            continue
        target = urldefrag(urljoin(url, href))[0]
        if urlsplit(target).scheme not in {"http", "https"} or not _same_site(target, url) or target in seen:
            continue
        seen.add(target)
        if len(seen) > MAX_LINKS:
            break
        if netguard.enabled() and not netguard.url_allowed(target):
            continue                  # a request of ours (not the browser's) must not reach a private address either
        try:
            status = page.request.head(target, timeout=8000, max_redirects=5).status
            if status >= 500:           # a server error once is a hiccup: ask again before calling the link broken
                page.wait_for_timeout(800)
                status = page.request.head(target, timeout=8000, max_redirects=5).status
        except Exception:
            out.append(_entry(control, "link", "unreachable", href=target))
            continue
        if status >= 400 and status not in _RESTRICTED:
            out.append(_entry(control, "link", "broken", href=target, status=status))
        else:
            out.append(_entry(control, "link", "ok", href=target, status=status))
    return out


def _reload(page, url: str) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded")
        settle_page(page)
        dismiss_overlays(page)
        return True
    except Exception:
        return False


def sweep_buttons(page, url: str, controls: list[dict], limit: int, log=None) -> list[dict]:
    out = []
    candidates = [c for c in controls if _is_probe_trigger(c)][:limit]
    for control in candidates:
        if not _reload(page, url):
            out.append(_entry(control, "button", "not_reached"))
            break
        locator = _locator_for(page, control)
        if locator is None:
            out.append(_entry(control, "button", "not_reached"))
            continue
        try:
            before = {_control_key(c) for c in page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)}
        except Exception:
            before = set()
        health, start = PageHealth(page, url), page.url
        try:
            locator.click(timeout=2500)
            page.wait_for_timeout(900)
        except Exception:
            health.detach(page)
            out.append(_entry(control, "button", "not_reached"))
            continue
        health.freeze()
        errors = _errors(health)
        if page.url != start:
            item = _entry(control, "button", "navigates", to=page.url, status=health.main_status)
        else:
            try:
                after = {_control_key(c) for c in page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)}
            except Exception:
                after = before
            item = _entry(control, "button", "reveals" if after - before else "no_visible_change")
        if errors:
            item["errors"] = errors
        health.detach(page)
        out.append(item)
        if log:
            log.info('sweep: "%s" -> %s%s', item["name"], item["outcome"], f" ({len(errors)} script error(s))" if errors else "")
    return out


def sweep_fields(page, url: str, controls: list[dict], limit: int) -> list[dict]:
    """Choose the first real option / type a plausible value. Never presses Enter or submits."""
    fields = [c for c in actionable_controls({"controls": controls})
              if (c.get("tag") == "select" or (c.get("tag") in {"input", "textarea"}
                                                and (c.get("type") or "text").lower() in {"text", "email", "tel", "number", "search", "url", "date", ""}))
              and not c.get("readonly")][:limit]
    if not fields:
        return []
    out = []
    if not _reload(page, url):
        return [_entry(c, "field", "not_reached") for c in fields]
    health = PageHealth(page, url)
    for control in fields:
        locator = _flow_locator(page, control)
        seen_errors = len(health.page_errors)
        outcome = "not_reached"
        if locator is not None:
            try:
                if control.get("tag") == "select":
                    outcome = "selected" if _select_first_real(locator) is not None else "not_reached"
                else:
                    locator.fill(_plausible_value(control), timeout=2000)
                    outcome = "filled"
            except Exception:
                outcome = "not_reached"
        item = _entry(control, "field", outcome)
        if len(health.page_errors) > seen_errors:
            item["errors"] = [e["text"] for e in health.page_errors[seen_errors:]][:3]
        out.append(item)
    health.detach(page)
    return out


def sweep_page(page, url: str, controls: list[dict], max_controls: int, log=None) -> list[dict]:
    """Every safe control on the page, once. `max_controls` bounds the clicks and the field pass (each click
    reloads the page, so this is where the time goes); links are checked by request and bounded by MAX_LINKS."""
    if max_controls <= 0:
        return []
    out: list[dict] = []
    for step in (lambda: sweep_links(page, url, controls),
                 lambda: sweep_buttons(page, url, controls, max_controls, log),
                 lambda: sweep_fields(page, url, controls, max_controls)):
        try:
            out.extend(step())
        except Exception as exc:       # the sweep is additive: it must never lose the page's inventory
            if log:
                log.info("sweep: a step errored (%s)", str(exc).splitlines()[0][:150])
    if log:
        log.info("sweep: %s touched %d of %d control(s)", url, sum(touched(i) for i in out), len(out))
    return out
