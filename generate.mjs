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
const finiteInt = (name, fallback, min, max) => { const n = Number(process.env[name] ?? fallback); if (!Number.isInteger(n) || n < min || n > max) throw new Error(`${name} must be an integer between ${min} and ${max}`); return n; };
const MODEL_TIMEOUT_MS = finiteInt('MODEL_TIMEOUT_MS', 300000, 1000, 900000);
const MODEL_RETRIES = finiteInt('MODEL_RETRIES', 4, 0, 10);
const RETRY_BASE_MS = finiteInt('RETRY_BASE_MS', 3000, 100, 120000);
const NAV_TIMEOUT_MS = finiteInt('NAV_TIMEOUT_MS', 60000, 1000, 300000);
const API_URL = process.env.API_URL || 'https://llm-1.d4done.com/v1/chat/completions';
const MODEL_NAME = process.env.MODEL_NAME || 'google/gemma-4-26b-a4b-qat';
const API_KEY = process.env.API_KEY;
const DEBUG_LOG = pathOf('generation-debug.log'); let requestNumber = 0;
writeFileSync(DEBUG_LOG, `Generation ${runId} started ${new Date().toISOString()}\n`);
function debug(message, details='') { const line=`[${new Date().toISOString()}] ${message}${details?` | ${details}`:''}`; console.log(`  ${line}`); appendFileSync(DEBUG_LOG,line+'\n'); }
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
function isTransientStatus(s){return [408,409,425,429,500,502,503,504,524].includes(s);}
function canonicalUrl(raw){const u=new URL(raw); if(!['http:','https:'].includes(u.protocol)) throw new Error(`Unsupported URL protocol: ${u.protocol}`); u.hash=''; if(u.pathname.length>1)u.pathname=u.pathname.replace(/\/+$/,''); return u.toString();}
function specName(raw){const u=new URL(raw); const stem=`${u.hostname}${u.pathname||'home'}`.replace(/[^a-z0-9]+/gi,'-').replace(/^-|-$/g,'').toLowerCase()||'page'; return `${stem}-${createHash('sha256').update(u.toString()).digest('hex').slice(0,10)}`;}
function safePath(root, name){const p=resolve(root,name); if(!['','..'].every(x=>!relative(root,p).startsWith(x))) throw new Error('Unsafe output path'); return p;}
async function callModel(prompt){
 if(!API_KEY) throw new Error('API_KEY is required for the configured model endpoint');
 const messages=[{role:'system',content:'You are a test spec generator. Return exactly one complete TypeScript code block and no prose.'},{role:'user',content:prompt}];
 for(let attempt=0;attempt<=MODEL_RETRIES;attempt++){const id=++requestNumber,started=Date.now(),controller=new AbortController(),timer=setTimeout(()=>controller.abort(),MODEL_TIMEOUT_MS); try{
  const resp=await fetch(API_URL,{method:'POST',headers:{'Content-Type':'application/json','Authorization':`Bearer ${API_KEY}`},body:JSON.stringify({model:MODEL_NAME,messages,temperature:0.1,max_tokens:3072,stream:false}),signal:controller.signal}); const body=await resp.text(); debug(`request #${id}`,`attempt=${attempt+1} status=${resp.status} ms=${Date.now()-started}`);
  if(!resp.ok){const e=new Error(`API error: ${resp.status}`);e.status=resp.status;e.body=body.slice(0,1000);if(!isTransientStatus(resp.status)||attempt===MODEL_RETRIES)throw e; const ra=Number(resp.headers.get('retry-after')); await sleep(Number.isFinite(ra)?ra*1000:RETRY_BASE_MS*2**attempt);continue;}
  let data;try{data=JSON.parse(body)}catch{const e=new Error('API returned non-JSON success response');e.transient=true;throw e;}
  if(data.error) throw new Error(`Model error: ${JSON.stringify(data.error).slice(0,500)}`);
  let content=data.choices?.[0]?.message?.content; if(Array.isArray(content))content=content.map(x=>typeof x==='string'?x:x.text||'').join('');
  if(typeof content!=='string'||!content.trim())throw new Error('Model response had no usable content');
  if(data.choices?.[0]?.finish_reason==='length')throw new Error('Model output was truncated'); return content;
 }catch(e){const transient=e.transient||e.name==='AbortError'||e.code==='ECONNRESET'||isTransientStatus(e.status);debug(`request #${id} failed`,`attempt=${attempt+1} message=${e.message}`);if(!transient||attempt===MODEL_RETRIES)throw e;await sleep(RETRY_BASE_MS*2**attempt);}finally{clearTimeout(timer)}}
}
async function explorePage(page){const controls=await page.locator('a,button,input,select,textarea,[role]').evaluateAll(es=>es.filter(e=>{const s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&e.getClientRects().length}).map(e=>({tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),accessibleName:e.getAttribute('aria-label')||e.textContent?.trim().replace(/\s+/g,' ').slice(0,160),href:e.getAttribute('href'),type:e.getAttribute('type'),value:e instanceof HTMLInputElement||e instanceof HTMLSelectElement?e.value:undefined,disabled:e.hasAttribute('disabled')})).slice(0,120));const headings=await page.locator('h1,h2,h3,[role="heading"]').evaluateAll(es=>es.filter(e=>e.getClientRects().length).map(e=>({level:e.tagName.match(/H([1-3])/i)?.[1]||e.getAttribute('aria-level'),text:e.textContent?.trim().replace(/\s+/g,' ').slice(0,180)})));return{headings,controls};}
function compactAccessibilitySignals(s){return s.split(/\r?\n/).filter(x=>/heading|button|link|textbox|combobox|listbox|checkbox|radio|tab|form/i.test(x)).slice(0,180).join('\n').slice(0,9000);}
function extractCode(output){const m=output.match(/```(?:typescript|ts)?\s*\n([\s\S]*?)```/); if(!m)throw new Error('Model did not return a TypeScript code block'); const code=m[1].trim()+'\n'; if(!/\bexpect\s*\(/.test(code))throw new Error('Generated spec has no assertion'); return code;}
function validate(file){execFileSync(process.platform==='win32'?'npx.cmd':'npx',['playwright','test',file,'--list'],{cwd:ROOT,stdio:'pipe',maxBuffer:2*1024*1024});execFileSync(process.platform==='win32'?'node.exe':'node',['scripts/validate_specs.mjs',file],{cwd:ROOT,stdio:'pipe',maxBuffer:2*1024*1024});}
mkdirSync(testsDir,{recursive:true});mkdirSync(explorationDir,{recursive:true});mkdirSync(failedDir,{recursive:true});
const raw=readFileSync(inputFile,'utf8').split(/\r?\n/); const urls=[...new Set(raw.map(x=>x.trim()).filter(x=>x&&!x.startsWith('#')).map(canonicalUrl))]; if(!urls.length)throw new Error(`No valid URLs in ${inputFile}`);
const browser=await chromium.launch({headless:process.env.HEADLESS!=='false'}); const failed=[];
try{for(const url of urls){const name=specName(url),stage=pathOf(`.generated-${runId}-${name}.spec.ts`);console.log(`\nProcessing: ${url}`);let context;try{context=await browser.newContext();const page=await context.newPage();page.setDefaultNavigationTimeout(NAV_TIMEOUT_MS);await page.goto(url,{waitUntil:'domcontentloaded'});await page.waitForLoadState('networkidle',{timeout:5000}).catch(()=>{});let snapshot='';try{snapshot=await page.locator('body').ariaSnapshot()}catch(e){debug('accessibility snapshot unavailable',`url=${url} message=${e.message}`)}const title=await page.title();const exploration=await explorePage(page);writeFileSync(pathOf('exploration',`${name}.aria-snapshot.txt`),snapshot);writeFileSync(pathOf('exploration',`${name}.inventory.json`),JSON.stringify({url,title,...exploration},null,2));const prompt=`${guide}\n\nPERSONA:\n${persona}\n\nPAGE URL: ${url}\nPAGE TITLE: ${title}\n\nCOMPACT PAGE INVENTORY:\n${JSON.stringify({url,title,...exploration},null,2)}\n\nHIGH-VALUE ACCESSIBILITY SIGNALS:\n${compactAccessibilitySignals(snapshot)}\n\nQUALITY GATE: Generate only complete independent tests supported by observed targets. Import evidence helpers exactly from './evidence'. Use actionEvidence(page, testInfo, label, action, verify) and observationEvidence(page, testInfo, label, verify). Destructure testInfo. Assert meaningful postconditions. Use no getByText, text=, or :has-text selectors.`;let ok=false,last='';for(let attempt=1;attempt<=2&&!ok;attempt++){try{const code=extractCode(await callModel(prompt));writeFileSync(stage,code);validate(stage);renameSync(stage,pathOf('tests',`${name}.spec.ts`));ok=true;console.log(`  ✅ tests/${name}.spec.ts`)}catch(e){last=e.message;debug('draft rejected',`url=${url} attempt=${attempt} reason=${last}`);if(existsSync(stage))rmSync(stage,{force:true})}}if(!ok){failed.push(name);writeFileSync(pathOf('_failed-specs',`${name}-${runId}.txt`),last);console.log(`  ❌ ${name}: ${last}`)} }catch(e){failed.push(url);console.log(`  ❌ ${url}: ${e.message}`)}finally{await context?.close()}}}finally{await browser.close()}
console.log(`\nDone. ${urls.length-failed.length}/${urls.length} valid specs written.`);if(failed.length)console.log('Failed pages:',failed.join(', '));
