import { chromium } from 'playwright';
import { readFileSync, writeFileSync, appendFileSync, mkdirSync, renameSync, existsSync } from 'fs';
import { execSync } from 'child_process';
import 'dotenv/config';

// 1. Read inputs
const guide = readFileSync('AI-TEST-GUIDE.md', 'utf-8');
const persona = readFileSync('persona.txt', 'utf-8');
const urls = readFileSync('my-crawler/urls.txt', 'utf-8').split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#'));

function specName(rawUrl) {
  const parsed = new URL(rawUrl);
  const path = parsed.pathname.replace(/^\/+|\/+$/g, '').replace(/[^a-z0-9]+/gi, '-');
  const host = parsed.hostname.replace(/[^a-z0-9]+/gi, '-');
  return `${host}-${path || 'home'}`.toLowerCase().replace(/-+/g, '-');
}

// 2. Launch browser (visible so you can watch)
const browser = await chromium.launch({ headless: process.env.HEADLESS !== 'false' });
const page = await browser.newPage();

const API_URL = process.env.API_URL || 'https://llm-1.d4done.com/v1/chat/completions';
const MODEL_NAME = process.env.MODEL_NAME || 'google/gemma-4-26b-a4b-qat';
const API_KEY = process.env.API_KEY;

// AI connection (OpenAI-compatible format)
const MODEL_TIMEOUT_MS = Number(process.env.MODEL_TIMEOUT_MS || 300000);
const MODEL_RETRIES = Number(process.env.MODEL_RETRIES || 4);
const RETRY_BASE_MS = Number(process.env.RETRY_BASE_MS || 3000);
const DEBUG_LOG = 'generation-debug.log';
let requestNumber = 0;
writeFileSync(DEBUG_LOG, `Generation started ${new Date().toISOString()}\n`);

function debug(message, details = '') {
  const line = `[${new Date().toISOString()}] ${message}${details ? ` | ${details}` : ''}`;
  console.log(`  ${line}`);
  appendFileSync(DEBUG_LOG, `${line}\n`);
}

function preview(value, limit = 700) {
  return String(value ?? '').replace(/\s+/g, ' ').slice(0, limit);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function isTransientStatus(status) {
  return status === 408 || status === 409 || status === 425 || status === 429 || status === 500 || status === 502 || status === 503 || status === 504 || status === 524;
}

async function callModel(prompt) {
  const messages = [
    { role: 'system', content: 'You are a test spec generator. Output ONLY the TypeScript code block.' },
    { role: 'user', content: prompt }
  ];

  for (let attempt = 1; attempt <= MODEL_RETRIES; attempt++) {
    const requestId = ++requestNumber;
    const started = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), MODEL_TIMEOUT_MS);
    try {
      const resp = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${API_KEY}` },
        body: JSON.stringify({ model: MODEL_NAME, messages, temperature: 0.1, max_tokens: 3072, stream: false }),
        signal: controller.signal
      });
      const body = await resp.text();
      debug(`request #${requestId} response`, `attempt=${attempt}/${MODEL_RETRIES} status=${resp.status} ms=${Date.now() - started} bytes=${body.length}`);
      if (!resp.ok) {
        const error = new Error(`API error: ${resp.status}`);
        error.status = resp.status;
        error.body = body.slice(0, 1000);
        if (!isTransientStatus(resp.status) || attempt === MODEL_RETRIES) throw error;
        debug('transient server error; retrying', `request=#${requestId} status=${resp.status} waitMs=${RETRY_BASE_MS * 2 ** (attempt - 1)} body=${preview(body)}`);
        await sleep(RETRY_BASE_MS * 2 ** (attempt - 1));
        continue;
      }
      let data;
      try { data = JSON.parse(body); } catch {
        const error = new Error('API returned non-JSON success response');
        error.transient = true; error.body = body.slice(0, 1000);
        throw error;
      }
      let content = data.choices?.[0]?.message?.content ?? data.output_text ?? data.response ?? data.content;
      if (Array.isArray(content)) content = content.map(part => typeof part === 'string' ? part : part.text || '').join('');
      if (!content) {
        const error = new Error('API response had no usable model content');
        error.transient = true;
        error.body = `keys=${Object.keys(data).join(',')} preview=${preview(JSON.stringify(data))}`;
        throw error;
      }
      debug(`request #${requestId} accepted`, `contentChars=${String(content).length} keys=${Object.keys(data).join(',')}`);
      return content;
    } catch (error) {
      const transient = error.transient || error.name === 'AbortError' || error.code === 'ECONNRESET' || isTransientStatus(error.status);
      debug(`request #${requestId} failed`, `attempt=${attempt}/${MODEL_RETRIES} ms=${Date.now() - started} type=${error.name || 'Error'} message=${error.message} detail=${preview(error.body)}`);
      if (!transient || attempt === MODEL_RETRIES) throw error;
      debug('retrying model request', `request=#${requestId} waitMs=${RETRY_BASE_MS * 2 ** (attempt - 1)}`);
      await sleep(RETRY_BASE_MS * 2 ** (attempt - 1));
    } finally {
      clearTimeout(timeout);
    }
  }
}

