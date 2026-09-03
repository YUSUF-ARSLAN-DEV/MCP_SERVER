import re

from .models import PageInventory
from .pageutils import (
    _ACCEPT_NAMES,
    _CLOSE_NAMES,
    dismiss_overlays,
    prime_lazy_content,
    settle_page,
)

_CONTROL_SEL = 'a,button,input,select,textarea,video,audio,[role]'
_PANEL_SEL = (
    '[role="dialog"],[role="region"],[role="tabpanel"],[role="listbox"],[role="menu"],'
    '[role="grid"],[role="table"],[role="tree"],[role="alert"],[role="status"],'
    '[aria-modal="true"],dialog,table,fieldset,'
    # widget dropdowns that carry no ARIA role - jQuery-UI multiselect, select2,
    # chosen, MUI, generic .dropdown-menu - so "click opens the channel picker"
    # is recorded even though the options inside it are 1px sr-only nodes.
    '.ui-multiselect-menu,.ui-menu,.select2-dropdown,.select2-results,.chosen-drop,'
    '[class*="dropdown-menu"],[class*="multiselect__content"],[class*="MuiMenu-"],[class*="-listbox"]'
)
_VALIDATION_SEL = (
    '[role="alert"],[aria-invalid="true"],.error,.errors,.invalid-feedback,.field-error,'
    '.form-error,.help-block,.messages,.alert,.parsley-errors-list,[class*="error" i]'
)

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
    // A stable CSS selector: prefer #id, else a class selector built from the
    // element's error/validation-ish classes (Drupal's <div class="messages messages--error">
    // has no id, so without this the model is left guessing [role=alert]).
    const MEANINGFUL_CLS = /(^|[-_])(error|errors|invalid|warning|alert|danger|message|messages|feedback|help-block)($|[-_])/i;
    const classSelector = e => {
        const picks = [...(e.classList || [])]
            .filter(c => MEANINGFUL_CLS.test(c) && !volatileId(c))
            .slice(0, 3)
            .map(c => '.' + c.replace(/([^\w-])/g, '\\$1'));
        return picks.length ? picks.join('') : null;
    };
    const stableSelector = e => (e.id && !volatileId(e.id)) ? ('#' + e.id) : classSelector(e);
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
        // Visually-hidden but DOM-present: sr-only fields, and (the case that bites us)
        // jQuery-UI multiselect's .ui-helper-hidden-accessible checkboxes that a menu
        // click "reveals" - the model then asserts to_be_visible() on a 1px offscreen node.
        const r = e.getBoundingClientRect();
        const cs = getComputedStyle(e);
        const hidden = r.width <= 1 || r.height <= 1
            || parseFloat(cs.opacity) === 0
            || r.bottom < 0 || r.right < 0
            || r.left > (window.innerWidth || 10000) + 1500;
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
            hidden: hidden,
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

_VALIDATION_JS = "els => {" + _JS_HELPERS + r"""
    return els.filter(e => {
        const s = getComputedStyle(e);
        return s.display !== 'none' && s.visibility !== 'hidden'
            && (e.textContent || '').trim().length > 0;
    }).slice(0, 20).map(e => ({
        tag: e.tagName.toLowerCase(),
        role: e.getAttribute('role') || null,
        name: (e.getAttribute('aria-label') || (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60)),
        selector: stableSelector(e),
        region: regionOf(e),
        text: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 140)
    }));
}"""

_PANELS_JS = "els => {" + _JS_HELPERS + r"""
    return els.filter(e => {
        const s = getComputedStyle(e);
        const r = e.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden'
            && parseFloat(s.opacity) !== 0 && r.width > 1 && r.height > 1;
    }).slice(0, 40).map(e => {
        const h = e.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"],legend,caption');
        const name = e.getAttribute('aria-label')
            || (h && (h.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80))
            || (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80);
        return {
            tag: e.tagName.toLowerCase(),
            role: e.getAttribute('role') || null,
            name: name,
            selector: e.id ? `#${e.id}` : null,
            region: regionOf(e),
            text: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 160)
        };
    });
}"""

