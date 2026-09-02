# AI Test Generation Guide

You are generating website-independent **Python + Playwright** smoke tests, run
under `pytest`. Follow this guide exactly. It encodes hard-won rules — every
rule here exists because ignoring it caused real failures.

## Task

1. You are given ONE page: its URL, title, observed inventory (headings and
   interactive controls), a filtered accessibility snapshot, and a
   **REVEALED BY INTERACTION** block listing what the explorer saw appear when it
   clicked specific `[content]` triggers.
2. Produce ONE `pytest` module for that page — a set of independent smoke tests
   derived only from what was actually observed.
3. Return **exactly one complete Python code block** (```python … ```), no prose,
   no alternatives, no partial drafts. The pipeline extracts the first code
   block and writes it to `python_tests/<slug>_test.py`.
4. There is no fixed test count and no fixed workflow. Generate a test only when
   the observed inventory gives you a reliable postcondition to assert.

## Inventory tags

Each CONTROL and HEADING line is prefixed with tags:

- `[content]` — inside `<main>` / `<article>`; specific to **this** page.
- `[chrome]` — inside the header, nav, or footer; the same on every page of the site.
- `[other]` — neither.
- headings also carry `[hidden]` (present only for screen readers — never assert
  `to_be_visible`, use `to_have_count(1)`) and `[feed]` (inside an article/feed —
  the text rotates, do not assert it).
- controls carry `VOLATILE-ID` (the id/name is auto-generated and changes every
  load — locate by role/placeholder/label, never `#id`) and `AMBIGUOUS` (the
  name was seen on more than one element).

## Coverage — test this page, not the site chrome

- **At most ONE `[chrome]` test.** A single check that the primary navigation and
  footer are present is enough. Do **not** write a footer test, a newsletter
  test, a language-switch test and a search test on every page — those are
  identical site-wide and add no coverage.
- **Every other test targets a `[content]` control or `[content]` heading** — the
  thing that makes this page different.
- **If the page has only one or two `[content]` controls, write only one or two
  tests.** A short page-specific file beats a long one padded with chrome checks.
- **At least half your tests must DO something** (click / fill / select / press)
  and assert the result — not just `expect(x).to_be_visible()`.
- **Use the REVEALED BY INTERACTION block.** For every `click "<trigger>" ->
  reveals:` entry, write a test that clicks that trigger via `action_evidence`
  and asserts one of the listed revealed controls became visible. For a
  `click "<trigger>" -> navigates to <path>` entry, click via `action_evidence`
  and assert `expect(page).to_have_url(re.compile(r"<path>"))`. These count as
  behavioural tests; the trigger names and revealed selectors are already in the
  observed inventory, so they pass the selector check.

## Output contract (the pipeline rejects the spec if any of these fail)

- The module must be valid Python (it is parsed with `ast.parse`).
- It must contain at least one real `assert` **or** Playwright `expect(...)`
  assertion. A test that only clicks and waits is invalid.
- The **exact page URL string must appear literally** in the module — as the
  `URL = "..."` constant that `_open` passes to `open_page`. Do **not** add an
  `expect(page).to_have_url(URL)` check (sites redirect / add a trailing slash or
  locale — see Assertion rules).
- **No text-based selectors.** The strings `get_by_text`, `:has-text`, and
  `text=` are forbidden anywhere in the file.
- **No system imports.** Never import `os`, `subprocess`, or `socket`.
- Every state-changing action (`.click(`, `.fill(`, `.select_option(`,
  `.check(`, `.uncheck(`, `.press(`) must go through `action_evidence(...)`
  (see Evidence rules). A bare action in the module body is rejected.

## Module skeleton

```python
import re
from pathlib import Path
from playwright.sync_api import Page, expect
from website_test_pipeline.evidence import action_evidence, observation_evidence
from website_test_pipeline.pageutils import open_page

URL = "<exact page URL>"


def _open(page: Page) -> None:
    open_page(page, URL)  # navigate, wait for load, clear consent / ad overlays


def test_<behavior_specific_to_this_page>(page: Page, evidence_dir: Path) -> None:
    _open(page)
    heading = page.get_by_role("heading", name="<exact accessible name from the snapshot>", exact=True)
    observation_evidence(page, "main-heading-visible", lambda: expect(heading).to_be_visible(), evidence_dir)
```

