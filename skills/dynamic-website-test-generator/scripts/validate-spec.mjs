#!/usr/bin/env node
// validate-spec.mjs - mechanical checks on a generated Playwright TypeScript spec, before it is ever run.
//
// Usage:  node validate-spec.mjs --urls urls.txt tests/home.spec.ts [more.spec.ts ...]
//         node validate-spec.mjs --url https://example.com/ tests/home.spec.ts
//         add --strict-evidence to require actionEvidence(...) around EVERY state-changing action
// Exit code 0 = every spec passed, 1 = at least one rule failed (each failure is printed with a fix hint).
// Plain text pattern checks on purpose: no parser, no dependencies, nothing executed.
//
// Evidence rule: by default setup steps (typing, ticking, navigating before the first screenshot) may be bare, but
// the action under test and every action after the first screenshot must go through actionEvidence(...).
import fs from "node:fs";

const args = process.argv.slice(2);
const urls = [];
const files = [];
let strictEvidence = false;
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--strict-evidence") strictEvidence = true;
  else if (args[i] === "--url") urls.push(args[++i]);
  else if (args[i] === "--urls") {
    urls.push(...fs.readFileSync(args[++i], "utf8").split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#")));
  } else files.push(args[i]);
}
if (!files.length) {
  console.error("Usage: node validate-spec.mjs [--url URL | --urls file] <spec.ts> ...");
  process.exit(2);
}
const origins = new Set(urls.map((u) => new URL(u).origin));

// Strip comments so a rule is not tripped (or dodged) by commented-out code.
const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

