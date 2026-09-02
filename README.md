# Dynamic Website Test Generator and Evidence Suite

This repository explores a URL with Playwright, supplies compact page-specific
signals to a configurable OpenAI-compatible model, validates the generated
tests, executes the reviewed smoke tests, and produces screenshot evidence.
Generated output is intentionally ignored by Git.

The implementation is a single Python package: `website_test_pipeline`. (An
earlier JavaScript/TypeScript implementation was removed — see
`git log` for history.)

## Setup

Requirements: Python 3.10+ and Playwright Chromium.

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
# Edit .env and provide API_KEY locally
```

Never commit `.env`, API keys, reports, screenshots, traces, or generated
specifications. The model endpoint is an external dependency and may return
rate limits, Cloudflare 524/530 errors, malformed responses, or timeouts. The
generator retries transient failures and records the exact error per URL in
`python_artifacts/run.json` and `python_artifacts/generation.log`.

## Commands

```powershell
python -m website_test_pipeline.cli crawl      # BFS from SEED_URL, overwrite urls.txt with same-origin pages
python -m website_test_pipeline.cli explore    # load each URL, write page inventories
python -m website_test_pipeline.cli generate   # explore + generate a validated spec per URL
python -m website_test_pipeline.cli execute    # run the generated specs under pytest
pytest tests_python -q                          # unit tests for the pipeline itself
```

- URLs come from `urls.txt` (one per line, `#` comments allowed). Override with
  the `URLS_FILE` environment variable. `crawl` is the only command that writes
  `urls.txt` — it replaces the file with the pages it discovered. The other
  commands never modify it, so a hand-curated list stays intact until you run
  `crawl` again. `crawl` requires `SEED_URL`; it is never run automatically.
- Generated specs are written to `python_tests/`; run artifacts and inventories
  to `python_artifacts/`. Both directories are ignored by Git.
- Failures are isolated per URL — one unreachable page or one bad model
  response does not stop the run.

### Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `API_URL` | `https://llm-1.d4done.com/v1/chat/completions` | OpenAI-compatible chat completions endpoint |
| `API_KEY` | *(empty)* | Bearer token for the endpoint |
| `MODEL_NAME` | `qwen/qwen3-coder-30b` | Model identifier — use a non-reasoning code model |
| `MODEL_TIMEOUT_MS` | `300000` | Per-request timeout |
| `MODEL_RETRIES` | `4` | Retry count for transient failures |
| `RETRY_BASE_MS` | `3000` | Base for exponential backoff |
| `NAV_TIMEOUT_MS` | `60000` | Playwright navigation timeout |
| `HEADLESS` | `true` | Set `false` to watch the browser (`explore`/`generate`/`crawl`) |
| `SEED_URL` | *(empty)* | Entry point for `crawl`; required by that command |
| `CRAWL_MAX_DEPTH` | `3` | BFS depth from the seed (`0` = seed only) |
| `CRAWL_MAX_PAGES` | `100` | Hard cap on discovered URLs |
| `EXTRA_URLS` | *(empty)* | Comma/space-separated same-origin URLs merged into `crawl` output (deep links the BFS can't reach with real params) |
| `EXPLORE_PROBE_MAX` | `5` | `explore` clicks up to N `[content]` triggers per page and records what each surfaced (`0` disables) |
| `URLS_FILE` | `urls.txt` | Path to the URL list (`crawl` overwrites it) |

`crawl` also drops URLs whose path holds an unsubstituted placeholder
(`/-1/`, `/null`, `/undefined`, `/{id}`, `/:slug`, …) — they render empty shells
and only ever produce shallow visibility tests. Put the real deep links in
`EXTRA_URLS` or a `seeds.txt` file in the workspace (`runs/<site>/seeds.txt`);
both are canonicalized, same-origin-filtered, and folded into `urls.txt`.

## How the pipeline works

0. **`crawl`** (`crawler.py`) — optional. Breadth-first walk from `SEED_URL`,
   following only same-origin `<a href>` links, canonicalized and deduplicated,
   bounded by `CRAWL_MAX_DEPTH` / `CRAWL_MAX_PAGES`, with placeholder URLs
   dropped and `EXTRA_URLS` / `seeds.txt` merged in. Overwrites `urls.txt` with
   the result. Skip it and maintain `urls.txt` by hand if you prefer. No AI.
1. **`explore`** (`explorer.py`) — loads each page in Chromium and records a
   compact `PageInventory`: title, visible headings, up to 120 interactive
   controls, and a filtered ARIA snapshot. Then it runs an interaction probe:
   it clicks up to `EXPLORE_PROBE_MAX` `[content]` buttons and records what each
   one revealed (new in-page controls) or where it navigated, so `generate` has
   a real postcondition to assert instead of a bare visibility check. No AI.
2. **`generate`** (`generator.py` + `llm.py`) — builds a prompt from
   `AI-TEST-GUIDE.md` + `persona.txt` + the page inventory, calls the model,
   and extracts a single Python code block.
3. **`validate`** (`validator.py`) — AST-parses the generated spec and rejects
   it unless it asserts something, references the target URL, wraps every
   state-changing action in `action_evidence(...)`, avoids unstable text
   selectors, and imports nothing dangerous (`os`, `subprocess`, `socket`).
   Invalid specs trigger one regeneration attempt.
4. **`execute`** — runs `python_tests/` under `pytest`. Each spec captures a
   full-page screenshot after every verified action (`evidence.py`).
5. **`report.py`** — assembles captured evidence into a Word document.

## Architecture — three layers, ordered by how much you can trust them

The suite is deliberately split into three layers. The first two are pure code
and can be trusted completely; the third is AI-assisted and is treated as
*reviewed drafts*, never as ground truth.

1. **Deterministic exploration** (no AI, trustworthy) — visits each URL and
   records only what is observably on the page.
2. **Mechanical validation** (no AI, trustworthy) — AST checks on the generated
   spec: has assertions, names the URL, evidences its actions, no unsafe
   imports, no unstable selectors.
3. **Behavioral tests** (AI-generated, reviewed drafts) — one spec per page,
   generated from `AI-TEST-GUIDE.md`. This is the only fuzzy layer — the only
   one that can hallucinate — so every spec is human-reviewed.

## Core principle: `is` vs `should`

An AI can observe what **is** on a page (an element exists, is visible, is named
X). It cannot know what **should** be there — the intended behavior lives in the
developers' heads or a spec, neither of which we have here. So the behavioral
tests are **is-checks, smoke tests, and change-detectors**, not behavior
specifications. When a pattern is wrong, fix the **generator**
(`AI-TEST-GUIDE.md`) and regenerate — don't hand-patch each site's output.

## `AI-TEST-GUIDE.md` — the reusable asset

This is the part worth keeping. It's the rulebook the model reads to generate
specs, and it encodes the locator lessons this project paid for:

- Target an element by its **exact accessible name, copied verbatim from the
  page snapshot** — never an OR-regex or a single shared word (they match
  sibling elements and cause strict-mode violations).
- The site has **no `<main>` landmark** — use `#main-content` (the skip-link
  target). Header/footer exist but the header is empty, so assert attachment,
  not visibility.
- The mobile nav toggle is `button.navbar-toggle`, `display:none` at desktop
  width (absent from the accessibility tree — use a CSS locator).
- Table columns are matched by `columnheader` role and name, not `th` text.
- Match the assertion to the intent: "this specific heading says X" → named
  single-element locator; "some heading exists" → a count assertion.

**Regeneration discipline:** regenerate in small batches (2–3 files at a time —
a single all-files pass strains the model and produces malformed syntax) and
always validate generated code before running it.

## Known limitations

- **No product spec**, so the behavioral layer verifies structure/presence, not
  correctness of behavior. This is a property of the task, not a bug.
- **Chromium only.**
- The model endpoint is external and rate-limited; large runs will hit
  transient failures that the retry logic absorbs but cannot eliminate.