- The `page` fixture comes from `pytest-playwright`. Do not create your own
  browser or `sync_playwright()` block.
- `evidence_dir` is a `pathlib.Path` fixture (from conftest.py) — the persistent
  folder this test's screenshots are written to. Pass it straight into
  `action_evidence` / `observation_evidence`. Do not use `tmp_path`.
- **Every test must capture at least one screenshot.** A read-only test calls
  `observation_evidence(...)` once; a test with actions wraps each one in
  `action_evidence(...)`. A test with no evidence call is rejected.
- Call `_open(page)` (or inline equivalent) at the start of every test — tests
  must be independent and not rely on another test's navigation or side effects.

## Locator rules (these caused the most failures)

- **Prefer, in this order:** `page.get_by_role("<role>", name="<accessible
  name>")`, then an attribute locator (`page.locator('a[href="/en/map"]')`),
  then `page.get_by_label(...)` / `page.get_by_test_id(...)`.
- **For a `<select>` / combobox, always locate by id or selector**
  (`page.locator('#countrylist')`). Never `get_by_label` or
  `get_by_role(name=...)` for a select — its accessible name is often every
  option concatenated and will not match.
- Copy the accessible name **verbatim from the snapshot**. Never guess, invent,
  or translate it. Never use an OR-regex or a single shared word — it matches
  sibling elements and raises a strict-mode violation.
- **Always pass `exact=True`** to `get_by_role(..., name=...)`, `get_by_text`
  is banned, `get_by_label(..., exact=True)`. A partial name match on a busy
  page (header + footer + newsletter widget) hits multiple elements.
- **Common link/heading names** ("News", "Sport", "Privacy Policy", "About")
  appear in the header nav, the footer, AND inline widgets. Scope every nav
  check to the landmark and every footer check to `contentinfo`, **and append
  `.first`** — pages routinely render the primary nav twice (desktop +
  mobile/mega-menu) so even a landmark-scoped locator matches two elements:
  `page.get_by_role("navigation").get_by_role("link", name="News", exact=True).first`,
  `page.get_by_role("contentinfo").get_by_role("link", name="Privacy Policy", exact=True).first`.
  Do the same for any control the inventory marks `AMBIGUOUS`. When the count
  itself is the point, assert `.to_have_count(n)` only if you observed `n`.
- **Only real ARIA roles.** `button`, `link`, `heading`, `textbox`, `combobox`,
  `checkbox`, `radio`, `navigation`, `banner`, `contentinfo`, `list`,
  `listitem`, `dialog`, `region`, `img`, … There is **no `video` role** — a
  `<video>` has no role; locate a player by id/selector or an observed control.
- On Arabic (`/ar`) pages use the actual Arabic accessible names you observe.
  Do not reuse English names like "Toggle navigation".
- Never assume a control, route, success message, language, or business rule
  exists because it is common on other sites. The observed inventory, title,
  URL, and accessibility snapshot are the only source of truth.

## Assertion rules

- Use Playwright's auto-retrying `expect(...)` for anything the page renders
  asynchronously (`to_be_visible`, `to_have_text`, `to_have_url`,
  `to_have_count`). Use bare `assert` only for pure Python values you already
  hold.
- **Do not assert `to_be_visible()` on elements hidden by design:**
  - Skip links (`a[href="#main-content"]`) are screen-reader-only. Assert
    `expect(locator).to_have_count(1)` instead.
  - Any `sr-only` / visually-hidden element, or a heading tagged `[hidden]`:
    check presence with `to_have_count(1)`, not visibility.
- **Below-the-fold elements** (footer links, newsletter widget): call
  `locator.scroll_into_view_if_needed()` as a plain statement *before* the
  `observation_evidence(...)` line, then assert visibility.
