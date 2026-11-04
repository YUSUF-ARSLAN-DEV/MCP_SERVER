# Selector strategy

Rules learned from repeated real-world failures. They exist because ignoring them caused flaky or wrong tests.

## Order of preference
1. `page.getByRole("<role>", { name: "<accessible name>", exact: true })`
2. `page.getByLabel("...", { exact: true })`, `getByPlaceholder`, `getByTestId`
3. A stable attribute locator: `page.locator('a[href="/map"]')`
4. Never: `getByText`, `:has-text`, `text=`, XPath, positional `nth(3)`, auto-generated ids.

## Names
- Copy the accessible name **verbatim** from the inventory or the ARIA snapshot. Never guess, translate or paraphrase.
- Always `exact: true` with a name. A partial match on a busy page (header + footer + widget) matches several elements and
  raises a strict-mode violation.
- Never use one shared word or an OR-regex as a name.
- Non-English pages: use the real names you observed; do not reuse English labels.

## Ambiguous and repeated controls
- Names like "News", "Privacy Policy" or "Next" appear in the nav, the footer and inline. Scope them to a landmark
  (`getByRole("navigation")`, `getByRole("contentinfo")`) and add `.first()`; many pages render the nav twice (desktop and mobile).
- Put `.first()` on the locator line. Use `toHaveCount(n)` only when you observed `n`.

## Selects and pickers
- A native `<select>` often has an accessible name made of all its options. Locate it by id or a stable selector from the
  inventory, then `selectOption`, and verify the selected value changed.
- A custom dropdown button shows the **choice** once you pick one ("2 selected"), so a locator built from the old caption no
  longer matches. After choosing, assert the *result* of the choice (results table, URL, next control), not the old button.
- The option rows of such a picker are often visually hidden: open the menu with its button and pick by text, or assert only
  `toHaveCount(1)` for an option.

## Hidden and responsive elements
- Skip links and `sr-only` elements are present but not visible: assert `toHaveCount(1)`.
- Below the fold: `await locator.scrollIntoViewIfNeeded()` before asserting visibility.
- Mobile-only controls (hamburger): `await page.setViewportSize({ width: 375, height: 667 })` first.
- Controls with `volatileId: true`: never `locator("#id")`.

## Real roles only
`button, link, heading, textbox, combobox, checkbox, radio, navigation, banner, contentinfo, list, listitem, dialog, region,
img, ...`. There is no `video` role: locate a player by id or an observed control.
