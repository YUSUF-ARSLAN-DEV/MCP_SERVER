# Report rules (enforced in code)

The decision report is built from one data object (`report-data.json`). A rule that is broken raises
`ReportConsistencyError` and **no document is written** (the CLI prints `REPORT REFUSED: <reason>` and exits 1).
The rules live in `website_test_pipeline/report_policy.py`; the document-level checks in `report.py:_verify_document`.

## Types of item (section 5), each with its own ID prefix
| Type | ID | Meaning | Affects verdict |
|---|---|---|---|
| Defect | DEF | something ran and was broken | yes |
| Unverified | UNV | the journey could not be confirmed either way | CONDITIONAL GO |
| Limitation | LIM | the tooling cannot test it (CAPTCHA, sign-in) | no |
| Test defect | TST | the test itself is wrong (same expected/observed URL; stated expectation differs from the assertion) | no, and left out of the failure count |
| Tooling issue | TOOL | harness failure: skipped test, no spec generated | no |

Verdict: any open High/Critical Defect is NO-GO; otherwise any other open Defect or any open Unverified is CONDITIONAL GO;
otherwise GO. Only a High/Critical Defect is ever a "release blocker".

## Severity (from user impact, not failure class)
Core page element not rendered = Critical; wrong address = High; secondary/cosmetic = Medium; Unverified = business value of
the journey (default Medium); Limitation, test defect, tooling = Low and never P1. A run whose Defects and Unverified items
all share one priority is rejected.

## What you must configure
| Setting | Purpose | If missing |
|---|---|---|
| `REPORT_OWNER` or `owners.json` in the run folder (`default`, `by_category`, `by_url`) | a real name on every action | **report refused** |
| `REPORT_RELEASE_DATE` / `owners.json: release_date` | caps every due date (ISO) | due = run date + 3/7/14 days for P1/P2/P3 |
| `APP_BUILD_ID` | build or commit SHA of the **site under test** | looked up from the site (header, `<meta>`, `/version.json`); else `NOT CAPTURED` with the reason |
| `CI_JOB_URL` (or GitHub/Jenkins/Circle/Buildkite variables) | CI run link | `NOT CAPTURED` |
| `ARTIFACT_BASE_URL`, `ARTIFACT_RETENTION_DAYS` | evidence links become absolute; retention window | links stay relative to the report folder; reason printed |

`owners.json` example: `{"default": "Dana Reviewer", "by_category": {"limitation": "Site owner"}, "by_url": {"https://x/y": "Sam"}}`

## Run-over-run
`artifacts/report/run-history.json` keeps the last 30 runs. Section 4 compares with the previous one (added, removed with a
reason, now passing, still failing, new failures) and flags a browser or Playwright version change with its direction.
