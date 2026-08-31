from .models import PageInventory
from .pageutils import dismiss_overlays

def explore(page, url: str) -> PageInventory:
    dismiss_overlays(page)  # snapshot the real page, not consent / ad overlays
    title = page.title()
    headings = page.locator('h1,h2,h3,h4,h5,h6,[role="heading"]').evaluate_all("""els => els.filter(e => e.getClientRects().length).map(e => ({level:e.tagName,text:(e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,180)}))""")
    controls = page.locator('a,button,input,select,textarea,[role]').evaluate_all("""els => els.filter(e => { const s=getComputedStyle(e); return s.display!=='none' && s.visibility!=='hidden' && e.getClientRects().length; }).slice(0,150).map(e => {
        const isInputButton = e.tagName === 'INPUT' && ['button','submit','reset'].includes((e.type||'').toLowerCase());
        const label = e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].textContent) || e.getAttribute('placeholder') || (isInputButton ? e.value : '') || (e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,120);
        const testid = (e.dataset && e.dataset.testid) || null;
        let selector = null;
        if (testid) selector = `[data-testid="${testid}"]`;
        else if (e.id) selector = `#${e.id}`;
        else if (e.getAttribute('name')) selector = `${e.tagName.toLowerCase()}[name="${e.getAttribute('name')}"]`;
        return {
            tag: e.tagName.toLowerCase(),
            role: e.getAttribute('role') || e.type || null,
            name: label,
            selector: selector,
            testid: testid,
            id: e.id || null,
            field_name: e.getAttribute('name') || null,
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
    })""")
    forms = page.locator('form').evaluate_all("""els => els.map(f => ({
        selector: f.id ? `#${f.id}` : (f.getAttribute('name') ? `form[name="${f.getAttribute('name')}"]` : null),
        action: f.getAttribute('action'),
        method: (f.getAttribute('method') || 'get').toLowerCase(),
        fields: [...f.elements].map(el => el.getAttribute('name')).filter(Boolean)
    }))""")
    try: accessibility = page.locator('body').aria_snapshot()
    except Exception: accessibility = ''
    signals = '\n'.join(x for x in accessibility.splitlines() if any(k in x.lower() for k in ('heading','button','link','textbox','combobox','listbox','checkbox','radio','form')))[:9000]
    return PageInventory(url, title, headings, controls, signals, forms)
