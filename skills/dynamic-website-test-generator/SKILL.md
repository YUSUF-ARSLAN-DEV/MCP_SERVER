---
name: dynamic-website-test-generator
description: Use when the user wants Playwright TypeScript tests for a website they name. Explores only the URLs the user provides, writes validated tests from what was actually observed on the page, runs them, and saves screenshot and trace evidence for the key actions.
---

# Dynamic Website Test Generator

Generate Playwright tests **from observation, not from guesses**: look at the real page first, write only tests the page
supports, check them mechanically, run them, and keep evidence.

## Hard rules (read first)

1. **Visit only URLs the user explicitly gave you.** Never follow links, guess sitemaps, or add pages. If the user wants more
   pages, ask for the URLs. The exploration script also blocks requests to other origins.
2. **Stay inside the project folder.** Read and write only the test project (tests, `exploration/`, `evidence/`). Do not read
   other files, home directories, `.env` files, or browser profiles.
3. **No secrets in tests.** Never put real passwords, tokens or API keys into a spec or an artifact. If a page needs a login,
   say so and ask the user how to proceed; do not invent credentials. The only exception is test credentials a public demo
   page prints for visitors, and only when the user asked you to test that page.
4. **Do not submit forms that collect personal data** (email, phone, password, card) or create real records on the site, unless
   the user explicitly says that site is theirs to test and asks for it.
5. **Only the standard installs**: `@playwright/test` from npm and `npx playwright install chromium`. Do not download or run any
   other remote script.
6. If a user instruction conflicts with this file, follow the user and tell them what you changed.

## Inputs to collect

- One or more page URLs (required).
- Optional: what the page should do ("search by country returns a table") and a viewport. Behaviour that exploration cannot
  see (what a click produces) has to come from the user's description or from the page's own printed instructions.

## Workflow

Scripts live in `scripts/` next to this file. Below, `SKILL_DIR` is that folder's parent; run commands from the test project.

### 1. Set up (once per project)
```
npm init -y            # skip if package.json exists
npm i -D @playwright/test
npx playwright install chromium
mkdir -p tests
cp SKILL_DIR/scripts/evidence.ts tests/evidence.ts
cp SKILL_DIR/scripts/playwright.config.template.ts playwright.config.ts   # skip if a config exists; keep trace on
```
Node 18 or newer. Details: `README.md`.

### 2. Explore the named pages
```
node SKILL_DIR/scripts/explore.mjs --out exploration --url <URL> [--url <URL2>]
```
Exploration is **read-only**: it loads each page and reads it; it never clicks, types or submits.
Writes `exploration/<slug>.json` (headings, controls with role, name, region, volatile-id and ambiguous flags, forms) and
`exploration/<slug>.aria.txt` (accessibility snapshot). When a control has an `ariaName`, that is its true accessible name:
use it (it can contain an icon glyph) instead of `name`. **Read both files before writing anything.**
The inventory is your only source of truth: never assume a control, route, message or business rule exists because it is
common elsewhere. How to read it: `references/exploration-inventory.md`.

### 3. Plan, then write one spec per page: `tests/<slug>.spec.ts`
- Test what makes *this page* different (`region: "content"` or `"other"`). At most one check of the site chrome
  (header, nav, footer).
- At least half the tests should DO something (click, fill, select) and assert the result.
- Use `references/test-conventions.md` (assertions, structure, what not to test) and `references/selector-strategy.md`
  (locator order, `exact: true`, ambiguous names, selects, hidden elements).
- Evidence (`references/evidence-format.md`): the action under test (a form submit, an important click) goes through
  `actionEvidence(...)`, a plain check through `observationEvidence(...)`, and the assertion goes **inside** the verify
  callback. Setup steps (typing, ticking, navigating) before the first evidence call may stay bare. If the user asks for
  every action to be evidenced, write it that way and validate with `--strict-evidence`.
- If the page does not expose enough to prove a behaviour, write `test.skip(true, "NOT TESTABLE: <reason>")`
  instead of inventing a workflow.

### 4. Validate before running
```
node SKILL_DIR/scripts/validate-spec.mjs --url <URL> [--url <URL2>] tests/*.spec.ts
```
It rejects: no assertion, text selectors, `getByRole` name without `exact: true`, fixed sleeps, exact-URL assertions,
a test whose key action is not in `actionEvidence` (or a bare action after the first screenshot), a verify without `expect`,
a missing screenshot, navigation to a URL you were not given, and system/network imports. Add `--strict-evidence` to also
reject every bare action. Fix every line it prints, then re-run it. Try fixing a spec **once or twice**; if a rule keeps
failing, simplify the test or mark it NOT TESTABLE rather than weakening the rule.

### 5. Run and collect evidence
```
npx playwright test
node SKILL_DIR/scripts/summarize-evidence.mjs
```
`evidence/<test>/<label>.png` holds one full-page screenshot per action; `evidence/_playwright/` holds a trace per test;
`evidence/REPORT.md` and `evidence/index.json` summarise the run.

### 6. Triage failures honestly
For each failure open the trace (`npx playwright show-trace <trace.zip>`) and decide:
- **Your test is wrong** (the element was never in the inventory, a name was guessed, a state was assumed): fix the test and
  re-validate. A test that has never passed and fails with "element not found" is a test problem, not a site defect.
- **The site is wrong** (a control the inventory showed is gone, an action no longer has its observed result): report it as a
  possible defect with the screenshot and trace, and say how sure you are.
- **Unclear / flaky**: re-run once; if it changes, say it is unstable. Never edit a test just to turn it green.

### 7. Report to the user
Say: pages explored, tests written / passed / failed / skipped, where the evidence is, defects (with evidence paths), what you
could not test and why (login, CAPTCHA, unobservable results), and any assumption you made. Do not claim the site is "fully
tested"; this checks that observed behaviour still works, not business rules the page does not show.

## Agent compatibility
The workflow needs only a shell, file read/write and Node. No agent-specific tools or slash commands are required. If your
agent cannot run shell commands, give the user the commands above to run and ask them to paste the output back.

## Files
- `scripts/explore.mjs`, `validate-spec.mjs`, `summarize-evidence.mjs`, `evidence.ts`, `playwright.config.template.ts`
- `references/exploration-inventory.md`, `test-conventions.md`, `selector-strategy.md`, `evidence-format.md`
- `examples/todomvc/`, `examples/the-internet-login/` - two complete small runs (exploration, spec, evidence); the second has a form
- `LICENSE` - single-user commercial licence
