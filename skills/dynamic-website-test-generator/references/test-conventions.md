# Test conventions

## Coverage
- Test the page, not the site chrome: at most **one** header/nav/footer test per page.
- A page with one or two content controls gets one or two tests. A short, specific file beats a padded one.
- At least half of the tests should perform an action and assert its result. Aim for a mix: reveal a panel, navigate,
  submit with required fields empty (error region appears), select and search (results region appears).
- Use only what was observed. If a behaviour cannot be proven from the inventory, emit a skipped test with the reason.
- Do not test other domains, analytics or third-party console noise.

## Structure
- File: `tests/<slug>.spec.ts`. Declare the exact page URL once: `const URL = "https://..."` and `page.goto(URL)`.
- Each test is independent: it navigates itself and defines its own locators. No shared state between tests.
- Test names say the behaviour and the page-specific target ("selecting a country enables the channel list"), not
  "works", "navigation" or "displays correctly". One verb per concept (`_present`, not a mix of visible/exists).
- `.first()` on the same line as the locator when a name repeats.
- Imports allowed: `@playwright/test` and `./evidence`. No `fs`, `child_process`, `http`, `net`, `os`, no `fetch`, no `eval`.
- No secrets, no credentials, no environment lookups.

## Assertions
- Use auto-retrying `expect(...)` (`toBeVisible`, `toHaveText`, `toHaveURL`, `toHaveCount`). No fixed sleeps.
- Never assert an exact URL (redirects, locale, trailing slash, tracking params). Use a path regex
  `expect(page).toHaveURL(/\/map/)`, or better a heading unique to the destination.
- Never assert `toBeDisabled()` / `toBeEnabled()` unless the inventory shows that state.
- Do not assert guessed copy: no invented success or error messages, no text of rotating news items, feeds or lists.
- Do not assert a state your own action just changed away (a submit button that becomes "Sending...", a field that clears).
  Assert the genuine post-state: a value you set, a panel that appeared, a destination heading.
- Language switchers usually leave the domain: assert the link is visible, never the resulting URL; prefer not to test it.

## Skips
`test.skip(true, "NOT TESTABLE: <specific reason>")` for: needs a login you were not given, CAPTCHA, result not observable,
form that collects personal data. Never invent a workflow to avoid a skip.
