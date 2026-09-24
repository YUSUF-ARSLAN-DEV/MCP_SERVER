# Credential popup - design

Status: proposal (approved in principle, nothing built yet).

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

## Steps

### 1. Detect a login wall
In `explorer.py`, while recording a page, flag a `login` region when the page has a visible
`input[type=password]` (or `autocomplete=current-password`) inside a form. Record the fields (label, type,
required) on `PageInventory` as `auth`. Also flag when a navigation lands on a page that redirects to such a form.

### 2. Decide whether to ask
New setting `INTERACTIVE_AUTH` (default false). Ask only when: enabled, a display is available, and
`headless` is off for that session. Otherwise record "login wall not tested" as an untested area in the report.

### 3. The popup
A small local window (start with a Tk or pywebview window; no server needed) with three parts:
- **Mirror:** a screenshot of the agent's current page, refreshed every ~1s (`page.screenshot`). Live video is
  out of scope.
- **Form:** one input per detected field, masked for password types.
- **Guidance:** a plain-language line per field from its own label, e.g. "Email you use to sign in", plus a note
  on what the agent will do next ("I will submit this form and check that you are signed in").
Time-limited: a visible countdown; on timeout the run continues and marks the login as skipped.

### 4. Fill and confirm
Fill each field with `page.fill`, submit, then judge the outcome from observable signals (password field gone,
URL changed, error region appeared, a logout control appeared) - not by guessing.

### 5. Reuse the session
On success, save Playwright `storage_state` to `runs/<site>/auth/state.json` (git-ignored) so later runs start
logged in and never open the popup. On the next run, if that state no longer works, ask again.

### 6. Let flows use it
Relax the "no credentials" guard in `intents.py`/`documents.py` only when a saved session exists, so journeys
past the login can be proposed, expanded, verified and turned into tests like any other flow.

### 7. Retries (phase 2)
Record per attempt: `first_try_ok`, `attempts`, `failure_signal`. The popup shows the site's own error text and
lets the user retry. The report gets an "Authentication" line ("signed in on first attempt" / "needed 2
attempts") and a finding when a login never succeeds.

## Risks

- **Secrets leaking into artifacts.** Screenshots of a filled form can contain typed text: mask password fields
  in the mirror and never screenshot after values are typed.
- **Saved session is a credential.** Keep it out of git and out of the Word report; document how to delete it.
- **Sites that block automation** (bot detection, CAPTCHA). Detect and report as "could not test", never fail
  the run.
- **Unattended runs.** Must never hang waiting for input: the timeout and headless skip are mandatory.

## Suggested commit order

1. Detect + record `auth` in the inventory, and show "login wall found, not tested" in the report (no popup yet).
2. `INTERACTIVE_AUTH` setting + popup with mirror and guided form.
3. Fill, confirm, and saved session state.
4. Unblock flow generation behind a saved session.
5. Retry tracking and the report's Authentication line.

## Open questions for the team

- Which popup toolkit is acceptable to ship (Tk is built in; pywebview is nicer but adds a dependency)?
- Do we have a test account on the staging site to validate against?