# Accessible-name frequency across ALL links/buttons - hidden ones included - so
# a wizard's later-step "Next" buttons (display:none at snapshot) still mark the
# control AMBIGUOUS and the generator is told to add .first.
_NAME_FREQ_JS = r"""els => {
    const norm = e => (
        e.getAttribute('aria-label')
        || (e.tagName === 'INPUT' ? (e.value || '') : '')
        || (e.textContent || '')
    ).replace(/\s+/g, ' ').trim().toLowerCase().slice(0, 80);
    const freq = {};
    els.forEach(e => { const n = norm(e); if (n) freq[n] = (freq[n] || 0) + 1; });
    return freq;
}"""

_FORMS_JS = r"""els => els.map(f => ({
    selector: f.id ? `#${f.id}` : (f.getAttribute('name') ? `form[name="${f.getAttribute('name')}"]` : null),
    action: f.getAttribute('action'),
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    fields: [...f.elements].map(el => el.getAttribute('name')).filter(Boolean)
}))"""

# Third-party map embeds (Google Maps JS canvas, Leaflet, a maps <iframe>). These
# pages have no driveable DOM - the only honest assertion is "the map container
# rendered". Given a broad candidate set, keep only the ones that really are a map.
_EMBEDS_JS = "els => {" + _JS_HELPERS + r"""
    const MAP_SRC = /(google\.[a-z.]+\/maps|maps\.google|\/maps\/embed|openstreetmap\.org|mapbox\.com|api\.mapbox|bing\.com\/maps|arcgis\.com|2gis\.|yandex\.[a-z]+\/map)/i;
    const seen = new Set();
    const out = [];
    for (const e of els) {
        const cn = (typeof e.className === 'string' ? e.className : '') + ' ' + (e.id || '');
        let kind = null, provider = null;
        if (e.tagName === 'IFRAME') {
            const src = e.getAttribute('src') || e.getAttribute('data-src') || '';
            if (!MAP_SRC.test(src)) continue;
            kind = 'map'; provider = (src.match(MAP_SRC) || ['iframe'])[0].toLowerCase();
        } else if (e.classList.contains('gm-style') || e.querySelector('.gm-style')) {
            kind = 'map'; provider = 'google-maps-js';
        } else if (e.classList.contains('leaflet-container') || e.querySelector('.leaflet-container')) {
            kind = 'map'; provider = 'leaflet';
        } else if (/(^|[\s_#-])maps?([\s_-]|canvas|container|$)/i.test(cn) && e.querySelector('canvas')) {
            kind = 'map'; provider = 'canvas';
        } else {
            continue;
        }
        let selector = null;
        if (e.id && !volatileId(e.id)) selector = '#' + e.id;
        else if (e.tagName === 'IFRAME') selector = 'iframe';
        else {
            const cls = [...(e.classList || [])]
                .filter(c => !volatileId(c) && /map|leaflet|gm-style/i.test(c))
                .slice(0, 2).map(c => '.' + c.replace(/([^\w-])/g, '\\$1'));
            if (cls.length) selector = cls.join('');
        }
        const key = (selector || '') + '|' + provider;
        if (seen.has(key)) continue;
        seen.add(key);
        const r = e.getBoundingClientRect();
        // A selector for the RENDERED content, not just the container - asserting
        // this waits for the map to actually paint before the screenshot.
        const under = (selector && selector[0] === '#') ? selector + ' ' : '';
        let content_selector = null;
        if (provider === 'google-maps-js') content_selector = under + '.gm-style';
        else if (provider === 'leaflet') content_selector = under + '.leaflet-container';
        else if (provider === 'canvas') content_selector = under + 'canvas';
        out.push({
            kind: kind, provider: provider, selector: selector,
            content_selector: content_selector,
            tag: e.tagName.toLowerCase(),
            title: e.getAttribute('title') || e.getAttribute('aria-label') || null,
            region: regionOf(e),
            big: (r.width * r.height) > 60000
        });
    }
    return out.slice(0, 8);
}"""
_EMBED_SEL = ('iframe,.gm-style,.leaflet-container,'
              '[class*="map"],[class*="Map"],[id*="map"],[id*="Map"]')


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


def _panel_key(panel: dict) -> tuple:
    return (
        panel.get("tag"),
        panel.get("role"),
        (panel.get("name") or "").strip().lower()[:40],
        panel.get("selector") or "",
    )


