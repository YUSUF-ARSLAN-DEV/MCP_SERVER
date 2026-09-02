from .models import PageInventory
from .pageutils import (
    _ACCEPT_NAMES,
    _CLOSE_NAMES,
    dismiss_overlays,
    prime_lazy_content,
    settle_page,
)

_CONTROL_SEL = 'a,button,input,select,textarea,video,audio,[role]'

# Statements injected at the top of each scrape's arrow-function body.
_JS_HELPERS = r"""
    const CHROME = 'header, nav, footer, [role="banner"], [role="contentinfo"], [role="navigation"]';
    const CONTENT = 'main, [role="main"], article, [role="article"]';
    const regionOf = e => e.closest(CONTENT) ? 'content' : (e.closest(CHROME) ? 'chrome' : 'other');
    const volatileId = v => !!v && (
        /\d{4,}$/.test(v)                                    // trailing 4+ digit run: newsletter-email-768180
        || /^(radix|mui|headlessui|ember|rc|:r|__reakit|downshift)/i.test(v)   // framework-generated
        || /^[a-f0-9]{8,}$/i.test(v)                         // hex blob
        || /[-_][a-f0-9]{6,}$/i.test(v)                      // trailing hex chunk
    );
"""

_HEADINGS_JS = "els => {" + _JS_HELPERS + r"""
    return els.filter(e => e.getClientRects().length).map(e => {
        const r = e.getBoundingClientRect();
        const cs = getComputedStyle(e);
        const hidden = (r.width <= 1 || r.height <= 1)
            || cs.clip === 'rect(0px, 0px, 0px, 0px)'
            || cs.clipPath === 'inset(50%)'
            || parseFloat(cs.opacity) === 0
            || cs.visibility === 'hidden';
        return {
            level: e.tagName,
            text: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 180),
            region: regionOf(e),
            in_feed: !!e.closest('article, [role="article"], [role="feed"]'),
            hidden: hidden
        };
    });
}"""

_CONTROLS_JS = "els => {" + _JS_HELPERS + r"""
    return els.filter(e => {
        const s = getComputedStyle(e);
        return s.display !== 'none' && s.visibility !== 'hidden' && e.getClientRects().length;
    }).slice(0, 150).map(e => {
        const isInputButton = e.tagName === 'INPUT' && ['button','submit','reset'].includes((e.type||'').toLowerCase());
        const isMedia = e.tagName === 'VIDEO' || e.tagName === 'AUDIO';
        const label = e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].textContent) || e.getAttribute('placeholder') || (isInputButton ? e.value : '') || (isMedia ? e.tagName.toLowerCase() + ' player' : '') || (e.textContent||'').trim().replace(/\s+/g,' ').slice(0,120);
        const testid = (e.dataset && e.dataset.testid) || null;
        const rawId = e.id || null;
        const rawName = e.getAttribute('name') || null;
        const idOk = rawId && !volatileId(rawId);
        const nameOk = rawName && !volatileId(rawName);
        let selector = null;
        if (testid) selector = `[data-testid="${testid}"]`;
        else if (idOk) selector = `#${rawId}`;
        else if (nameOk) selector = `${e.tagName.toLowerCase()}[name="${rawName}"]`;
        else if (isMedia) selector = e.tagName.toLowerCase();
        return {
            tag: e.tagName.toLowerCase(),
            role: e.getAttribute('role') || e.type || null,
            name: label,
            selector: selector,
            testid: testid,
            id: idOk ? rawId : null,
            field_name: nameOk ? rawName : null,
            volatile_id: !testid && ((!!rawId && !idOk) || (!!rawName && !nameOk)),
            region: regionOf(e),
            type: e.getAttribute('type'),
            href: e.getAttribute('href'),
            value: e.value || null,
            placeholder: e.getAttribute('placeholder') || null,
            required: e.required || e.getAttribute('aria-required') === 'true',
            disabled: e.disabled || e.hasAttribute('disabled'),
            checked: (e.type === 'checkbox' || e.type === 'radio') ? e.checked : null,
            in_form: !!e.closest('form'),
            options: e.tagName === 'SELECT' ? [...e.options].slice(0,20).map(o => o.value || (o.textContent||'').trim()) : null
        };
    });
}"""

_FORMS_JS = r"""els => els.map(f => ({
    selector: f.id ? `#${f.id}` : (f.getAttribute('name') ? `form[name="${f.getAttribute('name')}"]` : null),
    action: f.getAttribute('action'),
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    fields: [...f.elements].map(el => el.getAttribute('name')).filter(Boolean)
}))"""