async function explorePage(page) {
  const controls = await page.locator('a,button,input,select,textarea,[role]').evaluateAll(elements => elements
    .filter(element => {
      const style = window.getComputedStyle(element);
      return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
    })
    .map(element => ({
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute('role'),
      accessibleName: element.getAttribute('aria-label') || element.textContent?.trim().replace(/\s+/g, ' ').slice(0, 160),
      href: element.getAttribute('href'),
      type: element.getAttribute('type'),
      value: element instanceof HTMLInputElement || element instanceof HTMLSelectElement ? element.value : undefined,
      disabled: element.hasAttribute('disabled')
    }))
  );
  const headings = await page.locator('h1,h2,h3,[role="heading"]').evaluateAll(elements => elements
    .filter(element => element.getClientRects().length > 0)
    .map(element => ({ level: element.tagName.match(/H([1-3])/i)?.[1] || element.getAttribute('aria-level'), text: element.textContent?.trim().replace(/\s+/g, ' ').slice(0, 180) }))
  );
  return { headings, controls: controls.slice(0, 120) };
}

function compactAccessibilitySignals(snapshot) {
  const lines = snapshot.split(/\r?\n/).filter(line => /heading|button|link|textbox|combobox|listbox|checkbox|radio|tab|form/i.test(line));
  return lines.slice(0, 180).join('\n').slice(0, 9000);
}