- **VOLATILE-ID controls:** never `page.locator("#that-id")` — locate with
  `get_by_role` / `get_by_placeholder` / `get_by_label`.
- **Language switch:** the "English" / "العربية" toggle usually leaves the
  domain. Assert only that the link is visible; never assert the resulting URL.
  Prefer not to test it — it is `[chrome]` and identical everywhere.
- **Responsive / mobile-only elements** (hamburger, nav toggle): do not assert
  visibility at desktop width. Call
  `page.set_viewport_size({"width": 375, "height": 667})` first, then assert.
- Match the assertion to the intent: "this specific heading says X" → a named
  single-element locator; "some heading exists" → `.to_have_count(...)`.
- **Never assert an exact URL** — not after a click and not for the page under
  test. Sites redirect, append a locale segment, tracking params, or a trailing
  slash. And `to_have_url()` does **not** understand globs. Inside a verify
  callback use a path regex: `expect(page).to_have_url(re.compile(r"/middleeast"))`
  (`import re` is in the skeleton). `page.wait_for_url("**/middleeast**")` also
  works but only as a plain statement, not inside a `lambda`. Best of all, assert
  a heading/landmark unique to the destination. To confirm the page under test
  loaded, assert its main heading is visible.
- **Do not assert a state your own action just changed or removed.** A submit
  button that becomes disabled or relabelled ("Subscribing…") after a click, an
  overlay `_open()` already dismissed, a field that clears on submit — verify the
  genuine post-state instead (a value you set, a panel that appeared, a URL glob).
- **Do not assert guessed copy.** No invented success/error messages. Do not
  assert the exact text of individual article/news headlines or anything inside a
  feed, list, or article region — it rotates between crawl and run. Assert stable
  structural headings and labels only.

## Evidence rules

- For every meaningful user action, wrap it:
  `action_evidence(page, "<label>", lambda: <do the action>, lambda: <assert the
  post-state>, evidence_dir)`. It performs the action, runs the verify callback,
  then captures a full-page screenshot and returns its path.
- For a pure observation with no action, use
  `observation_evidence(page, "<label>", lambda: <assert>, evidence_dir)`.
- The `verify` callback **must itself call `expect(...)`** — e.g.
  `lambda: expect(field).to_have_value("x")`. The screenshot is taken right after
  `verify()` runs, so the assertion has to be *inside* the callback.
  `lambda: None` (or putting the assertion only outside the callback) is rejected.
- Give every label a descriptive, page-specific name such as
  `country-selected-post-state`, not `works` or `after-click`.
- For a native `<select>`, verify the selected value changed — opening the OS
  menu is not evidence. For a custom picker, verify the option list is visible
  and contains the expected options.
- If the page does not expose enough to prove the intended behavior, emit a
  skipped test instead of guessing:
  `@pytest.mark.skip(reason="NOT TESTABLE: <specific reason>")` (add
  `import pytest` when you use it). Do not invent a workflow.

## Structure rules

- Name each test after the behavior it checks, and make the name match what it
  actually asserts. No generic names (`test_works`, `test_navigation`,
  `test_displays_correctly`).
- Every test name must identify the page-specific target: the exact heading,
  link, field, picker, button, or resulting state observed on this page.
- Keep tests independent and self-contained — each test does its own setup.
- **Define every locator variable inside the test that uses it.** A name bound in
  one test is invisible in another — the spec is rejected for using an undefined
  name (Python would raise `NameError`).
- **`.first` goes on the locator line.** For an `AMBIGUOUS` control, or any
  `[name=...]` / role+name the CONTROLS list shows more than once (a wizard with
  several "Next" buttons, a nav rendered twice), put `.first` (or `.nth(i)`) on
  the same line as the `page.locator(...)` / `get_by_role(...)` call.
- Use one consistent verb per concept: `test_<thing>_present`, not a mix of
  `_visible` / `_exists` / `_present` for the same kind of check.

## What NOT to test

- External domains — only this site.
- Analytics / third-party console noise.
- Anything you did not actually observe in the supplied inventory.