def _is_probe_trigger(control: dict) -> bool:
    """Buttons the probe may safely click: <button>, <input type=button>, and
    [role=button] anywhere outside the site chrome - excluding submit/reset and
    anything that reads as destructive or as a checkout / auth step. Many sites
    never use <main>, so page-specific buttons land in region 'other'."""
    if control.get("disabled") or control.get("hidden") or control.get("region") == "chrome":
        return False
    name = (control.get("name") or "").strip()
    if not name or name.lower() in _PROBE_SKIP_NAMES:
        return False
    if any(s in name.lower() for s in _PROBE_SKIP_SUBSTR):
        return False
    tag = control.get("tag")
    typ = (control.get("type") or "").lower()
    role = (control.get("role") or "").lower()
    if tag == "input" and typ in {"submit", "reset"}:
        return False
    if tag == "button" and typ != "button" and control.get("in_form"):
        return False  # a bare <button> in a form defaults to type=submit
    return tag == "button" or (tag == "input" and typ == "button") or role == "button"


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


def _probe_interactions(page, url: str, baseline: list[dict], limit: int, log=None) -> list[dict]:
    """Click up to ``limit`` [content] triggers and record what each surfaces.

    Reloads the page before every probe so each result is isolated. A trigger
    that navigates is recorded as ``navigates``; one that mounts new in-page
    controls or panels is recorded as ``reveals`` with them, giving the generator
    a real postcondition to assert instead of a bare visibility check.
    """
    if limit <= 0:
        return []
    known_ctl: set[tuple] = {_control_key(c) for c in baseline}
    known_panel: set[tuple] = set()
    triggers = [c for c in baseline if _is_probe_trigger(c)]
    if log:
        log.info("probe: %d candidate trigger(s) on %s", min(len(triggers), limit), url)
    revealed: list[dict] = []
    for trigger in triggers[:limit]:
        name = (trigger.get("name") or "").strip()
        try:
            page.goto(url, wait_until="domcontentloaded")
            settle_page(page)
            dismiss_overlays(page)
        except Exception as exc:
            if log:
                log.info('probe: "%s" -> reload failed (%s)', name, exc)
            break
        try:
            pre_ctl = page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)
            pre_pan = page.locator(_PANEL_SEL).evaluate_all(_PANELS_JS)
        except Exception:
            pre_ctl, pre_pan = [], []
        pre_ctl_keys = known_ctl | {_control_key(c) for c in pre_ctl}
        pre_pan_keys = known_panel | {_panel_key(p) for p in pre_pan}
        locator = _locator_for(page, trigger)
        if locator is None:
            if log:
                log.info('probe: "%s" -> no usable locator', name)
            continue
        before_url = page.url
        try:
            locator.click(timeout=2500)
            page.wait_for_timeout(1200)  # widgets that animate a dropdown open
        except Exception as exc:
            if log:
                log.info('probe: "%s" -> click failed (%s)', name, str(exc).splitlines()[0][:120])
            continue
        if page.url != before_url:
            revealed.append({"trigger": name, "effect": "navigates", "to": page.url})
            if log:
                log.info('probe: "%s" -> navigates to %s', name, page.url)
            continue
        try:
            post_ctl = page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)
            post_pan = page.locator(_PANEL_SEL).evaluate_all(_PANELS_JS)
        except Exception:
            post_ctl, post_pan = [], []
        fresh_ctl = [
            c for c in post_ctl
            if _control_key(c) not in pre_ctl_keys
            and c.get("region") in {"content", "other"}
            and not c.get("hidden")  # skip 1px sr-only nodes (jQuery-UI multiselect checkboxes)
            and (c.get("name") or c.get("selector") or c.get("id"))
        ][:12]
        fresh_pan = [
            p for p in post_pan
            if _panel_key(p) not in pre_pan_keys
            and p.get("region") in {"content", "other"}
            and (p.get("name") or "").strip()
        ][:6]
        combined = fresh_ctl + fresh_pan
        if combined:
            revealed.append({"trigger": name, "effect": "reveals", "controls": combined})
            known_ctl |= {_control_key(c) for c in fresh_ctl}
            known_panel |= {_panel_key(p) for p in fresh_pan}
            if log:
                log.info('probe: "%s" -> reveals %d element(s)', name, len(combined))
        elif log:
            log.info('probe: "%s" -> no visible change', name)
    return revealed