// --- Deterministic cleanup of common weak-model artifacts ---
function sanitize(code) {
  // rejoin split test() calls: `test('x');\n async ({page}) => {` -> `test('x', async ({page}) => {`
  code = code.replace(/test\((\s*(?:'[^']*'|"[^"]*")\s*)\);\s*\r?\n\s*async\s*\(/g, (_m, t) => `test(${t.trim()}, async (`);
  // strip any stray markdown fence lines that survived extraction
  code = code.replace(/^\s*```(?:typescript|ts)?\s*$/gm, '');
  return code.trim() + '\n';
}

// --- Faithful validation: use Playwright's OWN loader (esbuild is too lenient) ---
function isValidSpec(file) {
  try {
    execSync(`npx playwright test "${file}" --list`, { stdio: 'pipe' });
    const code = readFileSync(file, 'utf-8');
    // A spec can load successfully and still be useless. Reject common
    // weak-model output before it reaches the suite.
    if (!/\bexpect\s*\(/.test(code)) return { valid: false, reason: 'no expect assertion found' };
    if (/:has-text\s*\(|locator\(\s*['"]text=|getByText\s*\(/.test(code)) return { valid: false, reason: 'unstable text selector found' };
    if (!/test\.describe\s*\(\s*['"]https?:\/\//.test(code)) return { valid: false, reason: 'full URL missing from test.describe' };
    if (/\.(click|selectOption|fill|check|uncheck|press|setInputFiles|hover)\s*\(/.test(code) && !/import\s*\{[^}]*actionEvidence/.test(code)) return { valid: false, reason: 'actionEvidence import missing' };
    execSync(`node scripts/validate_specs.mjs "${file}"`, { stdio: 'pipe' });
    return { valid: true, reason: '' };
  } catch (error) {
    return { valid: false, reason: error.stdout?.toString().trim() || error.stderr?.toString().trim() || error.message };
  }
}

mkdirSync('tests', { recursive: true });
mkdirSync('exploration', { recursive: true });
mkdirSync('_failed-specs', { recursive: true }); // quarantine, OUTSIDE tests/ so a bad file never runs

const failed = [];

// 3. Process each URL
for (const url of urls) {
  console.log(`\nProcessing: ${url}`);
  try {
    new URL(url);
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});
  } catch (error) {
    console.log(`  ❌ ${url}: could not load URL (${error.message})`);
    failed.push(url);
    continue;
  }
  const snapshot = await page.locator('body').ariaSnapshot();
  const title = await page.title();
  const exploration = await explorePage(page);
  const compactSignals = compactAccessibilitySignals(snapshot);
  const explorationFile = specName(url);
  writeFileSync(`exploration/${explorationFile}.aria-snapshot.txt`, snapshot);
  writeFileSync(`exploration/${explorationFile}.inventory.json`, JSON.stringify({ url, title, ...exploration }, null, 2));

  const prompt = `${guide}\n\nPERSONA:\n${persona}\n\nPAGE URL: ${url}\nPAGE TITLE: ${title}\nPAGE PATH: ${new URL(url).pathname}\n\nCOMPACT PAGE INVENTORY:\n${JSON.stringify({ url, title, ...exploration }, null, 2)}\n\nHIGH-VALUE ACCESSIBILITY SIGNALS:\n${compactSignals}`;

  const name = specName(url);
  const filename = `tests/${name}.spec.ts`;
  debug('page prompt prepared', `url=${url} promptChars=${prompt.length} inventoryControls=${exploration.controls.length} headings=${exploration.headings.length}`);

  // Generate -> sanitize -> write -> validate. Retry the draft, then quarantine.
  let ok = false;
  try {
    for (let attempt = 1; attempt <= 2 && !ok; attempt++) {
      const output = await callModel(`${prompt}\n\nQUALITY GATE: Generate a dynamic set of independent smoke tests from the exploration inventory and accessibility snapshot. There is no fixed test count and no fixed workflow: generate only candidates supported by distinct observed targets and observable outcomes. Every test must be page-specific: its title must name the observed control, heading, link, form, field, picker, button, or resulting state on THIS page. Import helpers exactly as: import { actionEvidence, observationEvidence } from './evidence'. Every actionEvidence call MUST use actionEvidence(page, testInfo, label, action, verify), and every observationEvidence call MUST use observationEvidence(page, testInfo, label, verify); destructure testInfo in the test signature. Any test that clicks, selects, fills, submits, navigates, switches, searches, or changes state MUST use actionEvidence, assert a meaningful postcondition for that exact target, and capture a named screenshot after that assertion. Observation tests must use observationEvidence and verify visible content/state. Do not use toBeAttached alone for behavioral tests. If exploration does not reveal a reliable postcondition, emit a skipped test titled 'NOT TESTABLE: <reason>' instead of inventing an assertion. Never use getByText, text=, or :has-text selectors.`);
      const match = output.match(/```(?:typescript|ts)?\n([\s\S]*?)```/);
      const specCode = sanitize(match ? match[1] : output);
      writeFileSync(filename, specCode);
      const validation = isValidSpec(filename);
      ok = validation.valid;
      if (!ok) console.log(`  ⚠  attempt ${attempt} produced an invalid spec — ${validation.reason.slice(0, 500)} — retrying`);
    }
  } catch (error) {
    console.log(`  ❌ ${name}: model generation failed (${error.message})`);
    if (error.body) console.log(`     server response: ${error.body}`);
    debug('page generation failed', `url=${url} message=${error.message} detail=${preview(error.body)}`);
  }

  if (ok) {
    console.log(`  ✅ ${filename}`);
  } else {
    // Never let unvalidated model output stay in the suite: one broken file aborts the whole run.
    if (existsSync(filename)) renameSync(filename, `_failed-specs/${name}.spec.ts.bad`);
    failed.push(name);
    console.log(`  ❌ ${name}: still invalid after 2 attempts — moved to _failed-specs/ for review`);
  }
}

// 4. Close browser and report
await browser.close();
console.log(`\nDone. ${urls.length - failed.length}/${urls.length} valid specs written.`);
if (failed.length) console.log('Failed pages (see _failed-specs/):', failed.join(', '));
