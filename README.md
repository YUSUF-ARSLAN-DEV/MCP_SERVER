# Dynamic Website Test Generator and Evidence Suite

This repository explores a URL with Playwright, supplies compact page-specific
signals to a configurable OpenAI-compatible model, validates generated tests,
executes reviewed smoke tests, and produces screenshot evidence in JSON and
Word formats. Generated output is intentionally ignored by Git.

## Safe setup

Requirements: Node.js 20+, Python 3.10+, and Playwright Chromium.

```powershell
npm install
npx playwright install chromium
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and provide API_KEY locally
```

Never commit `.env`, API keys, reports, screenshots, traces, crawler storage,
or generated specifications. The model endpoint is an external dependency and
may return rate limits, Cloudflare 524/530 errors, malformed responses, or
timeouts. The generator retries transient failures and writes diagnostics to
`generation-debug.log`.

## Commands

```powershell
npm run check                 # syntax and generated-spec validation
npm run generate              # explore URLs and generate validated drafts
npm run test:smoke            # run the executable smoke suite and HTML report
npm run report                # open the Playwright report
npm run clean:artifacts       # remove local generated output
```

Set URLs in `my-crawler/urls.txt`. Set `API_URL`, `MODEL_NAME`, `MODEL_TIMEOUT_MS`,
`MODEL_RETRIES`, and `RETRY_BASE_MS` in `.env` as needed. Treat AI-generated
tests as reviewed drafts: validation checks syntax and unsafe patterns, while
human review remains necessary for behavioral accuracy.

Automated end-to-end tests for the Al Jazeera Satellite Frequencies site
(`https://sat.aljazeera.net`), covering the English site and its Arabic (`/ar`)
mirror — 12 pages total. Built with [Playwright](https://playwright.dev/)
(`@playwright/test`), Node ≥ 20.

**Status:** 399 passing · 0 failing · 0 flaky · 2 documented skips · Chromium only.

---

## Architecture — three layers, ordered by how much you can trust them

The suite is deliberately split into three layers. The first two are pure code
and can be trusted completely; the third is AI-assisted and is treated as
*reviewed drafts*, never as ground truth.

**1. Deterministic crawler — `crawl.mjs`** (no AI, trustworthy)
BFS over same-site links, dedupes by the pre-`#` key, caps at 100 pages, logs
load failures as findings. Produces `urls.txt` and `crawl-report.json`.

**2. Mechanical health checks — `tests/health.spec.ts`** (no AI, trustworthy)
Per URL: HTTP status < 400, no broken images, alt-text present. Pure observation.

**3. Behavioral tests — `tests/*.spec.ts`** (AI-generated, reviewed drafts)
One spec per page, generated from `AI-TEST-GUIDE.md`. This is the only fuzzy
layer — the only one that can hallucinate — so every spec is human-reviewed.

## Core principle: `is` vs `should`

An AI can observe what **is** on a page (an element exists, is visible, is named
X). It cannot know what **should** be there — the intended behavior lives in the
developers' heads or a spec, neither of which we have here. So the behavioral
tests are **is-checks, smoke tests, and change-detectors**, not behavior
specifications. When a pattern is wrong, fix the **generator** (`AI-TEST-GUIDE.md`)
and regenerate — don't hand-patch each site's output.

## What "399 green" actually means (read this before trusting it)

Green means **none of our assertions are currently false**, verified against the
live DOM — not that the site is proven correct. Concretely:

- **Strong assertions** (exact accessible names, `#id` selectors, `columnheader`
  roles, `toHaveCount`) *would* fail if the site renamed a heading, dropped the
  search dropdown, or broke the frequencies table. Real regression protection.
- **Smoke tests** (`count >= 1`, non-empty body) catch a page that renders blank
  or errors out. Shallow, but real.
- **No always-true assertions remain** — every test can fail for some real reason.
- **2 skips** are documented inline (Arabic tune-widget *click* flows that live in
  a hidden tab and assert no outcome — kept as `test.skip` with the reason in the
  title rather than faked green).

## Running the suite

```bash
npm install
npx playwright install chromium          # first time only

npx playwright test --reporter=list,html # run + write the HTML report
npx playwright show-report               # open the visual dashboard
```

Run one file or one test:

```bash
npx playwright test tests/en.spec.ts             # one file
npx playwright test tests/en.spec.ts:44          # one test (by line)
npx playwright test -g "select your location"    # by title
```

> Note: `--reporter=list` alone prints counts to the terminal but does **not**
> write the HTML report. Use `--reporter=list,html` to get both.

## Project layout

| Path | What it is |
|------|-----------|
| `crawl.mjs`, `collect-urls.mjs` | Crawler → `urls.txt`, `crawl-report.json` |
| `tests/health.spec.ts` | Layer 2 — mechanical health checks |
| `tests/{en,ar,…}.spec.ts` | Layer 3 — per-page behavioral specs |
| `AI-TEST-GUIDE.md` | The reusable rulebook a model reads to generate specs |
| `login.mjs` | Optional SSO/session capture → `.auth/state.json` (not wired in) |
| `playwright.config.ts` | Chromium-only; screenshot/video/trace on failure |

## `AI-TEST-GUIDE.md` — the reusable asset

This is the part worth keeping. It's the rulebook the model reads to generate
specs, and it encodes the locator lessons this project paid for:

- Target an element by its **exact accessible name, copied verbatim from the page
  snapshot** — never an OR-regex or a single shared word (they match sibling
  elements and cause strict-mode violations).
- The site has **no `<main>` landmark** — use `#main-content` (the skip-link
  target). Header/footer exist but the header is empty, so assert `toBeAttached`,
  not `toBeVisible`.
- The mobile nav toggle is `button.navbar-toggle`, `display:none` at desktop width
  (so it's absent from the accessibility tree — use a CSS locator + `toBeAttached`).
- Table columns are `getByRole('columnheader', { name: '…' })`, not `th` text.
- Match the assertion to the intent: "this specific heading says X" → named
  single-element locator; "some heading exists" → `.count()` / `toHaveCount`.

**Regeneration discipline:** regenerate in small batches (2–3 files at a time — a
single all-files pass strains the model and produces malformed syntax) and always
syntax-validate generated code (e.g. with `esbuild`) before running it.

## Known limitations

- **No product spec**, so the behavioral layer verifies structure/presence, not
  correctness of behavior. This is a property of the task, not a bug.
- **Chromium only.** Firefox/WebKit/mobile projects were removed while stabilizing;
  re-add them in `playwright.config.ts` and run `npx playwright install` to restore.
- **Auth not wired.** `login.mjs` can capture a session to `.auth/state.json`; to
  use it, add `storageState: '.auth/state.json'` to the config `use:` block.

## Next steps

- **CI:** a GitHub Actions workflow that runs the suite on every push and publishes
  the HTML report as an artifact. (Turns this from a QA task into a CI/CD one.)
- **Cross-browser:** re-enable Firefox/WebKit once the suite is stable.
- **v2 — pluggable spec generator (separate project):** a `generate.mjs` that takes
  `AI-TEST-GUIDE.md` + a page snapshot and emits a spec via a **swappable model
  backend** (OpenAI / Anthropic / local Ollama) with syntax validation. This keeps
  the runtime deterministic — the model only ever runs at authoring time.
