# AI Test Generation Guide

You are generating website-independent Playwright smoke tests. Follow this guide exactly.
It encodes hard-won rules — every rule here exists because ignoring it caused real failures.

## Task

1. Read the supplied URL list. It contains one URL per line; the domain, language, controls, and workflows are unknown until observed.
2. Process the URLs **one at a time**.
3. For each URL, navigate to it and read the accessibility snapshot.
4. Create ONE spec file per page in `tests/`, named after the page
   (e.g. `en-frequency-search.spec.ts`, `ar-subscribe.spec.ts`). Never combine pages.
5. Wrap each page's tests in `test.describe('<full page URL>', () => { ... })` so the
   HTML report groups tests by page.
6. If a spec file for that page already exists, OVERWRITE it.
7. When finished, report how many spec files you created.

## Locator rules (these caused the most failures)

- **Never use `:has-text(...)` or text-based CSS selectors.** They match visible text,
  which is often translated (Arabic pages), rendered differently, or not the accessible name.
- **Prefer, in this order:** `getByRole('<role>', { name: '<accessible name>' })`, then
  `href`-based locators (`a[href="/en/map"]`), then `getByLabel` / `getByTestId`.
- Read the **actual** accessible name from the snapshot. Do not guess or invent text.
- Never assume a control, workflow, success message, language, route, or business rule exists because it is common on another website.
- Use the page title, URL, accessibility snapshot, and observed DOM state as the only source of truth.
- On Arabic (`/ar`) pages, use the actual Arabic accessible names you observe —
  do NOT reuse English names like "Toggle navigation".

## Assertion rules

- **Every test must have at least one real `expect`.** Never write a test that only
  clicks and waits.
- **`await` every async call.** In particular `await page.title()`, `await locator.inputValue()`.
  A missing `await` returns a Promise and the assertion silently checks the wrong thing.
- **Do not assert `toBeVisible()` on elements that are hidden by design:**
  - Skip links (`a[href="#main-content"]`) are screen-reader-only and invisible.
    Assert `await expect(locator).toHaveCount(1)` or `.toBeAttached()` instead.
  - Any visually-hidden / `sr-only` element: check presence, not visibility.
- **Responsive / mobile-only elements** (hamburger / nav toggle): do NOT assert they are
  visible at desktop width. Set a mobile viewport first
  (`await page.setViewportSize({ width: 375, height: 667 })`), then assert visible.
- For links that may sit below the fold, `toBeVisible()` is fine (it checks render state,
  not viewport), but if unsure prefer `.toBeAttached()`.

## Evidence rules

- For every meaningful user action, import `actionEvidence` from `./evidence`.
- Use it to perform the action, assert the expected post-state, and capture a screenshot.
- Give every evidence label a descriptive name such as `country-selected-post-state`.
- Never rely on the generic end-of-test screenshot as proof that an action succeeded.
- For native selectors, verify the selected value changed; opening the OS menu is not screenshot evidence.
- For custom pickers, verify that the option list is visible and contains the expected options.
- After language navigation, verify the target URL plus a target-language title and visible heading before capture.
- A CTA observation must verify visible CTA text and a visible button; attachment alone is invalid.
- Generic Playwright screenshots are diagnostic only and must not be reported as action evidence.
- If the page does not expose enough information to prove the intended behavior, write a skipped test
  titled `NOT TESTABLE: <reason>` rather than guessing or asserting only that an element is attached.
- A behavioral test that only checks `toBeAttached()` is invalid and must be regenerated.

## Structure rules

- Handle the cookie banner in `beforeEach` (click "Allow all" if present), guarded so it
  doesn't fail when the banner is absent.
- Keep tests independent — no test should depend on another test's side effects.
- Name each test after the behavior it checks, matching what it actually asserts.
- Every test title must identify the page-specific target, such as the exact heading, link,
  form field, picker, button, or resulting state observed on that page.
- Do not use generic titles such as `works`, `displays correctly`, `functional navigation`,
  or `selects a country and channel` when the page exposes multiple possible targets.
- Generate a dynamic set of independent smoke tests from the observed targets and outcomes.
  There is no fixed test count or fixed workflow; do not invent extra tests to reach a quota.

## What NOT to test

- External domains (facebook.com, aljazeera.com proper) — only this site.
- Analytics / third-party console noise.
- Anything you did not actually observe on the page.


## Machine-checkable output contract

- Return exactly one complete TypeScript code block; never return prose, multiple alternatives, or a partial draft.
- Treat the supplied live inventory as authoritative. Do not infer controls, success messages, URLs, or workflows from PERSONA text.
- Every generated behavioral test must contain one actionEvidence call whose action callback performs the action and whose verify callback contains the postcondition assertion.
- Every observationEvidence call must contain a meaningful visibility/content/state assertion in its verify callback.
- If no reliable postcondition is observed, generate `test.skip('NOT TESTABLE: <specific reason>', ...)` and do not invent a workflow.
- Keep each test independent and include all setup required for that test.
