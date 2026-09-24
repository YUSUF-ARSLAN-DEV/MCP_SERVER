# Credential popup - design

Status: steps 1-6 built (detect, AUTH_MODE, popup, fill/confirm/save session, session -> .env -> popup order with
accounts - try it with `python -m website_test_pipeline.cli auth <url> [--account NAME]`); step 7 proposed.

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

## When the agent authenticates

Default rule (decided with the team, 2026-09-24): when a page has a **login** form the agent logs in, and when it
has a **sign-up** form it signs up - unless that was already done (a working saved session, or an account
already created for this site), in which case it does neither. Two consequences:

- A logged-in user who is redirected away from the login / landing page means that page is not testable while
  logged in; it is skipped and reported, not treated as a failure.
- A subscribe / newsletter / contact form has no password. It is a **feature of the site**, not a wall: it is
  always tested like any other form (fill, submit, assert), never through the popup. Only password-based
  logins and sign-ups use the popup.

`AUTH_MODE` in `.env`: `auto` (default - the rule above) or `none` (never ask, never log in; walls are only
reported as not tested). Sign-ups create real accounts, so they use only details the user typed into the popup,
and a CAPTCHA or email verification step is reported as a limit, not a failure.

## Two phases

The popup appears in exactly two situations, and credentials stay out of every generated file in both.

**Phase 1 - exploration.** The explorer cannot see anything behind a login wall, so this is where the popup is
needed. It mirrors the agent's view, guides the user through each field, logs in, and then persists two things
(both git-ignored, both outside the report and the model prompt):
- the logged-in session: Playwright `storage_state` at `runs/<site>/auth/state.<account>.json`;
- optionally (user opts in, off by default): the credentials as `.env` variables
  (`AUTH_<SITE>_<ACCOUNT>_<FIELD>`), so the session can be renewed without asking again. `.env` details, when
  present, are used BEFORE the popup (step 5), so unattended runs are never interrupted.
The explorer then continues past the wall using that session.

**Phase 2 - test time.** Generated specs load the saved session and never contain a credential. If the session
has expired, the spec/runner re-logs in from the environment variable NAMES (`os.environ[...]`), never the values.
Only if that also fails, and the run is interactive, does the popup return; unattended runs mark the login
"needs attention" and continue.

## Steps

### 1. Detect a login wall  (DONE, commit 86c696d)
`explorer._detect_auth` records visible, non-readonly password fields and their sibling fields as
`PageInventory.auth`; the report lists each wall under "Not tested".

### 2. Decide whether to ask  (DONE - `authpopup.should_ask`, `Settings.auth_mode`)
`AUTH_MODE` (`auto` | `none`). Ask only when the mode is `auto`, the run is interactive (a terminal and a
display, Tk available), the wall's form is password-based, and there is no working session or account yet. The
browser can stay headless because the popup mirrors it with screenshots. Otherwise the wall stays in the report
as "not tested".

### 3. The popup (phase 1)  (DONE - `authpopup.py`; demo: `python -m website_test_pipeline.authpopup`)
A small local window (start with Tk or pywebview; no server) with:
- **Mirror:** a screenshot of the agent's current page, refreshed about once a second. No live video.
- **Form:** one input per detected field, masked for password types.
- **Guidance:** a plain-language line per field from the page's own label, plus what the agent will do next.
- **Save choices:** a checkbox "remember these in .env" (default off).
Time-limited with a visible countdown; on timeout the run continues and the login is marked skipped.

### 4. Fill, confirm, persist  (DONE - `authflow.py`; up to 3 attempts, the site's own error shown between them)
Fill with `page.fill`, submit, judge success from observable signals (password field gone, URL changed, error
region appeared, a logout control appeared). On success write `storage_state`, then make sure it is ignored:
run `git check-ignore` on the file and, if it is not ignored, append its path to `.gitignore` before writing it.
If the user opted in, write the `.env` variables (same check for `.env`).

### 5. Reuse the session, .env first, accounts  (DONE - `authflow.ensure_session`)
Order on every `explore` / `generate` / `auth`, so an unattended run is never interrupted:
1. a saved session that still gets past the wall (`ensure_session` loads it and re-checks the page);
2. otherwise the details in `.env` for this site + account, signed in silently;
3. otherwise, only in an interactive run, the popup (which can offer to remember the details in `.env`);
4. otherwise the wall is reported "not tested" and the exact `.env` names to fill in are printed.
Crawl, explore, verify and every generated test start from the saved session (`WTP_STORAGE_STATE` for pytest).

**Exploring what is behind the login.** The login detector also runs on every page the crawler loads
(`authflow.wall_handler`): when a page deeper in the site shows a wall (e.g. `/account` redirecting to a login
form) it signs in on the spot - session, then `.env`, then the popup - and reloads the page, so the rest of the
crawl sees the logged-in area. It acts once per crawl, and not at all when the seed check already handled a login.
`crawl` also runs the same sequence before it walks the site, and the page a successful sign-in ends on (`auth/landing.<account>.txt`) is crawled as a second starting point.
That page is not linked from the public site, so without this nothing would ever discover the logged-in area
(before, a `seeds.txt` had to be written by hand). Everything downstream - explore, intents, expand, verify, the
generated tests - then sees the logged-in pages through the saved session.

**Login check before the browser stages** (`authflow.preflight_session`). `flows run`, `execute` and `report` first
run the same check on the seed URL, so a session that expired since `explore` is renewed silently from `.env`. If it
cannot be renewed (details missing or refused, popup skipped) the browser stages are skipped with the reason
instead of producing tests that all fail at the login page. A site with no wall costs one extra page load.

**Accounts.** A site with several logins (admin / customer, different passwords) uses `--account NAME` or
`AUTH_ACCOUNT` (default `default`). Each account has its own session (`auth/state.<account>.json`) and `.env` keys
`AUTH_<SITE>_<ACCOUNT>_<FIELD>`. Nobody has to know the field names in advance: the tool prints them for the
form it found. Not yet done: a flow declaring which account it needs, and a wall found on a page deeper than the
seed URL (it is still reported as not tested).

### 6. Let flows use it  (DONE - `secretrefs.py`, `authflow.bind_credentials`, `intents.unsuitable_reason(allow_account=)`)
- **The guard.** When the pipeline holds a login (a saved session, or `.env` details for a wall it found) the AI may
  write journeys that sign in; payment and personal data stay banned. The prompt tells it to write "signs in with
  the saved account", never a username or password. With no login the old guard applies unchanged.
- **References, never values.** Expansion (code, not the model) points every fill step aimed at a login field at
  `{env:AUTH_<SITE>_<ACCOUNT>_<FIELD>}`. A flow whose login details are not in `.env` is not built and the reason
  names the variables to set. `flows.json`, the plain-language journey, the report and the generated test hold only
  those names.
- **Generated tests.** They call `secret('NAME')` (reads the variable at run time), assert `not_to_have_value("")`
  instead of comparing against a secret, and use the `logged_out_page` fixture, because a sign-in flow must start
  signed OUT (a saved session would already be past the login, and most sites redirect a signed-in visitor away
  from `/login`). `verify` does the same. A password box is never filled with a guessed value.
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