def _form_submit_locator(page, form: dict):
    selector = form.get("selector")
    scope = page.locator(selector) if selector else page.locator("form")
    for candidate in ('button[type="submit"]', 'input[type="submit"]', 'button:not([type])'):
        try:
            loc = scope.locator(candidate).first
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def _probe_forms(page, url: str, forms: list[dict], limit: int, log=None) -> list[dict]:
    """Submit each form with nothing filled in and record the validation state -
    error regions that appeared, or how many fields the browser marked :invalid.
    Gives the generator a real 'submit empty -> errors' postcondition to assert."""
    if limit <= 0 or not forms:
        return []
    out: list[dict] = []
    for form in forms[:limit]:
        label = form.get("selector") or form.get("action") or "form"
        try:
            page.goto(url, wait_until="domcontentloaded")
            settle_page(page)
            dismiss_overlays(page)
        except Exception:
            break
        submit = _form_submit_locator(page, form)
        if submit is None:
            if log:
                log.info('probe: form %s -> no submit button found', label)
            continue
        try:
            pre = page.locator(_VALIDATION_SEL).evaluate_all(_VALIDATION_JS)
        except Exception:
            pre = []
        pre_keys = {_panel_key(p) for p in pre}
        before_url = page.url
        try:
            submit.click(timeout=2500)
            page.wait_for_timeout(900)
        except Exception as exc:
            if log:
                log.info('probe: submit %s -> click failed (%s)', label, str(exc).splitlines()[0][:100])
            continue
        if page.url != before_url:
            out.append({"trigger": f"submit {label} with no input", "effect": "submits-without-validation", "to": page.url})
            if log:
                log.info('probe: submit %s -> navigated (no client validation)', label)
            continue
        try:
            post = page.locator(_VALIDATION_SEL).evaluate_all(_VALIDATION_JS)
        except Exception:
            post = []
        try:
            native_invalid = int(page.evaluate(
                "document.querySelectorAll('input:invalid, select:invalid, textarea:invalid').length"
            ))
        except Exception:
            native_invalid = 0
        fresh = [
            p for p in post
            if _panel_key(p) not in pre_keys and p.get("region") in {"content", "other"}
        ][:8]
        if fresh or native_invalid:
            entry = {"trigger": f"submit {label} with no input", "effect": "validation", "controls": fresh}
            if native_invalid:
                entry["native_invalid_fields"] = native_invalid
            out.append(entry)
            if log:
                log.info('probe: submit %s -> validation (%d error region(s), %d native-invalid field(s))',
                         label, len(fresh), native_invalid)
        elif log:
            log.info('probe: submit %s -> no visible validation', label)
    return out


# -------------------------------------------------------------- primary-flow probe
# The page's MAIN interaction is often a search / filter widget that is NOT a
# <form> (a <select> + a button wired by JS). _probe_forms never sees it. This
# probe finds that widget, fills it with real values, submits it, and records the
# result - giving the generator the ONE test that is an actual smoke test.

_ACTION_VERB = re.compile(
    r"\b(search|find|look\s?up|filter|apply|show(?:\s+results?)?|go|submit|"
    r"see\s+results?|get\s+results?|explore|check|calculate)\b", re.I)
_PLACEHOLDER_OPT = re.compile(
    r"^\s*(-+\s*$|please\s+(?:select|choose)|select\s|choose\s|--|all\b|any\b|none\b|n/?a\b)", re.I)
_PLACEHOLDER_VAL = {"", "-1", "0", "null", "none", "all", "any", "undefined"}
_MS_HINT = re.compile(r"select|choose|channel|category|option|type|brand|make|model|topic", re.I)

