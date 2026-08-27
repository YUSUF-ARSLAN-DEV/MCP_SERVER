import { PlaywrightCrawler } from 'crawlee';
import { writeFileSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import "dotenv/config";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const outputFile = resolve(process.env.CRAWL_OUTPUT || resolve(ROOT, 'my-crawler/urls.txt'));
const failureFile = resolve(process.env.CRAWL_FAILURES || resolve(ROOT, 'my-crawler/crawl-failures.json'));
const startUrls = (process.env.START_URLS || 'https://sat.aljazeera.net/en').split(',').map(x => x.trim()).filter(Boolean);
const allowedOrigins = new Set((process.env.ALLOWED_ORIGINS || startUrls.map(x => new URL(x).origin).join(',')).split(',').map(x => x.trim()).filter(Boolean));
const maxRequests = Number.parseInt(process.env.MAX_REQUESTS_PER_CRAWL || '100', 10);

function canonicalize(raw) {
  const u = new URL(raw);
  if (!['http:', 'https:'].includes(u.protocol) || !allowedOrigins.has(u.origin)) return null;
  u.hash = '';
  if (u.pathname.length > 1) u.pathname = u.pathname.replace(/\/+$/, '');
  return u.toString();
}
const visited = new Set(); const failures = [];
const crawler = new PlaywrightCrawler({
  maxRequestsPerCrawl: Number.isFinite(maxRequests) && maxRequests > 0 ? maxRequests : 100,
  maxRequestRetries: Number.parseInt(process.env.MAX_REQUEST_RETRIES || '2', 10),
  requestHandlerTimeoutSecs: Number.parseInt(process.env.REQUEST_TIMEOUT_SECS || '60', 10),
  headless: process.env.HEADLESS !== 'false',
  async requestHandler({ request, page, enqueueLinks, log }) {
    const url = canonicalize(request.loadedUrl || request.url);
    if (!url) return;
    visited.add(url); log.info(`Crawled: ${url}`);
    await enqueueLinks({ selector: 'a', strategy: 'same-domain', transformRequestFunction: req => { const c = canonicalize(req.url); return c ? { ...req, url: c } : false; } });
  },
  failedRequestHandler({ request, log }) {
    const failure = { url: request.url, errorMessages: request.errorMessages || [] }; failures.push(failure); log.error(`Failed: ${request.url}`);
  },
});
try { await crawler.run(startUrls.map(canonicalize).filter(Boolean)); } finally {
  mkdirSync(dirname(outputFile), { recursive: true });
  writeFileSync(outputFile, [...visited].sort().join('\n') + (visited.size ? '\n' : ''));
  writeFileSync(failureFile, JSON.stringify({ generatedAt: new Date().toISOString(), urls: [...visited], failures }, null, 2));
}
console.log(`Saved ${visited.size} URLs to ${outputFile}; failures: ${failures.length}`);
if (failures.length && process.env.FAIL_ON_CRAWL_FAILURES === 'true') process.exitCode = 1;