// Split into test blocks: everything from `test(` to the next `test(` at line start.
function testBlocks(src) {
  const re = /^[ \t]*test(?:\.\w+)?\(\s*(['"`])(.+?)\1/gm;
  const starts = [...src.matchAll(re)].map((m) => ({ title: m[2], at: m.index }));
  return starts.map((s, i) => ({ title: s.title, body: src.slice(s.at, i + 1 < starts.length ? starts[i + 1].at : src.length) }));
}


// Position and text of each `name(...)` call, parentheses balanced (parentheses inside strings are rare in specs).
function callSpans(body, name) {
  const spans = [];
  const re = new RegExp("\\b" + name + "\\s*\\(", "g");
  let m;
  while ((m = re.exec(body))) {
    let depth = 1, i = m.index + m[0].length;
    while (i < body.length && depth > 0) {
      if (body[i] === "(") depth++;
      else if (body[i] === ")") depth--;
      i++;
    }
    spans.push({ start: m.index, end: i, text: body.slice(m.index, i) });
  }
  return spans;
}

const GENERIC_NAME = /^(works?|test\d*|navigation|displays? correctly|basic|smoke|page loads?)$/i;
let failed = 0;

for (const file of files) {
  const raw = fs.readFileSync(file, "utf8");
  const src = stripComments(raw);
  const problems = [];
  const add = (rule, hint) => problems.push(`${rule} - ${hint}`);

  const blocks = testBlocks(src);
  if (!blocks.length) add("no tests found", "the file must contain test('...', async ({ page }) => { ... })");

  if (!/\bexpect\s*\(/.test(src)) add("no assertion", "at least one expect(...) is required; a test that only clicks proves nothing");

  // Safety and portability: a DEFENSIVE DENYLIST. These patterns are here to REJECT unsafe generated specs
  // (code that runs processes, evaluates strings as code, makes its own network calls, reads secrets from the
  // environment, or deletes/writes files). Nothing below is executed or used by this skill; the words appear only
  // because the validator has to name what it refuses.
  for (const [re, why] of [
    [/from\s+['"](?:node:)?(?:child_process|net|dgram|http|https|http2|tls|os|vm|worker_threads|cluster)['"]/, "system/network module import"],
    [/require\(\s*['"](?:node:)?(?:child_process|net|dgram|http|https|http2|tls|os|vm|worker_threads|cluster)['"]\s*\)/, "system/network module require"],
    [/\bchild_process\b|\bexecSync\b|\bspawnSync?\b/, "process execution"],
    [/\beval\s*\(|new\s+Function\s*\(/, "dynamic code execution"],
    [/\bfetch\s*\(|XMLHttpRequest|WebSocket\s*\(/, "direct network call (use the page, not code, to talk to the site)"],
    [/process\.env\.(?!EVIDENCE_DIR\b)\w*(?:KEY|TOKEN|SECRET|PASSWORD)/i, "reads a secret from the environment"],
    [/\bfs\.(?:rm|rmdir|unlink|writeFile|appendFile)\w*\(/, "file deletion/writing from a spec (evidence goes through evidence.ts)"],
  ]) {
    if (re.test(src)) add(`forbidden: ${why}`, "remove it; specs only drive the page and assert");
  }

  // Navigation stays on the URLs the user named.
  const gotos = [...src.matchAll(/\.goto\(\s*(['"`])(https?:\/\/[^'"`]+)\1/g)].map((m) => m[2]);
  for (const g of gotos) {
    let o = "";
    try { o = new URL(g).origin; } catch { /* handled below */ }
    if (origins.size && !origins.has(o)) add(`navigates to ${g}`, "only URLs the user explicitly provided may be visited");
  }
  if (origins.size) {
    const mentioned = urls.some((u) => src.includes(u) || src.includes(u.replace(/\/$/, "")));
    if (!mentioned) add("target URL missing", `the exact page URL (${urls.join(", ")}) must appear literally in the spec`);
  }

  // Selector stability.
  if (/\bgetByText\s*\(|:has-text\(|['"`]text=/.test(src)) add("text selector", "locate by role/name, label, placeholder or test id; page copy changes");
  if (/getByRole\([^)]*\bname\s*:[^)]*\)(?![^;]*exact)/.test(src.replace(/\{[^{}]*exact\s*:\s*true[^{}]*\}/g, "{exact}"))) {
    // Cheap heuristic: a getByRole with a name must say exact: true.
    const loose = [...src.matchAll(/getByRole\(\s*(['"`])\w+\1\s*,\s*\{([^}]*)\}/g)].filter((m) => /\bname\s*:/.test(m[2]) && !/exact\s*:\s*true/.test(m[2]));
    if (loose.length) add(`getByRole with a name but no exact: true (${loose.length}x)`, "add { exact: true } so a partial name cannot match several elements");
  }
  if (/\bwaitForTimeout\s*\(/.test(src)) add("fixed sleep (waitForTimeout)", "wait on an expect(...) or a locator instead");
  if (/toHaveURL\(\s*(['"`])https?:\/\//.test(src)) add("exact URL assertion", "use a regex such as /\\/path/ ; sites redirect and add locale or trailing slashes");
  if (/\.(?:toBeDisabled|toBeEnabled)\(/.test(src)) add("disabled/enabled assertion", "only keep it if the exploration inventory shows that state");
  if (/\.nth\(\s*\d+\s*\)|\.locator\(\s*['"`]\/\/|\.locator\(\s*['"`]xpath/.test(src)) add("positional/XPath selector", "use role, label or a stable attribute");

  // Per-test rules.
  const seen = new Set();
  for (const b of blocks) {
    const where = `test "${b.title}"`;
    if (GENERIC_NAME.test(b.title.trim())) add(`${where}: generic name`, "name the behaviour and the page-specific target");
    if (seen.has(b.title)) add(`${where}: duplicate title`, "titles must be unique");
    seen.add(b.title);
    const skipped = /test\.(?:skip|fixme)\(/.test(b.body);
    if (!skipped && !/\b(?:actionEvidence|observationEvidence)\s*\(/.test(b.body)) add(`${where}: no evidence call`, "capture at least one screenshot via evidenceDir + actionEvidence/observationEvidence");
    if (/\bevidenceDir\s*\(/.test(b.body) === false && /\b(?:actionEvidence|observationEvidence)\s*\(/.test(b.body)) add(`${where}: evidence without evidenceDir`, "const dir = evidenceDir(testInfo)");
    // State-changing actions that are not inside an actionEvidence(...) call ("bare" actions).
    const spans = callSpans(b.body, "actionEvidence");
    const firstEvidenceAt = Math.min(
      ...callSpans(b.body, "actionEvidence").concat(callSpans(b.body, "observationEvidence")).map((s) => s.start),
      Infinity,
    );
    const bare = [...b.body.matchAll(/\.(?:click|fill|selectOption|check|uncheck|press|dblclick|setInputFiles|dragTo)\(/g)]
      .filter((m) => !spans.some((s) => m.index >= s.start && m.index < s.end));
    const lineOf = (idx) => b.body.slice(0, idx).split("\n").length;
    if (strictEvidence) {
      for (const m of bare) add(`${where}: bare action on line ${lineOf(m.index)}`, "strict evidence: wrap every state-changing action in actionEvidence(...)");
    } else if (bare.length) {
      // Default: setup before the first evidence call is free. The action under test, and anything after the first
      // screenshot (a later submit or click that is asserted), must go through actionEvidence.
      if (!spans.length) add(`${where}: the key action is not evidenced`, "wrap the action under test (the click or submit you assert on) in actionEvidence(...); setup steps may stay bare");
      for (const m of bare) {
        if (m.index > firstEvidenceAt) add(`${where}: bare action on line ${lineOf(m.index)} after evidence started`, "a click or submit that you assert on belongs in actionEvidence(...); only setup before the first evidence call may stay bare");
      }
    }
    // verify callback must assert: look at the whole call, with balanced parentheses
    for (const s of spans) {
      if (!/expect\s*\(/.test(s.text)) add(`${where}: actionEvidence verify has no expect(...)`, "the assertion must be inside the verify callback");
    }
    if (/observationEvidence\s*\(\s*page\s*,\s*['"`][^'"`]+['"`]\s*,\s*(?:async\s*)?\(\)\s*=>\s*(?:\{\s*\}|undefined|null)\s*,/.test(b.body)) add(`${where}: empty verify`, "observationEvidence needs a real expect(...)");
  }

  if (problems.length) {
    failed++;
    console.log(`FAIL ${file}`);
    for (const p of problems) console.log(`  - ${p}`);
  } else {
    console.log(`ok   ${file} (${blocks.length} test${blocks.length === 1 ? "" : "s"})`);
  }
}
process.exit(failed ? 1 : 0);
