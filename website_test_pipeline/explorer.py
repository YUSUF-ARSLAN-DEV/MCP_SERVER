from .models import PageInventory

def explore(page, url: str) -> PageInventory:
    title = page.title()
    headings = page.locator('h1,h2,h3,[role="heading"]').evaluate_all("""els => els.filter(e => e.getClientRects().length).map(e => ({level:e.tagName,text:(e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,180)}))""")
    controls = page.locator('a,button,input,select,textarea,[role]').evaluate_all("""els => els.filter(e => { const s=getComputedStyle(e); return s.display!=='none' && s.visibility!=='hidden' && e.getClientRects().length; }).map(e => ({tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),name:e.getAttribute('aria-label') || (e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,160),href:e.getAttribute('href'),type:e.getAttribute('type'),value:e.value || null,disabled:e.disabled || e.hasAttribute('disabled')})).slice(0,120)""")
    try: accessibility = page.locator('body').aria_snapshot()
    except Exception: accessibility = ''
    signals = '\n'.join(x for x in accessibility.splitlines() if any(k in x.lower() for k in ('heading','button','link','textbox','combobox','listbox','checkbox','radio','form')))[:9000]
    return PageInventory(url, title, headings, controls, signals)
