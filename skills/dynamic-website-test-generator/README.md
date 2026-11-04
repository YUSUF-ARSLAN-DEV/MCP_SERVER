# Dynamic Website Test Generator

An agent skill that writes **Playwright TypeScript tests for a website from what is really on the page**. It explores only the
URLs you give it, writes tests the page supports, rejects weak or unsafe specs with a mechanical validator, runs them, and
keeps a screenshot for every action plus a trace per test.

Works with any coding agent that can read a `SKILL.md`, run shell commands and edit files (Claude Code, Cursor, Copilot,
Codex CLI and others). Nothing in it is agent-specific.

## What you get
- `exploration/<page>.json` and `.aria.txt`: headings, controls (role, name, region, ambiguity, forms), accessibility snapshot
- `tests/<page>.spec.ts`: validated tests
- `evidence/<test>/<label>.png`, traces, `evidence/REPORT.md`, `evidence/index.json`

## Requirements
- Node.js 18 or newer (tested on Node 24) and npm
- `@playwright/test` and a Chromium build (`npx playwright install chromium`, about 120 MB, from the official Playwright CDN)
- A shell and file access for the agent

## Install
Copy the `dynamic-website-test-generator/` folder into your agent's skills location, for example:

| Agent | Folder |
| --- | --- |
| Claude Code | `~/.claude/skills/` or `<project>/.claude/skills/` |
| Cursor / Copilot / Codex CLI / others | the skills or rules folder your agent documents, or keep the folder anywhere and tell the agent to read its `SKILL.md` |

## Quick start
In the project you want tests in, ask your agent:

> Use the dynamic-website-test-generator skill on https://demo.playwright.dev/todomvc and give me passing tests with evidence.

Or run the steps by hand (replace `SKILL_DIR` with the skill folder):
```
npm init -y && npm i -D @playwright/test && npx playwright install chromium
mkdir -p tests && cp SKILL_DIR/scripts/evidence.ts tests/ && cp SKILL_DIR/scripts/playwright.config.template.ts playwright.config.ts
node SKILL_DIR/scripts/explore.mjs --out exploration --url https://demo.playwright.dev/todomvc
# the agent writes tests/todomvc.spec.ts from exploration/*.json (see examples/todomvc/)
node SKILL_DIR/scripts/validate-spec.mjs --url https://demo.playwright.dev/todomvc tests/*.spec.ts
npx playwright test
node SKILL_DIR/scripts/summarize-evidence.mjs
```
A complete finished run is in `examples/todomvc/`.

## Safety and privacy
- Visits **only** the URLs you provide; requests to other origins are blocked during exploration.
- No telemetry. The only network use is the target site you name and the standard npm / Playwright downloads.
- Does not read files outside the test project, does not run remote scripts, does not submit forms that ask for personal data.
- `--probe N` (off by default) clicks up to N buttons outside forms to learn what they reveal; that can change page state, so
  use it only on sites you are allowed to test.
- Screenshots and traces contain whatever the page shows. Review them before sharing.

## Limits
It checks that what it observed still works. It cannot know business rules a page does not show (a correct price, a correct
message), cannot get past logins or CAPTCHAs on its own, and Chromium is the only browser configured. Generated tests are
reviewed drafts: read them before relying on them.

## Layout
```
SKILL.md                 the workflow the agent follows
scripts/                 explore.mjs, validate-spec.mjs, summarize-evidence.mjs, evidence.ts, playwright.config.template.ts
references/              exploration-inventory.md, test-conventions.md, selector-strategy.md, evidence-format.md
examples/todomvc/        one small complete run
```
