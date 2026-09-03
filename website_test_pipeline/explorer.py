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
    '[aria-modal="true"],dialog,table,fieldset'
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
            page.wait_for_timeout(800)
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
    if probe_max > 0:
        revealed = revealed + _probe_forms(page, url, forms, min(2, probe_max), log)
    return PageInventory(url, title, headings, controls, signals, forms, revealed)
