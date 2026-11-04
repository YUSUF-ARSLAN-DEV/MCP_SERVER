# Reading the exploration inventory

`explore.mjs` writes one JSON file per page. Everything you may assert comes from it.

| Field | Meaning | What to do with it |
| --- | --- | --- |
| `controls[].role` / `name` | role and accessible name as the page exposes them | copy `name` verbatim into `getByRole(role, { name, exact: true })` |
| `controls[].region` | `content` (inside main/article), `chrome` (header/nav/footer), `other` | many sites have no `<main>`: treat `other` like `content`. Test `content`/`other`; write at most one `chrome` test |
| `controls[].volatileId` | the `id` looks auto-generated (digits, hashes, `:r1:`) | never locate by that id; use role, label or placeholder |
| `controls[].ambiguous` | the same role+name appears more than once | scope it (`getByRole("navigation")...`) and/or add `.first()` |
| `controls[].visible` | rendered with a size | if false, do not assert `toBeVisible()`; assert `toHaveCount(1)` |
| `controls[].disabled` / `required` / `readonly` | state at load | only assert a disabled state if the inventory shows it; never fill a readonly field |
| `controls[].inForm` | the control is inside a `<form>` | decides empty-submit validation tests (below) |
| `headings[].hiddenByDesign` | screen-reader-only heading | assert `toHaveCount(1)`, not visible |
| `headings[].inFeed` | inside an article or feed | its text rotates: never assert it |
| `forms[]` | field counts, required fields, submit button, `asksPersonalData`, `hasCaptcha` | a form that asks personal data or has a CAPTCHA is not submitted; list it as "needs a person" |
| `revealedByInteraction[]` | with `--probe`: trigger, effect (`reveals`, `no-visible-result`, `not-clickable`), new controls | for `reveals`, test "click trigger, assert one of the new controls is visible" |
| `blockedOtherOrigins` | other hosts the page tried to call (blocked) | informational: third-party scripts were not loaded |
| `notes` | page errors, off-list redirects | mention in the report; an off-list redirect means the page left the user's origin |

`<slug>.aria.txt` is the accessibility snapshot. Use it to confirm the exact accessible name and the page structure.

## What a page does not tell you
Result text after a search, a success message after a submit, prices, permissions. If a behaviour was not observed (no
`--probe` effect, nothing in the snapshot), you may test that the control exists, but you may not assert what it produces.
Say so in the report, or write the test as `NOT TESTABLE`.

## Forms
- Required fields with a client-side error region observed (via probe or user instruction): submit empty and assert an error
  *region or state* (`[role=alert]`, `aria-invalid="true"`), never the message text.
- Search or filter form (a select plus a button): choose a real listed option, submit, assert a results region or heading;
  never individual result rows.
- Anything that asks for e-mail, phone, password or card: do not submit it.
