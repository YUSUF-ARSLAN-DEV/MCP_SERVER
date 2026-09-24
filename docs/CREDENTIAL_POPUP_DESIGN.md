# Credential popup - design

Status: step 1 built (login-wall detection); steps 2-7 proposed.

## Problem

Pages behind a login are invisible to the pipeline. Today it avoids them on purpose:

- `intents.py:45` and `documents.py:43` forbid journeys that need "an account, password, payment".
- `heuristics.py:55` already recognises login/sign-in wording, but only to exclude it.
- Every browser is launched from `settings.headless` (`cli.py:57`, `cli.py:130`, `runner.py:535`), with no way to
  ask a person for anything mid-run.

So for any site with an account area, the pipeline tests only the public half.

## Goal

When the agent reaches a login wall, a window pops up that **mirrors what the agent sees**, tells the user in plain
words **what to type into each field**, and lets the agent continue once the user has filled it in. Later, it
records whether the login worked first time.

Non-goals (v1): CAPTCHA/2FA solving, storing passwords, live video, headless CI use.

## Principles

- **Generalize.** Detection uses DOM signals (password inputs, autocomplete hints, form structure), never
  site-specific selectors or English-only words. Field guidance is derived from the DOM label/placeholder in
  whatever language the page uses.
- **Secrets never persist.** Typed values go straight to Playwright. They are not written to logs, the run
  workspace, reports, the model prompt, or `flows.json`.
- **Opt-in.** Off unless enabled. Headless/CI runs skip the popup and say so, as `flows run` already does for
  outages.

## Two phases

The popup appears in exactly two situations, and credentials stay out of every generated file in both.

**Phase 1 - exploration.** The explorer cannot see anything behind a login wall, so this is where the popup is
needed. It mirrors the agent's view, guides the user through each field, logs in, and then persists two things
(both git-ignored, both outside the report and the model prompt):
- the logged-in session: Playwright `storage_state` at `runs/<site>/auth/state.json`;
- optionally (user opts in, off by default): the credentials as `.env` variables (`SITE_USER`, `SITE_PASSWORD`
  style names, one pair per site), so the session can be renewed without asking again.
The explorer then continues past the wall using that session.

**Phase 2 - test time.** Generated specs load the saved session and never contain a credential. If the session
has expired, the spec/runner re-logs in from the environment variable NAMES (`os.environ[...]`), never the values.
Only if that also fails, and the run is interactive, does the popup return; unattended runs mark the login
"needs attention" and continue.

## Steps

### 1. Detect a login wall  (DONE, commit 86c696d)
`explorer._detect_auth` records visible, non-readonly password fields and their sibling fields as
`PageInventory.auth`; the report lists each wall under "Not tested".

### 2. Decide whether to ask
New setting `INTERACTIVE_AUTH` (default false). Ask only when enabled, a display is available, and the browser is
headed for that session. Otherwise the wall stays in the report as "not tested".

### 3. The popup (phase 1)
A small local window (start with Tk or pywebview; no server) with:
- **Mirror:** a screenshot of the agent's current page, refreshed about once a second. No live video.
- **Form:** one input per detected field, masked for password types.
- **Guidance:** a plain-language line per field from the page's own label, plus what the agent will do next.
- **Save choices:** a checkbox "remember these in .env" (default off).
Time-limited with a visible countdown; on timeout the run continues and the login is marked skipped.

### 4. Fill, confirm, persist
Fill with `page.fill`, submit, judge success from observable signals (password field gone, URL changed, error
region appeared, a logout control appeared). On success write `storage_state`, then make sure it is ignored:
run `git check-ignore` on the file and, if it is not ignored, append its path to `.gitignore` before writing it.
If the user opted in, write the `.env` variables (same check for `.env`).

### 5. Reuse the session (phase 2)
Runner and generated specs start from `storage_state`. A cheap probe on start (is the login form still shown?)
decides whether the session is alive; if not, re-login from env names, then fall back to the popup if interactive.

### 6. Let flows use it
Relax the "no credentials" guard in `intents.py` / `documents.py` only when a working session exists, so journeys
past the login are proposed, verified and turned into tests like any other flow. Generated specs reference env
var names only.

### 7. Retries (phase 2 of the roadmap)
Record per attempt: `first_try_ok`, `attempts`, `failure_signal`. The popup shows the site's own error text and
lets the user retry. The report gets an "Authentication" line ("signed in on first attempt" / "needed 2
attempts") and a finding when a login never succeeds.

## Risks

- **Secrets leaking into artifacts.** Screenshots of a filled form can contain typed text: mask password fields
  in the mirror and never screenshot after values are typed.
- **The session file and `.env` are secrets on disk.** `.gitignore` covers `auth/`, `*.storage_state.json` and
  `.env`; code re-checks before writing; neither goes in the Word report or the model prompt; document how to
  delete them.
- **Sites that block automation** (bot detection, CAPTCHA). Detect and report as "could not test", never fail
  the run.
- **Unattended runs.** Must never hang waiting for input: the timeout and headless skip are mandatory.

## Suggested commit order

1. DONE - detect + record `auth`, show "not tested" in the report.
2. `INTERACTIVE_AUTH` setting + popup with mirror and guided form.
3. Fill, confirm, and saved session state.
4. Unblock flow generation behind a saved session.
5. Retry tracking and the report's Authentication line.

## Open questions for the team

- Which popup toolkit is acceptable to ship (Tk is built in; pywebview is nicer but adds a dependency)?
- Do we have a test account on the staging site to validate against?
