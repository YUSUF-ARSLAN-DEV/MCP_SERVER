from .models import PageInventory
from .pageutils import dismiss_overlays, settle_page, prime_lazy_content

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


def explore(page, url: str) -> PageInventory:
    settle_page(page)
    dismiss_overlays(page)  # snapshot the real page, not consent / ad overlays
    prime_lazy_content(page)  # mount lazy footers / newsletter widgets before scraping
    title = page.title()
    headings = page.locator('h1,h2,h3,h4,h5,h6,[role="heading"]').evaluate_all(_HEADINGS_JS)
    controls = page.locator('a,button,input,select,textarea,video,audio,[role]').evaluate_all(_CONTROLS_JS)
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
    return PageInventory(url, title, headings, controls, signals, forms)
