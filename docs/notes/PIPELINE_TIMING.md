# Website Test Pipeline: System Walkthrough + Timing Model

This document has two jobs: (1) explain what this system actually is and how its pieces hand off to each
other, and (2) explain how long each piece takes on average, and why. It is written to be self-contained -
no other context about the codebase is assumed. Numbers are calibrated from a real run (`sat-stg.aljazeera.tv`,
13 pages, 2026-10-04) but describe the *general* behavior of the pipeline, not that one run's clock time.

---

## Part 1 - What this system is

It is an automated QA pipeline: give it one seed URL, and it discovers a site's pages, writes and repairs
real browser test code for them (using an LLM), discovers and verifies multi-step user journeys ("flows":
e.g. "pick a country, click Search, see results"), executes everything in a real browser, and produces a
Word QA report with a release recommendation (GO / CONDITIONAL GO / NO-GO).

### 1.1 The four stages, in order

```
 SEED URL
    |
    v
+-----------+        urls.txt         +------------+       tests/*.py        +------------+
|   CRAWL   | ----------------------> |  GENERATE  | -----------------------> | FLOWS RUN  |
+-----------+                         +------------+                         +------------+
  navigate &                         per page: probe        also writes/updates                |
  discover links                     controls, call LLM     flows.json (candidate               |
  (no LLM call)                      to write a test spec,   "explorer" flows found              |
                                      auto-repair it          while probing)                      |
                                                                                                    v
                                                                                     +-----------------------+
                                                                                     | intents -> expand ->  |
                                                                                     | verify -> flowgen     |
                                                                                     +-----------------------+
                                                                                     intents.json (plain-sentence
                                                                                     requirements) -> LLM expands
                                                                                     into concrete steps -> each
                                                                                     candidate flow is RUN LIVE
                                                                                     in a browser and judged ->
                                                                                     passing flows get rendered
                                                                                     into their own test file
                                                                                                    |
                                                                                                    v
                                                                                        flows.json (updated),
                                                                                        flow_ratings.json (history)
                                                                                                    |
                                                                                                    v
                                                                                     +------------------------+
                                                                                     |        REPORT         |
                                                                                     |  (--rerun executes    |
                                                                                     |   every test first)   |
                                                                                     +------------------------+
                                                                                     reads test_results.json +
                                                                                     flows.json + flow_ratings.json
                                                                                     + page inventories, builds
                                                                                     report-data.json (validated
                                                                                     JSON), renders .docx files
                                                                                                    |
                                                                                                    v
                                                                                        full-report.docx,
                                                                                        per-page .docx files,
                                                                                        full-report-appendix.docx
```

### 1.2 What each stage actually does, and what it hands the next one

| # | Stage | Input | What happens | Output (handed to next stage) | Calls the model? |
|---|---|---|---|---|---|
| 1 | **Crawl** | seed URL | Opens the seed page, follows same-site links breadth-first up to a page/depth limit, dedupes redirect aliases | `urls.txt` (list of discovered page URLs) | No |
| 2 | **Generate** | `urls.txt` | For each page: takes a snapshot of its controls/headings, clicks a few interactive triggers to see what they reveal (the "probe"), writes a page-level test spec via an LLM call, validates it, and if invalid sends it back for auto-repair (one or more extra LLM calls). While probing, if a page has an obvious primary action (a search box + submit, a simple form), that interaction is run live and - if it works - saved immediately as a `"verified"` flow. | `tests/<page>_test.py` files (one per page); `flows.json` gains "explorer"-sourced flows; `heuristics.json` may gain site-specific selector hints | **Yes** - 1+ call per page |
| 3a | **Flows: intents** | `intents.json` (plain-English requirement sentences, e.g. "a visitor can subscribe to alerts") | LLM turns each sentence into a flow skeleton (a goal + a starting page) | updates to `flows.json` (new candidate flows) | Yes |
| 3b | **Flows: expand** | candidate flows from 3a + the page's control inventory | LLM resolves the abstract goal into concrete steps (click X, select Y, fill Z) that reference real controls found on the page | `flows.json` flows now have concrete `steps` | Yes |
| 3c | **Flows: verify** | every candidate/stale flow in `flows.json` | Actually runs each flow's steps in a real Playwright browser, compares what happened to what was predicted (`judge()`), and marks it `verified` (passed), leaves it `candidate` (failed/inconclusive), or `stale` (used to pass, needs rechecking) | `flows.json` statuses updated; `flow_ratings.json` gets one history entry per flow per attempt | **No** (pure browser) |
| 3d | **Flows: flowgen** | verified/approved/stale flows | Renders each flow's steps into an actual pytest file - pure templating, no LLM | `tests/flow_<id>_test.py` files | No |
| 4 | **Report** (`--rerun`) | every file in `tests/` | First executes *all* of them (page tests + flow tests) in a real browser (`execute`), recording pass/fail/evidence; then reads that alongside `flows.json`/`flow_ratings.json`/page inventories, builds one canonical validated JSON (`report-data.json`), and renders it into Word documents | `test_results.json`, `report-data.json`, `full-report.docx` (+ per-page docs + appendix) | No |

