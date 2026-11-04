#!/usr/bin/env node
// explore.mjs - record what is really on each page you name, so tests are written from observation.
//
// Usage:  node explore.mjs --out exploration --url https://example.com/ [--url https://example.com/about ...]
//         node explore.mjs --out exploration --urls-file urls.txt [--probe 5] [--viewport 1280x800]
//
// Visits ONLY the URLs given (it never follows links). Requests to any other origin are blocked, and so is any
// redirect that leaves the origins you named. Writes <out>/<slug>.json (inventory) and <out>/<slug>.aria.txt
// (accessibility snapshot) per page, plus <out>/index.json. Needs @playwright/test in the project (npm i -D).
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const all = (name) => args.flatMap((a, i) => (a === name && args[i + 1] ? [args[i + 1]] : []));

const out = opt("--out", "exploration");
const probeMax = Number(opt("--probe", "0"));
const [vw, vh] = opt("--viewport", "1280x800").split("x").map(Number);
let urls = all("--url");
const urlsFile = opt("--urls-file", "");
if (urlsFile) {
  urls = urls.concat(
    fs.readFileSync(urlsFile, "utf8").split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#")),
  );
}
if (!urls.length) {
  console.error("Give at least one --url (or --urls-file). Nothing is visited unless you name it.");
  process.exit(2);
}
for (const u of urls) {
  if (!/^https?:\/\//i.test(u)) {
    console.error(`Not an http(s) URL: ${u}`);
    process.exit(2);
  }
}
const allowedOrigins = new Set(urls.map((u) => new URL(u).origin));

// Resolve Playwright from the PROJECT being tested, not from this script's folder.
const require = createRequire(path.join(process.cwd(), "package.json"));
let chromium;
try {
  ({ chromium } = require("@playwright/test"));
} catch {
  console.error("Cannot find @playwright/test. Run: npm i -D @playwright/test && npx playwright install chromium");
  process.exit(2);
}

const slugOf = (u) => {
  const x = new URL(u);
  return (x.hostname + x.pathname + x.search).replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80) || "page";
};

// Runs inside the page. Keep it dependency-free.
function inventoryInPage() {
  const cssPath = (el) => {
    if (el.id && !/\d{4,}|[0-9a-f]{8,}|^:r|^ember|^react/i.test(el.id)) return `#${CSS.escape(el.id)}`;
    return el.tagName.toLowerCase();
  };
  const region = (el) => {
    if (el.closest("header, nav, footer, [role=banner], [role=navigation], [role=contentinfo]")) return "chrome";
    if (el.closest("main, article, [role=main]")) return "content";
    return "other";
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const nameOf = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria.trim();
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)?.textContent?.trim() ?? "").join(" ").trim();
      if (t) return t;
    }
    if (el.labels && el.labels.length) return [...el.labels].map((l) => l.textContent.trim()).join(" ").trim();
    const alt = el.getAttribute("alt") || el.getAttribute("title") || el.getAttribute("placeholder");
    if (alt) return alt.trim();
    return (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const t = el.tagName.toLowerCase();
    if (t === "a") return el.hasAttribute("href") ? "link" : "generic";
    if (t === "button") return "button";
    if (t === "select") return "combobox";
    if (t === "textarea") return "textbox";
    if (t === "summary") return "button";
    if (t === "input") {
      const ty = (el.getAttribute("type") || "text").toLowerCase();
      if (ty === "checkbox") return "checkbox";
      if (ty === "radio") return "radio";
      if (["button", "submit", "reset", "image"].includes(ty)) return "button";
      if (ty === "range") return "slider";
      return "textbox";
    }
    return "generic";
  };

  const sel = "a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=combobox], [role=switch]";
  const controls = [];
  const counts = new Map();
  for (const el of document.querySelectorAll(sel)) {
    if (controls.length >= 150) break;
    const name = nameOf(el);
    const role = roleOf(el);
    const key = `${role}|${name}`;
    counts.set(key, (counts.get(key) || 0) + 1);
    controls.push({
      role,
      name,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("type") || undefined,
      href: el.tagName === "A" ? el.getAttribute("href") : undefined,
      id: el.id || undefined,
      volatileId: !!el.id && /\d{4,}|[0-9a-f]{8,}|^:r|^ember|^react/i.test(el.id),
      region: region(el),
      visible: visible(el),
      disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
      required: el.required === true || el.getAttribute("aria-required") === "true",
      readonly: el.readOnly === true,
      inForm: !!el.closest("form"),
      selector: cssPath(el),
      _key: key,
    });
  }
  for (const c of controls) {
    c.ambiguous = counts.get(c._key) > 1;
    delete c._key;
  }
  const headings = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading]")].slice(0, 80).map((h) => ({
    level: Number(h.tagName[1]) || Number(h.getAttribute("aria-level")) || 0,
    text: (h.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120),
    region: region(h),
    hiddenByDesign: !visible(h),
    inFeed: !!h.closest("article, [role=feed]"),
  }));
  const forms = [...document.querySelectorAll("form")].map((f) => ({
    id: f.id || undefined,
    fields: [...f.querySelectorAll("input:not([type=hidden]), select, textarea")].length,
    requiredFields: [...f.querySelectorAll("[required], [aria-required=true]")].length,
    hasSubmit: !!f.querySelector("button[type=submit], input[type=submit], button:not([type])"),
    asksPersonalData: !!f.querySelector("input[type=email], input[type=tel], input[type=password], [autocomplete*=cc-]"),
    hasCaptcha: !!f.querySelector("[class*=captcha i], [id*=captcha i], iframe[src*=recaptcha], iframe[src*=hcaptcha]"),
  }));
  return { controls, headings, forms };
}

fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
const index = [];
for (const url of urls) {
  const context = await browser.newContext({ viewport: { width: vw, height: vh } });
  const blocked = new Set();
  await context.route("**/*", (route) => {
    const origin = new URL(route.request().url()).origin;
    if (allowedOrigins.has(origin) || route.request().url().startsWith("data:")) return route.continue();
    blocked.add(origin);
    return route.abort();
  });
  const page = await context.newPage();
  const notes = [];
  page.on("pageerror", (e) => notes.push(`page error: ${String(e.message).slice(0, 200)}`));
  const slug = slugOf(url);
  try {
    const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
    const inv = await page.evaluate(inventoryInPage);
    const aria = await page.locator("body").ariaSnapshot().catch(() => "");
    const revealed = [];
    if (probeMax > 0) {
      // Opt-in: click a few buttons that are outside forms to learn what they reveal. This CAN change page state.
      const candidates = inv.controls.filter((c) => c.role === "button" && c.visible && !c.inForm && !c.disabled && c.region !== "chrome").slice(0, probeMax);
      for (const c of candidates) {
        const before = new Set((await page.evaluate(inventoryInPage)).controls.map((x) => `${x.role}|${x.name}`));
        const loc = page.getByRole("button", { name: c.name, exact: true }).first();
        try {
          await loc.click({ timeout: 3000 });
          await page.waitForTimeout(400);
          const after = (await page.evaluate(inventoryInPage)).controls;
          const added = after.filter((x) => x.visible && !before.has(`${x.role}|${x.name}`)).map((x) => `${x.role}:${x.name}`).slice(0, 8);
          revealed.push({ trigger: c.name, effect: added.length ? "reveals" : "no-visible-result", newControls: added, urlAfter: page.url() });
        } catch (e) {
          revealed.push({ trigger: c.name, effect: "not-clickable" });
        }
        await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
        await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
      }
    }
    const finalUrl = page.url();
    if (!allowedOrigins.has(new URL(finalUrl).origin)) notes.push(`redirected off-list to ${new URL(finalUrl).origin}`);
    const record = {
      requestedUrl: url,
      finalUrl,
      status: resp ? resp.status() : null,
      title: await page.title(),
      viewport: { width: vw, height: vh },
      blockedOtherOrigins: [...blocked],
      notes,
      ...inv,
      revealedByInteraction: revealed,
    };
    fs.writeFileSync(path.join(out, `${slug}.json`), JSON.stringify(record, null, 2));
    fs.writeFileSync(path.join(out, `${slug}.aria.txt`), aria);
    index.push({ url, slug, title: record.title, status: record.status, controls: inv.controls.length, headings: inv.headings.length });
    console.log(`explored ${url} -> ${slug}.json (${inv.controls.length} controls, ${inv.headings.length} headings)`);
  } catch (e) {
    index.push({ url, slug, error: String(e.message).split("\n")[0] });
    console.error(`FAILED ${url}: ${String(e.message).split("\n")[0]}`);
  }
  await context.close();
}
await browser.close();
fs.writeFileSync(path.join(out, "index.json"), JSON.stringify(index, null, 2));
