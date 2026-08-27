import { chromium } from 'playwright';
import { readFileSync, writeFileSync, appendFileSync, mkdirSync, renameSync, existsSync, rmSync } from 'node:fs';
import { resolve, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import 'dotenv/config';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)));
const pathOf = (...parts) => resolve(ROOT, ...parts);
const guide = readFileSync(pathOf('AI-TEST-GUIDE.md'), 'utf8');
const persona = readFileSync(pathOf('persona.txt'), 'utf8');
const inputFile = pathOf(process.env.URLS_FILE || 'my-crawler/urls.txt');
const outputRoot = pathOf(process.env.OUTPUT_ROOT || '.');
const testsDir = pathOf('tests'); const explorationDir = pathOf('exploration'); const failedDir = pathOf('_failed-specs');
const runId = `${Date.now()}-${process.pid}`;
const runManifest = pathOf(`generation-${runId}.manifest.json`);
const manifest = { runId, startedAt: new Date().toISOString(), urls: {} };
function saveManifest() { writeFileSync(runManifest, JSON.stringify(manifest, null, 2)); }
saveManifest();
const finiteInt = (name, fallback, min, max) => { const n = Number(process.env[name] ?? fallback); if (!Number.isInteger(n) || n < min || n > max) throw new Error(`${name} must be an integer between ${min} and ${max}`); return n; };
const MODEL_TIMEOUT_MS = finiteInt('MODEL_TIMEOUT_MS', 300000, 1000, 900000);
const MODEL_RETRIES = finiteInt('MODEL_RETRIES', 4, 0, 10);
const RETRY_BASE_MS = finiteInt('RETRY_BASE_MS', 3000, 100, 120000);
const NAV_TIMEOUT_MS = finiteInt('NAV_TIMEOUT_MS', 60000, 1000, 300000);
const LLM_PROVIDER = (process.env.LLM_PROVIDER || 'ollama').toLowerCase();
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || 'gemma4:31b';
const OLLAMA_API_URL = process.env.OLLAMA_API_URL || 'http://127.0.0.1:11434/api/chat';
const GEMINI_MODEL = process.env.GEMINI_MODEL || 'gemini-2.0-flash';
const GEMINI_API_URL = process.env.GEMINI_API_URL || `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`;
const GEMINI_API_KEY = process.env.GEMINI_API_KEY;
const DEBUG_LOG = pathOf('generation-debug.log'); let requestNumber = 0;
writeFileSync(DEBUG_LOG, `Generation ${runId} started ${new Date().toISOString()}\n`);
function debug(message, details='') { const line=`[${new Date().toISOString()}] ${message}${details?` | ${details}`:''}`; console.log(`  ${line}`); appendFileSync(DEBUG_LOG,line+'\n'); }
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
function isTransientStatus(s){return [408,409,425,429,500,502,503,504,524].includes(s);}
function canonicalUrl(raw){const u=new URL(raw); if(!['http:','https:'].includes(u.protocol)) throw new Error(`Unsupported URL protocol: ${u.protocol}`); u.hash=''; if(u.pathname.length>1)u.pathname=u.pathname.replace(/\/+$/,''); return u.toString();}
function specName(raw){const u=new URL(raw); const stem=`${u.hostname}${u.pathname||'home'}`.replace(/[^a-z0-9]+/gi,'-').replace(/^-|-$/g,'').toLowerCase()||'page'; return `${stem}-${createHash('sha256').update(u.toString()).digest('hex').slice(0,10)}`;}
function safePath(root, name){const p=resolve(root,name); if(!['','..'].every(x=>!relative(root,p).startsWith(x))) throw new Error('Unsafe output path'); return p;}
async function callModel(prompt){
 const system='You are a test spec generator. Return exactly one complete TypeScript code block and no prose.';
 for(let attempt=0;attempt<=MODEL_RETRIES;attempt++){const id=++requestNumber,started=Date.now(),controller=new AbortController(),timer=setTimeout(()=>controller.abort(),MODEL_TIMEOUT_MS); try{
  let endpoint,headers={'Content-Type':'application/json'},requestBody;
  if(LLM_PROVIDER==='ollama'){
   endpoint=OLLAMA_API_URL; requestBody={model:OLLAMA_MODEL,messages:[{role:'system',content:system},{role:'user',content:prompt}],stream:false,options:{temperature:0.1,num_predict:3072}};
  } else if(LLM_PROVIDER==='gemini'){
   if(!GEMINI_API_KEY) throw new Error('GEMINI_API_KEY is required when LLM_PROVIDER=gemini'); endpoint=GEMINI_API_URL; headers['x-goog-api-key']=GEMINI_API_KEY; requestBody={systemInstruction:{parts:[{text:system}]},contents:[{role:'user',parts:[{text:prompt}]}],generationConfig:{temperature:0.1,maxOutputTokens:3072,responseMimeType:'text/plain'}};
  } else throw new Error(`Unsupported LLM_PROVIDER: ${LLM_PROVIDER}`);
  const resp=await fetch(endpoint,{method:'POST',headers,body:JSON.stringify(requestBody),signal:controller.signal});const raw=await resp.text();debug(`request #${id}`,`provider=${LLM_PROVIDER} model=${LLM_PROVIDER==='ollama'?OLLAMA_MODEL:GEMINI_MODEL} attempt=${attempt+1} status=${resp.status} ms=${Date.now()-started}`);
  if(!resp.ok){const e=new Error(`${LLM_PROVIDER} API error: ${resp.status}`);e.status=resp.status;e.body=raw.slice(0,1000);if(!isTransientStatus(resp.status)||attempt===MODEL_RETRIES)throw e;const ra=Number(resp.headers.get('retry-after'));await sleep(Number.isFinite(ra)?ra*1000:RETRY_BASE_MS*2**attempt);continue;}
  let data;try{data=JSON.parse(raw)}catch{const e=new Error(`${LLM_PROVIDER} returned non-JSON success response`);e.transient=true;e.body=raw.slice(0,1000);throw e;}
  let content=LLM_PROVIDER==='ollama'?data.message?.content:data.candidates?.[0]?.content?.parts?.map(part=>part.text||'').join('');
  if(typeof content!=='string'||!content.trim())throw new Error(`${LLM_PROVIDER} response had no usable content`);
  if(LLM_PROVIDER==='gemini'&&data.candidates?.[0]?.finishReason==='MAX_TOKENS')throw new Error('Gemini output was truncated');
  if(LLM_PROVIDER==='ollama'&&data.done_reason==='length')throw new Error('Ollama output was truncated'); return content;
 }catch(e){const transient=e.transient||e.name==='AbortError'||e.code==='ECONNRESET'||isTransientStatus(e.status);debug(`request #${id} failed`,`attempt=${attempt+1} message=${e.message}`);if(!transient||attempt===MODEL_RETRIES)throw e;await sleep(RETRY_BASE_MS*2**attempt);}finally{clearTimeout(timer)}}
}
async function explorePage(page){const controls=await page.locator('a,button,input,select,textarea,[role]').evaluateAll(es=>es.filter(e=>{const s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&e.getClientRects().length}).map(e=>({tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),accessibleName:e.getAttribute('aria-label')||e.textContent?.trim().replace(/\s+/g,' ').slice(0,160),href:e.getAttribute('href'),type:e.getAttribute('type'),value:e instanceof HTMLInputElement||e instanceof HTMLSelectElement?e.value:undefined,disabled:e.hasAttribute('disabled')})).slice(0,120));const headings=await page.locator('h1,h2,h3,[role="heading"]').evaluateAll(es=>es.filter(e=>e.getClientRects().length).map(e=>({level:e.tagName.match(/H([1-3])/i)?.[1]||e.getAttribute('aria-level'),text:e.textContent?.trim().replace(/\s+/g,' ').slice(0,180)})));return{headings,controls};}
function compactAccessibilitySignals(s){return s.split(/\r?\n/).filter(x=>/heading|button|link|textbox|combobox|listbox|checkbox|radio|tab|form/i.test(x)).slice(0,180).join('\n').slice(0,9000);}
function extractCode(output){const m=output.match(/```(?:typescript|ts)?\s*\n([\s\S]*?)```/); if(!m)throw new Error('Model did not return a TypeScript code block'); const code=m[1].trim()+'\n'; if(!/\bexpect\s*\(/.test(code))throw new Error('Generated spec has no assertion'); return code;}
function validate(file){execFileSync(process.platform==='win32'?'npx.cmd':'npx',['playwright','test',file,'--list'],{cwd:ROOT,stdio:'pipe',maxBuffer:2*1024*1024});execFileSync(process.platform==='win32'?'node.exe':'node',['scripts/validate_specs.mjs',file],{cwd:ROOT,stdio:'pipe',maxBuffer:2*1024*1024});}
mkdirSync(testsDir,{recursive:true});mkdirSync(explorationDir,{recursive:true});mkdirSync(failedDir,{recursive:true});
const raw=readFileSync(inputFile,'utf8').split(/\r?\n/); const urls=[...new Set(raw.map(x=>x.trim()).filter(x=>x&&!x.startsWith('#')).map(canonicalUrl))]; if(!urls.length)throw new Error(`No valid URLs in ${inputFile}`);
const browser=await chromium.launch({headless:process.env.HEADLESS!=='false'}); const failed=[];
try{for(const url of urls){const name=specName(url),stage=pathOf(`.generated-${runId}-${name}.spec.ts`);console.log(`\nProcessing: ${url}`); manifest.urls[url] = { status: 'processing', startedAt: new Date().toISOString() }; saveManifest(); let context;try{context=await browser.newContext();const page=await context.newPage();page.setDefaultNavigationTimeout(NAV_TIMEOUT_MS);await page.goto(url,{waitUntil:'domcontentloaded'});await page.waitForLoadState('networkidle',{timeout:5000}).catch(()=>{});let snapshot='';try{snapshot=await page.locator('body').ariaSnapshot()}catch(e){debug('accessibility snapshot unavailable',`url=${url} message=${e.message}`)}const title=await page.title();const exploration=await explorePage(page);writeFileSync(pathOf('exploration',`${name}.aria-snapshot.txt`),snapshot);writeFileSync(pathOf('exploration',`${name}.inventory.json`),JSON.stringify({url,title,...exploration},null,2));const prompt=`${guide}\n\nPERSONA:\n${persona}\n\nPAGE URL: ${url}\nPAGE TITLE: ${title}\n\nCOMPACT PAGE INVENTORY:\n${JSON.stringify({url,title,...exploration},null,2)}\n\nHIGH-VALUE ACCESSIBILITY SIGNALS:\n${compactAccessibilitySignals(snapshot)}\n\nQUALITY GATE: Generate only complete independent tests supported by observed targets. Import evidence helpers exactly from './evidence'. Use actionEvidence(page, testInfo, label, action, verify) and observationEvidence(page, testInfo, label, verify). Destructure testInfo. Assert meaningful postconditions. Use no getByText, text=, or :has-text selectors.`;let ok=false,last='';for(let attempt=1;attempt<=2&&!ok;attempt++){try{const code=extractCode(await callModel(prompt));writeFileSync(stage,code);validate(stage);renameSync(stage,pathOf('tests',`${name}.spec.ts`));ok=true; manifest.urls[url] = { status: 'generated', spec: `tests/${name}.spec.ts`, completedAt: new Date().toISOString() }; saveManifest(); console.log(`  ✅ tests/${name}.spec.ts`)}catch(e){last=e.message;debug('draft rejected',`url=${url} attempt=${attempt} reason=${last}`);if(existsSync(stage))rmSync(stage,{force:true})}}if(!ok){manifest.urls[url] = { status: 'failed', error: last, completedAt: new Date().toISOString() }; saveManifest(); failed.push(name);writeFileSync(pathOf('_failed-specs',`${name}-${runId}.txt`),last);console.log(`  ❌ ${name}: ${last}`)} }catch(e){manifest.urls[url] = { status: 'failed', error: e.message, completedAt: new Date().toISOString() }; saveManifest(); failed.push(url);console.log(`  ❌ ${url}: ${e.message}`)}finally{await context?.close()}}}finally{await browser.close()}
manifest.finishedAt = new Date().toISOString(); manifest.summary = { total: urls.length, failed: failed.length, generated: urls.length - failed.length }; saveManifest(); console.log(`\nDone. ${urls.length-failed.length}/${urls.length} valid specs written.`);if(failed.length)console.log('Failed pages:',failed.join(', '));
