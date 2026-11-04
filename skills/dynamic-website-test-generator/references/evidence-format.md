# Evidence format

## In a spec
```ts
import { test, expect } from "@playwright/test";
import { actionEvidence, observationEvidence, evidenceDir } from "./evidence";

test("<behaviour and target>", async ({ page }, testInfo) => {
  const dir = evidenceDir(testInfo);              // evidence/<test title>/
  await page.goto(URL);
  const input = page.getByRole("textbox", { name: "<name>", exact: true });

  await actionEvidence(page, "<label>", async () => {   // the action
    await input.fill("x");
  }, async () => {                                       // the assertion, INSIDE verify
    await expect(input).toHaveValue("x");
  }, dir);

  await observationEvidence(page, "<label>", async () => {   // a pure check, no action
    await expect(page.getByRole("heading", { name: "<name>", exact: true })).toBeVisible();
  }, dir);
});
```
- Every action (`click, fill, selectOption, check, uncheck, press`) goes through `actionEvidence`, including setup steps.
- The screenshot is taken **after** the verify callback passed, so a screenshot proves the state that was asserted.
- `actionEvidence` retries the action once after a Playwright timeout (a not-yet-interactable element), never more.
- Labels are descriptive and page-specific (`country-selected-post-state`), not `after-click`. A native select: verify the
  selected value; a custom picker: verify the option list is visible and contains the option.
- A test with nothing it can prove: `test.skip(true, "NOT TESTABLE: ...")`.

## On disk
```
evidence/
  <test title>/<label>.png        one full-page screenshot per action or observation
  _playwright/<test>/trace.zip    one trace per test (config: trace "on")
  results.json                    Playwright JSON results
  index.json                      per test: title, status, error, screenshots, trace
  REPORT.md                       human summary produced by summarize-evidence.mjs
exploration/
  <slug>.json   <slug>.aria.txt   what the pages looked like when the tests were written
```
Set `EVIDENCE_DIR` to put the screenshots elsewhere. Open a trace with `npx playwright show-trace <trace.zip>`.
Screenshots may contain whatever the page shows (names, e-mails): do not publish the evidence folder without looking at it.