# Trigger names we never click during the probe: consent/close buttons (already
# handled by dismiss_overlays) and anything that reads as destructive or as a
# checkout / auth step.
_PROBE_SKIP_NAMES = {n.strip().lower() for n in (_ACCEPT_NAMES + _CLOSE_NAMES)}
_PROBE_SKIP_SUBSTR = (
    "delete", "remove", "logout", "log out", "sign out", "unsubscribe",
    "buy", "pay", "checkout", "purchase", "donate", "confirm", "submit", "send",
    "cart", "order", "book now", "reserve", "apply now", "register", "download",
)


def _control_key(control: dict) -> tuple:
    return (
        control.get("tag"),
        (control.get("name") or "").strip().lower()[:60],
        control.get("selector") or control.get("id") or control.get("field_name") or "",
    )


def _locator_for(page, control: dict):
    selector = control.get("selector")
    if selector:
        return page.locator(selector).first
    name = (control.get("name") or "").strip()
    if not name:
        return None
    role = "button" if control.get("tag") == "button" else (control.get("role") or "button")
    try:
        return page.get_by_role(role, name=name, exact=True).first
    except Exception:
        return None


def _probe_interactions(page, url: str, baseline: list[dict], limit: int) -> list[dict]:
    """Click up to ``limit`` [content] triggers and record what each surfaces.

    Reloads the page before every probe so each result is isolated. A trigger
    that navigates is recorded as ``navigates``; one that mounts new in-page
    controls is recorded as ``reveals`` with those controls, giving the generator
    a real postcondition to assert instead of a bare visibility check.
    """
    if limit <= 0:
        return []
    known: set[tuple] = {_control_key(c) for c in baseline}
    triggers = [
        c for c in baseline
        if c.get("region") == "content"
        and c.get("tag") == "button"
        and not c.get("disabled")
        and not c.get("in_form")
        and (c.get("name") or "").strip()
        and (c.get("name") or "").strip().lower() not in _PROBE_SKIP_NAMES
        and not any(s in (c.get("name") or "").lower() for s in _PROBE_SKIP_SUBSTR)
    ]
    revealed: list[dict] = []
    for trigger in triggers[:limit]:
        name = (trigger.get("name") or "").strip()
        try:
            page.goto(url, wait_until="domcontentloaded")
            settle_page(page)
            dismiss_overlays(page)
        except Exception:
            break
        try:
            pre = page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)
        except Exception:
            pre = []
        pre_keys = known | {_control_key(c) for c in pre}
        locator = _locator_for(page, trigger)
        if locator is None:
            continue
        before_url = page.url
        try:
            locator.click(timeout=2500)
            page.wait_for_timeout(700)
        except Exception:
            continue
        if page.url != before_url:
            revealed.append({"trigger": name, "effect": "navigates", "to": page.url})
            continue
        try:
            after = page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)
        except Exception:
            after = []
        fresh = [
            c for c in after
            if _control_key(c) not in pre_keys
            and c.get("region") in {"content", "other"}
            and (c.get("name") or c.get("selector") or c.get("id"))
        ][:12]
        if fresh:
            revealed.append({"trigger": name, "effect": "reveals", "controls": fresh})
            known |= {_control_key(c) for c in fresh}
    return revealed


def explore(page, url: str, probe_max: int = 5) -> PageInventory:
    settle_page(page)
    dismiss_overlays(page)  # snapshot the real page, not consent / ad overlays
    prime_lazy_content(page)  # mount lazy footers / newsletter widgets before scraping
    title = page.title()
    headings = page.locator('h1,h2,h3,h4,h5,h6,[role="heading"]').evaluate_all(_HEADINGS_JS)
    controls = page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)
    # Flag accessible names the crawl saw on more than one element - a bare
    # get_by_role(name=...) on these raises a strict-mode violation at run time.
    name_counts: dict[str, int] = {}
    for control in controls:
        key = (control.get("name") or "").strip().lower()
        if key:
            name_counts[key] = name_counts.get(key, 0) + 1
    for control in controls:
        key = (control.get("name") or "").strip().lower()
        control["ambiguous"] = bool(key) and name_counts.get(key, 0) > 1
    forms = page.locator('form').evaluate_all(_FORMS_JS)
    try:
        accessibility = page.locator('body').aria_snapshot()
    except Exception:
        accessibility = ''
    signals = '\n'.join(
        x for x in accessibility.splitlines()
        if any(k in x.lower() for k in ('heading', 'button', 'link', 'textbox', 'combobox', 'listbox', 'checkbox', 'radio', 'form'))
    )[:9000]
    # Interaction probe last: it navigates away and reloads, so it must run after
    # every static scrape above is done.
    revealed = _probe_interactions(page, url, controls, probe_max)
    return PageInventory(url, title, headings, controls, signals, forms, revealed)