_RESULTS_SEL = (
    'main,[role="main"],#content,#main,.site-main,article,[role="feed"],[aria-live],'
    'table,[role="table"],[role="grid"],[role="list"],'
    '[class*="result" i],[class*="listing" i],[class*="search-res" i],[class*="posts" i],[id*="result" i]'
)
_RESULTS_JS = "els => {" + _JS_HELPERS + r"""
    const CHROME_SEL = 'header, nav, footer, [role="banner"], [role="contentinfo"], [role="navigation"], [class*="menu" i], [class*="nav" i]';
    const looksLikeCode = t => /[.#][\w-]+\s*\{|@media|function\s*\(|;\s*\}/.test(t.slice(0, 200));
    return els.filter(e => {
        if (e.closest(CHROME_SEL)) return false;
        if (['STYLE', 'SCRIPT', 'NAV', 'HEADER', 'FOOTER'].includes(e.tagName)) return false;
        const s = getComputedStyle(e); const r = e.getBoundingClientRect();
        const txt = (e.textContent || '').trim();
        return s.display !== 'none' && s.visibility !== 'hidden'
            && r.width > 40 && r.height > 40 && txt.length > 30 && !looksLikeCode(txt);
    }).slice(0, 40).map(e => {
        // count things that read as result items, not menu links
        const rows = e.querySelectorAll(
            'article, [class*="result" i], [class*="post" i]:not([class*="poster" i]), '
            + 'li:not(nav li):not([class*="menu" i] li), tr, [role="row"], [role="listitem"], [role="article"]'
        ).length;
        return {
            tag: e.tagName.toLowerCase(),
            role: e.getAttribute('role') || null,
            selector: (e.id && !volatileId(e.id)) ? ('#' + e.id) : null,
            klass: (typeof e.className === 'string'
                ? '.' + e.className.trim().split(/\s+/).filter(c => c && !volatileId(c)).slice(0, 2).join('.')
                : null),
            region: regionOf(e),
            rows: rows,
            text: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120)
        };
    });
}"""


def _flow_locator(page, control: dict):
    selector = control.get("selector")
    if selector:
        try:
            return page.locator(selector).first
        except Exception:
            return None
    name = (control.get("name") or "").strip()
    if not name:
        return None
    role = control.get("role") or ("button" if control.get("tag") == "button" else None)
    if not role:
        return None
    try:
        return page.get_by_role(role, name=name, exact=True).first
    except Exception:
        return None


def _select_first_real(loc):
    """Pick the first non-placeholder option of a <select>; return its label."""
    try:
        opts = loc.evaluate("el => [...el.options].map(o => ({v: o.value, t: (o.textContent||'').trim()}))")
    except Exception:
        return None
    for opt in opts:
        value, text = (opt.get("v") or ""), (opt.get("t") or "")
        if value.strip().lower() in _PLACEHOLDER_VAL or _PLACEHOLDER_OPT.match(text):
            continue
        try:
            loc.select_option(value=value, timeout=1500)
            return text or value
        except Exception:
            return None
    return None


def _plausible_value(control: dict) -> str:
    typ = (control.get("type") or "text").lower()
    return {
        "email": "test@example.com", "tel": "0123456789", "number": "1",
        "date": "2025-01-01", "search": "a",
    }.get(typ, "test")


def _pick_multiselect(page, control: dict) -> str | None:
    """Open the widget and take its first real option; fall back to the underlying
    <select multiple> so a menu that won't open on a synthetic click still works."""
    loc = _flow_locator(page, control)
    if loc is not None:
        try:
            loc.click(timeout=2000)
            page.wait_for_timeout(700)
            menu = page.locator('.ui-multiselect-menu:visible, .select2-results:visible, [class*="dropdown-menu"]:visible').first
            option = menu.locator('li label, li [role="option"], li a').first
            if option.count():
                label = (option.inner_text(timeout=1000) or "").strip()
                option.click(timeout=1500)
                page.keyboard.press("Escape")
                return label[:80] or "first option"
        except Exception:
            pass
    try:
        sel = page.locator('select[multiple]').first
        if sel.count():
            picked = _select_first_real(sel)
            if picked:
                return picked
    except Exception:
        pass
    return None


def _search_term(url: str, headings: list[dict]) -> str:
    """A term that will actually return hits: the brand, else a content heading word."""
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    brand = re.sub(r"^www\.", "", host).split(".")[0]
    if len(brand) >= 4:
        return brand
    for h in headings or []:
        for word in re.findall(r"[A-Za-z]{5,}", h.get("text") or ""):
            return word.lower()
    return "the"


