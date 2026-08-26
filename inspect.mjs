import { chromium } from '@playwright/test';

const urls = [
  'https://sat.aljazeera.net/en/frequency-search',
  'https://sat.aljazeera.net/ar/frequency-search',
];

const browser = await chromium.launch();
const page = await browser.newPage();

for (const url of urls) {
  console.log('\n================================================');
  console.log('URL:', url);
  console.log('================================================');
  try { await page.goto(url, { waitUntil: 'domcontentloaded' }); }
  catch (e) { console.log('goto err', e.message); continue; }
  await page.waitForTimeout(1500);

  const h1s = await page.locator('h1').allTextContents();
  console.log('H1 x', h1s.length, JSON.stringify(h1s.map(s => s.trim())));
  const h2s = await page.locator('h2').allTextContents();
  console.log('H2 x', h2s.length, JSON.stringify(h2s.map(s => s.trim())));

  const btns = await page.$$eval('button', els => els.map(e => ({
    text: (e.textContent || '').trim().slice(0, 25),
    aria: e.getAttribute('aria-label'),
    cls: e.className.slice(0, 40),
  })));
  console.log('BUTTONS:', JSON.stringify(btns));

  const toggler = await page.$$eval('.navbar-toggler,[data-toggle],[data-bs-toggle],[class*=toggle]',
    els => els.map(e => ({ tag: e.tagName, cls: e.className, aria: e.getAttribute('aria-label') })));
  console.log('TOGGLER:', JSON.stringify(toggler));

  const anchors = await page.$$eval('a[href^="#"]', els => els.map(e => ({ href: e.getAttribute('href'), text: (e.textContent || '').trim().slice(0, 25) })));
  console.log('ANCHORS(#):', JSON.stringify(anchors));

  const selects = await page.$$eval('select', els => els.map(e => ({ name: e.getAttribute('name'), id: e.id, opts: e.options.length })));
  console.log('SELECTS:', JSON.stringify(selects));
}
await browser.close();
