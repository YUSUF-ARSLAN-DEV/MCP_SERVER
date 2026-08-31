# AI Test Generation Guide

You are generating website-independent **Python + Playwright** smoke tests, run
under `pytest`. Follow this guide exactly. It encodes hard-won rules — every
rule here exists because ignoring it caused real failures.

## Task

1. You are given ONE page: its URL, title, observed inventory (headings and
   interactive controls), and a filtered accessibility snapshot.
2. Produce ONE `pytest` module for that page — a set of independent smoke tests
   derived only from what was actually observed.
3. Return **exactly one complete Python code block** (```python … ```), no prose,
   no alternatives, no partial drafts. The pipeline extracts the first code
   block and writes it to `python_tests/<slug>_test.py`.
4. There is no fixed test count and no fixed workflow. Generate a test only when
   the observed inventory gives you a reliable postcondition to assert.

## Output contract (the pipeline rejects the spec if any of these fail)

- The module must be valid Python (it is parsed with `ast.parse`).
- It must contain at least one real `assert` **or** Playwright `expect(...)`
  assertion. A test that only clicks and waits is invalid.
- The **exact page URL string must appear literally** in the module (use it in
  `page.goto(...)` and/or an `expect(page).to_have_url(...)` check).
- **No text-based selectors.** The strings `get_by_text`, `:has-text`, and
  `text=` are forbidden anywhere in the file.
- **No system imports.** Never import `os`, `subprocess`, or `socket`.
- Every state-changing action (`.click(`, `.fill(`, `.select_option(`,
  `.check(`, `.uncheck(`, `.press(`) must go through `action_evidence(...)`
  (see Evidence rules). A bare action in the module body is rejected.

## Module skeleton

```python
from pathlib import Path
from playwright.sync_api import Page, expect
from website_test_pipeline.evidence import action_evidence, observation_evidence
from website_test_pipeline.pageutils import open_page

URL = "<exact page URL>"


def _open(page: Page) -> None:
    open_page(page, URL)  # navigate, wait for load, clear consent / ad overlays


def test_<behavior_specific_to_this_page>(page: Page, evidence_dir: Path) -> None:
    _open(page)
    expect(page).to_have_url(URL)
    heading = page.get_by_role("heading", name="<exact accessible name>")
    observation_evidence(page, "heading-visible", lambda: expect(heading).to_be_visible(), evidence_dir)
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
  check to the landmark and every footer check to `contentinfo`:
  `page.get_by_role("navigation").get_by_role("link", name="News", exact=True)`,
  `page.get_by_role("contentinfo").get_by_role("link", name="Privacy Policy", exact=True)`.
  If a locator can still match more than one, append `.first` for a
  presence check, or assert `.to_have_count(1)` when the count is the point.
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
  - Any `sr-only` / visually-hidden element: check presence, not visibility.
- **Responsive / mobile-only elements** (hamburger, nav toggle): do not assert
  visibility at desktop width. Call
  `page.set_viewport_size({"width": 375, "height": 667})` first, then assert.
- Match the assertion to the intent: "this specific heading says X" → a named
  single-element locator; "some heading exists" → `.to_have_count(...)`.
- **After clicking a navigation link, do not assert an exact URL** — sites
  redirect, append locale/tracking, or trailing-slash. Use a glob
  (`page.wait_for_url("**/middleeast**")`) or, better, assert a heading/landmark
  that is unique to the destination page.

## Evidence rules

- For every meaningful user action, wrap it:
  `action_evidence(page, "<label>", lambda: <do the action>, lambda: <assert the
  post-state>, evidence_dir)`. It performs the action, runs the verify callback,
  then captures a full-page screenshot and returns its path.
- For a pure observation with no action, use
  `observation_evidence(page, "<label>", lambda: <assert>, evidence_dir)`.
- The `verify` callback **must contain a real assertion** — visible text, a
  changed value, a target URL. Attachment or attachment-count alone is invalid.
- Give every label a descriptive, page-specific name such as
  `country-selected-post-state`, not `works` or `after-click`.
- For a native `<select>`, verify the selected value changed — opening the OS
  menu is not evidence. For a custom picker, verify the option list is visible
  and contains the expected options.
- After a language switch, verify the target URL **and** a target-language
  title and visible heading before capturing.
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

## What NOT to test

- External domains — only this site.
- Analytics / third-party console noise.
- Anything you did not actually observe in the supplied inventory.