def _probe_search_form(page, url: str, forms: list[dict], headings: list[dict], log=None) -> dict | None:
    """A <form> with a free-text field (WordPress ?s=, site search) - fill it with a
    real term, submit, and record the results page. The commonest 'primary flow'."""
    for form in forms or []:
        fields = [f for f in (form.get("fields") or []) if f]
        if not fields:
            continue
        try:
            page.goto(url, wait_until="domcontentloaded")
            settle_page(page)
            dismiss_overlays(page)
        except Exception:
            return None
        scope = page.locator(form.get("selector")) if form.get("selector") else page.locator("form").first
        field = None
        for name in fields:
            cand = scope.locator(f'input[name="{name}"], textarea[name="{name}"]').first
            try:
                if cand.count() and (cand.get_attribute("type") or "text") in {"text", "search", None}:
                    field = cand
                    break
            except Exception:
                continue
        if field is None:
            continue
        term = _search_term(url, headings)
        try:
            field.fill(term, timeout=2000)
        except Exception:
            continue
        try:
            pre = page.locator(_RESULTS_SEL).evaluate_all(_RESULTS_JS)
        except Exception:
            pre = []
        pre_keys = {(p.get("selector"), (p.get("text") or "")[:40], p.get("rows")) for p in pre}
        before_url = page.url
        submit = _form_submit_locator(page, form)
        try:
            if submit is not None:
                submit.click(timeout=2500)
            else:
                field.press("Enter", timeout=2500)
            page.wait_for_timeout(2200)
        except Exception as exc:
            if log:
                log.info("primary-flow(search): submit failed (%s)", str(exc).splitlines()[0][:120])
            return None
        flow = {"action": f'search for "{term}"',
                "action_selector": form.get("selector"),
                "steps": [{"kind": "fill", "selector": f'input[name="{fields[0]}"]',
                           "name": "search field", "value": term}]}
        if page.url != before_url:
            flow["effect"] = "navigates"
            flow["to"] = page.url
        try:
            post = page.locator(_RESULTS_SEL).evaluate_all(_RESULTS_JS)
        except Exception:
            post = []
        fresh = [
            p for p in post
            if (p.get("selector"), (p.get("text") or "")[:40], p.get("rows")) not in pre_keys
            and p.get("region") in {"content", "other"} and (p.get("rows") or 0) >= 2
        ]
        if fresh:
            # most specific container: prefer one with a selector/class, then fewest rows
            best = min(fresh, key=lambda p: (not (p.get("selector") or p.get("klass")), p.get("rows") or 999))
            flow.update(effect="results",
                        results_selector=best.get("selector") or best.get("klass"),
                        results_role=best.get("role"), row_count=best.get("rows"),
                        results_text=best.get("text"))
        elif "effect" not in flow:
            flow["effect"] = "no-visible-result"
        if log:
            tail = (f' -> results in {flow.get("results_selector") or flow.get("results_role")} '
                    f'({flow.get("row_count")} rows)' if flow["effect"] == "results" else f' -> {flow["effect"]}')
            log.info('primary-flow(search): "%s"%s', term, tail)
        return flow
    return None