### 1.3 The key files that connect the stages

| File | Written by | Read by | Contains |
|---|---|---|---|
| `runs/<site>/urls.txt` | Crawl | Generate | discovered page URLs |
| `runs/<site>/tests/*.py` | Generate, Flowgen | Report (execute) | real pytest/Playwright test code |
| `runs/<site>/flows.json` | Generate (explorer flows), intents, expand, verify | expand, verify, flowgen, Report | every flow's goal, steps, status, last observed outcome |
| `runs/<site>/flow_ratings.json` | verify | Report (defect/severity logic) | one history entry per verify attempt per flow (pass/fail, reason) |
| `runs/<site>/intents.json` | (authored, or an earlier `intents` pass) | intents, expand | plain-English requirement sentences |
| `runs/<site>/heuristics.json` | Generate (autorepair) | Generate, flow steps | site-specific selector/menu hints learned along the way |
| `runs/<site>/artifacts/test_results.json` | Report's `execute` sub-step | Report's render step | every test's pass/fail/evidence paths |
| `runs/<site>/artifacts/report/report-data.json` | Report | Report's docx renderer (and anyone auditing the numbers) | the single validated source of truth: counts, defects, recommendation, coverage |
| `runs/<site>/artifacts/report/*.docx` | Report | a human reader | the final deliverable |

### 1.4 Why the structure is shaped this way

- **Generate and Flows are separate** because a page-level test ("the heading is visible") and a flow
  ("a visitor can complete a multi-step journey") are different claims needing different evidence - a flow
  must actually be driven end-to-end and judged, not just written and trusted.
- **verify happens before flowgen** so a test file only ever gets written for a flow that has already been
  proven to work live - the pipeline does not generate tests for journeys it hasn't confirmed.
- **Report always re-executes everything** (`--rerun`) rather than trusting old results, specifically so the
  decision in the report reflects the current state of the site, not a stale run.

---

## Part 2 - How long each stage takes, and why

### 2.1 The variables that actually determine duration

