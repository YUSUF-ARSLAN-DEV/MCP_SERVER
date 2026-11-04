# Dynamic Website Test Generator

An agent skill that writes **Playwright TypeScript tests for a website from what is really on the page**. It explores only the
URLs you give it, writes tests the page supports, rejects weak or unsafe specs with a mechanical validator, runs them, and
keeps a screenshot for each key action plus a trace per test.

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
Complete finished runs are in `examples/todomvc/` and `examples/the-internet-login/` (a login form on a public demo site).

By default, setup steps (typing, navigating) can be plain Playwright calls and the action under test goes through
`actionEvidence`. Add `--strict-evidence` to `validate-spec.mjs` to require it for every action.

## Security
- **Read-only exploration:** `explore.mjs` loads and reads pages; it never clicks, types or submits.
- **Only user-provided URLs:** it never follows links, and the validator rejects a spec that navigates anywhere else.
- **Third-party origins blocked:** during exploration every request to an origin you did not name is aborted with
  `page.route`, and the blocked hosts are listed in the output (`blockedOtherOrigins`).
- **No network calls** besides the target site you name and the standard `npm` and `npx playwright install` downloads. No
  telemetry, no remote scripts downloaded or run.
- **No API keys needed.** Your own agent does the writing; the skill never asks for or stores a key, token or password.
- The validator rejects generated specs that import system or network modules, run processes, evaluate strings as code, read
  secrets from the environment, or delete files. Its list of those patterns is a defensive denylist, not functionality.
- Forms that ask for personal data (e-mail, phone, password, card) are not submitted unless you explicitly ask and the site is
  yours to test.
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
examples/                two small complete runs (todomvc, the-internet-login)
LICENSE                  licence terms
```

## Licence
Commercial single-user licence: see [LICENSE](LICENSE). In short, you may use and modify the skill in your own and your
employer's projects; redistribution, resale or republishing, modified or not, is not permitted.
