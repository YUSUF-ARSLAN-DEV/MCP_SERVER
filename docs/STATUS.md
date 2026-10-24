# Status (2026-10-24)

An AI-assisted website smoke tester. Point it at a site (optionally with user stories saying what the site should do);
it explores the pages, checks them with the browser itself and with AI-written tests, verifies user journeys in a real
browser, and writes a Word report with a release verdict that states how much it rests on.

## Ways to use it

| How | Command / place |
| --- | --- |
| Web page | `docker compose up --build`, then http://127.0.0.1:8000 (create an account, add a model, *New run*; attach stories) |
| Command line | `python -m website_test_pipeline.cli all [stories.md]` (set `SITE`, `SEED_URL`; `WTP_JOB=1` lets the environment override `.env`) |
| From an AI client | `python -m mcp_adapter.server` (MCP; tools `start_run`, `run_status`, `get_report`, ...) |

## What is verified

- **Seeded-fault benchmark** (`tests_python/test_benchmark_bugsite.py`): a broken link, a missing image, a script error, a click
  that lands on a 404, a 500 page, a blank page and an always-failing data call are each found; a clean page and a data call
  that fails once and recovers are not reported.
- **A real production site** (sat.aljazeera.net, 8 pages, 16 user stories): GO with zero open defects, run through the web API.
  The report states its own limits: confidence "Partial", the three biggest limits of the run, 2 CAPTCHA forms needing a person.
- ~1210 automated tests pass (`pytest tests_python`).

## How the report stays honest

- The verdict word is about the site; the line under it says how much it rests on. What the tool could not test never counts
  against the site (a CAPTCHA, a requirement it could not turn into a test, a model that failed to write a test).
- Only faults the browser itself reports are defects (an error page, a broken link, a blank page, a click that lands on an error):
  each is asked again before it counts (a one-off 502 is a note), and a script error about another site's service is a note.
- A generated test that has never passed cannot prove the site broken: its unexplained failure goes to "needs triage".
- Keys in URLs are masked in the report. Defects found by the browser stay open across runs like failing tests.

## Known limits

- It checks that pages load and journeys work mechanically. It cannot know business rules (a wrong price, wrong text) unless a
  user story states them and the journey can observe them.
- A few AI-written tests of dropdown pickers still fail for the test's own reasons; they appear as "needs triage", never as defects.
- A run takes about 10 to 25 minutes. Chromium only. Runs click real buttons on the target: test only sites you may test.
  A form that asks for an e-mail, phone, password or card is never submitted (not even empty) unless `ALLOW_FORM_SUBMIT=1`,
  because that would create a real sign-up, message or order. Such a form is simply not exercised (one with a CAPTCHA is
  also listed as needing a person).
- The API has been reviewed and hardened (`docs/SECURITY.md`) but not independently tested; deploy it behind https with
  `SECRETS_KEY` / `SESSION_SECRET` from a secret store and egress rules for the container.

## Where things are

`website_test_pipeline/` the engine (`pagechecks.py` + `sweep.py` no-model checks, `runner.py` journeys, `report*.py` the report) ·
`api/` job API · `static/index.html` web page · `mcp_adapter/` MCP server · `docs/stories/` example user stories ·
`tests_python/` tests and the seeded-fault site (`fixtures/bugsite.py`).