| Variable | Meaning | Typical range |
|---|---|---|
| `P` | pages discovered by crawl | 5-100+ |
| `C` | interactive controls per page worth probing | 2-10 |
| `R` | repair round-trips per page (model's draft fails validation and gets sent back) | 0-4 |
| `F` | flows needing live verification | often 1.5-3x `P` |
| `T` | total generated pytest tests | grows with `P` and `F` together |
| `L` | one model call's latency | 13-26s, ~20s typical |

A 5-page site and a 100-page site run the identical pipeline; `P`, `C`, `F`, `T` just differ by an order of
magnitude, so wall-clock time does too. There is no single "how long does it take" - only the formula below.

### 2.2 The general formula

```
Total ≈ Crawl + Generate + Flows + Report

Crawl    ≈ P × (navigation + settle)              # ~1-3s/page, no model calls - the cheap stage
Generate ≈ P × [ C × probe_cost + (1 + R) × L ]    # dominated by model calls, scales with repairs
Flows    ≈ F × verify_cost + (intents/expand calls, each ≈ L)
Report   ≈ T × test_execution_cost
```

Calibrated constants:
- `probe_cost` ≈ 2.3s (one interactive click + settle-wait)
- `L` (one model call) ≈ 13-26s, mean ~20s
- `verify_cost` ≈ 5.7s/flow (pure browser, no model call)
- `test_execution_cost` ≈ 5.8s/test (pure browser, no model call)

**The key asymmetry:** Generate and the intents/expand part of Flows are bound by model latency
(~20s/call). Crawl, verify, and Report are bound by browser-action latency (2-6s/action) - roughly 10x
cheaper per item. This is why Generate dominates total time even though it touches *fewer* things (pages)
than Report touches (tests): each thing it touches costs far more.

### 2.3 Why `R` (repair round-trips) matters most

`R` is the only variable about *quality*, not size - and it has an outsized effect because each extra
round-trip costs a full model call (13-26s), not a cheap retry. Measured range this run: 0 (clean first
try) to 4 (one page failed validation 5 times straight and was ultimately skipped). A page needing 3
repairs costs roughly as much time as 4 pages that pass first try.

### 2.4 Worked examples (same formula, different site size)

| Site | P | avg C | avg R | F | T | Generate | Flows | Report | Total |
|---|---|---|---|---|---|---|---|---|---|
| Small, simple | 5 | 3 | 0.5 | 6 | 10 | 5×(7+30)=185s | 36s | 60s | **~5 min** |
| This run (aljazeera) | 13 | 5 | 1.5 | 22-30 | 58-73 | 13×(11.5+50)=800s | ~150s | ~400s | **~23 min** |
| Large, form-heavy | 60 | 8 | 2 | 100 | 250 | 60×(18+60)=4680s | 600s | 1500s | **~2 hours** |

The relationship is close to **linear in `P` and `T`**, with a **multiplier on `P` for `R`**. A site 5x the
size takes roughly 5x as long - not because the pipeline slowed down, but because there is 5x more to
generate, verify, and execute.

### 2.5 Calibration data (real measurements behind the constants above)

Captured from one live run (`sat-stg.aljazeera.tv`, 13 pages, 2026-10-04 09:41-09:53):

**Model call latency** (19 real calls, generate stage): mean 20.1s, min 12.9s, max 25.9s, in seconds:
`[23.4, 23.6, 23.8, 13.5, 19.4, 19.3, 12.9, 19.4, 18.1, 18.7, 20.9, 21.1, 14.9, 22.9, 23.7, 19.2, 20.4, 25.9, 21.1]`

**Per-page generate duration** (varies almost entirely with repair count `R`):

| Page | Duration | Model calls (R+1) |
|---|---|---|
| `/` | 50s | 1 |
| `/en` | 86s | 3 |
| `/en/frequency-search` | 70s | 3 |
| `/en/newfrequencies` | 86s+ | 4 |
| `/ar/frequency-search` | 125s | 5 (ended in a model failure after retries - worst case observed) |
| `/en/map` | 29s | 1 (no primary-flow probe needed, fewer controls) |

**Flows verify**: 30 flows in 172s -> 5.7s/flow average (range ~6s for a 1-step flow to ~29s for an
8-step form).

**Report rerun**: 58 tests in 336s -> 5.8s/test average.

---

## Part 3 - Suggested visualizations

1. **Pipeline flow diagram** - boxes for Crawl / Generate / Flows (intents->expand->verify->flowgen) /
   Report, arrows labeled with the file each stage hands to the next (section 1.1/1.3). This is the
   architecture diagram; timing is a secondary annotation on each box (e.g. "~20s/call" on Generate).
2. **Stacked bar per stage** for a given `P`/`T`, using the formula in section 2.2 - shows Generate-stage
   time dominance.
3. **Histogram of the 19 model-call latencies** (section 2.5) - shows the 13-26s spread, mean ~20s.
4. **Line chart: total time vs. `P`** using the worked-example table (section 2.4) - shows near-linear
   scaling with site size.
5. **Bar chart: per-page generate duration vs. repair count `R`** (section 2.5's table) - makes "repairs
   cost the most" visible at a glance.