def _probe_primary_flow(page, url: str, controls: list[dict], forms: list[dict] | None = None,
                        headings: list[dict] | None = None, log=None) -> dict | None:
    content = [c for c in controls
               if c.get("region") in {"content", "other"} and not c.get("hidden") and not c.get("disabled")]
    action = next(
        (c for c in content
         if (c.get("tag") == "button" or c.get("role") == "button")
         and not c.get("href") and _ACTION_VERB.search((c.get("name") or ""))),
        None)
    if action is None:
        return _probe_search_form(page, url, forms or [], headings or [], log)
    selects = [c for c in content if c.get("tag") == "select" and (c.get("options") or [])]
    inputs = [c for c in content
              if c.get("tag") == "input" and (c.get("type") or "text") in
              {"text", "search", "tel", "email", "number", "date"}]
    ms_buttons = [c for c in content
                  if (c.get("tag") == "button" or c.get("role") == "button")
                  and c is not action and _MS_HINT.search((c.get("name") or ""))]
    if not (selects or inputs or ms_buttons):
        return None
    try:
        page.goto(url, wait_until="domcontentloaded")
        settle_page(page)
        dismiss_overlays(page)
    except Exception:
        return None

    steps: list[dict] = []
    for control in selects[:4]:
        loc = _flow_locator(page, control)
        if loc is None:
            continue
        picked = _select_first_real(loc)
        if picked is not None:
            steps.append({"kind": "select", "selector": control.get("selector"),
                          "name": control.get("name"), "value": picked})
    for control in inputs[:3]:
        loc = _flow_locator(page, control)
        if loc is None:
            continue
        value = _plausible_value(control)
        try:
            loc.fill(value, timeout=1500)
            steps.append({"kind": "fill", "selector": control.get("selector"),
                          "name": control.get("name"), "value": value})
        except Exception:
            pass
    for control in ms_buttons[:1]:
        picked = _pick_multiselect(page, control)
        if picked:
            steps.append({"kind": "multiselect", "selector": control.get("selector"),
                          "name": control.get("name"), "value": picked})
    if not steps:
        return None

    try:
        pre = page.locator(_RESULTS_SEL).evaluate_all(_RESULTS_JS)
    except Exception:
        pre = []
    pre_keys = {(p.get("selector"), (p.get("text") or "")[:40], p.get("rows")) for p in pre}
    before_url = page.url

    act_loc = _flow_locator(page, action)
    if act_loc is None:
        return None
    try:
        act_loc.click(timeout=2500)
        page.wait_for_timeout(2200)
    except Exception as exc:
        if log:
            log.info('primary-flow: "%s" click failed (%s)', action.get("name"), str(exc).splitlines()[0][:120])
        return None

    flow = {"action": action.get("name"), "action_selector": action.get("selector"), "steps": steps}
    if page.url != before_url:
        flow["effect"] = "navigates"
        flow["to"] = page.url
    else:
        try:
            post = page.locator(_RESULTS_SEL).evaluate_all(_RESULTS_JS)
        except Exception:
            post = []
        fresh = [
            p for p in post
            if (p.get("selector"), (p.get("text") or "")[:40], p.get("rows")) not in pre_keys
            and p.get("region") in {"content", "other"} and (p.get("rows") or 0) >= 2
        ]
        if fresh:
            # most specific container: prefer one with a selector/class, then fewest rows
            best = min(fresh, key=lambda p: (not (p.get("selector") or p.get("klass")), p.get("rows") or 999))
            flow.update(effect="results",
                        results_selector=best.get("selector") or best.get("klass"),
                        results_role=best.get("role"), row_count=best.get("rows"),
                        results_text=best.get("text"))
        else:
            flow["effect"] = "no-visible-result"
    if log:
        tail = (f' -> results in {flow.get("results_selector") or flow.get("results_role")} '
                f'({flow.get("row_count")} rows)' if flow["effect"] == "results"
                else f' -> {flow["effect"]}')
        log.info('primary-flow: "%s" + %d step(s)%s', action.get("name"), len(steps), tail)
    return flow


def explore(page, url: str, probe_max: int = 5, log=None) -> PageInventory:
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
    try:
        dom_name_freq = page.locator('a,button,input,[role="button"],[role="link"]').evaluate_all(_NAME_FREQ_JS)
    except Exception:
        dom_name_freq = {}
    for control in controls:
        key = (control.get("name") or "").strip().lower()
        # ambiguous if the visible snapshot saw the name twice OR the full DOM
        # (hidden elements included) has more than one - catches wizard steps.
        control["ambiguous"] = bool(key) and (
            name_counts.get(key, 0) > 1 or dom_name_freq.get(key[:80], 0) > 1
        )
    forms = page.locator('form').evaluate_all(_FORMS_JS)
    try:
        embeds = page.locator(_EMBED_SEL).evaluate_all(_EMBEDS_JS)
    except Exception:
        embeds = []
    if embeds and log:
        log.info("explore: %d map/media embed(s) on %s (%s)", len(embeds), url,
                 ", ".join(sorted({e.get("provider") or "?" for e in embeds})))
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
    revealed = _probe_interactions(page, url, controls, probe_max, log)
    primary_flow = None
    if probe_max > 0:
        revealed = revealed + _probe_forms(page, url, forms, min(2, probe_max), log)
        try:
            primary_flow = _probe_primary_flow(page, url, controls, forms, headings, log)
        except Exception as exc:
            if log:
                log.info("primary-flow: probe errored (%s)", str(exc).splitlines()[0][:150])
    return PageInventory(url, title, headings, controls, signals, forms, revealed, embeds, primary_flow)
