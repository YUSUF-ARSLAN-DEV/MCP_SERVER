# Plan: Flow Testing (journeys, not just pages)

_Rewritten 2026-09-20 to match what was actually built. The first version of this
plan (a `multi_hop_flows` flag threaded through explorer/generator/validator) was
superseded by a separate **flows layer**; it is still in git history if needed._

## Goal

The pipeline used to test **pages**: one `PageInventory`, one generated spec per
URL. A real user does **journeys**: start on page A, act, land on page B, keep
going, and end in an observable state. This plan builds journey testing on top of
the existing pipeline **without giving up its core guarantee: nothing in a test
is guessed.** Every selector, step and outcome must have been observed in a real
browser by code, not predicted by a model.

## Design principles (do not break these)

1. **Flows are data.** A flow is a JSON record (`runs/<site>/flows.json`), not a
   prompt. Anything can add one (explorer, model, human); one code path checks it.
2. **Models suggest, code decides.** A model may *propose* and *rate* flows. Code
   rejects any flow naming a page/control/outcome the explorer never saw. Only a
   real browser run can mark a flow `verified`.
3. **Observed beats predicted.** `verify` records what *actually* happened (DOM
   snapshot diff per step). Assertions in generated tests come from
   `flow["observed"]`, never from the model's guess.
4. **Humans always win.** `approved` / `rejected` statuses and `source: human`
   ratings are never overwritten by any command.
5. **History, not opinions.** `flow_ratings.json` is append-only: who judged, with
   what model + prompt version, when, and why.
6. **Additive.** Existing per-page generate/execute/report keep working unchanged.

## What exists today (steps 1-15 done, all pushed to `origin/master`)

The complete reference (commands, file formats, statuses, exit codes, troubleshooting, limits) is
**[docs/FLOWS.md](docs/FLOWS.md)**. `tests_python/test_docs.py` fails if it names a command, stage, status or
heuristics key that does not exist, so it cannot drift from the code. This plan keeps the history and the reasoning.

```
intents.json -> expand -> flows.json -> verify -> flowgen -> tests/flow_*_test.py -> execute / report
(sentences)     (AI+code)  (steps)       (browser)  (template)                        (results feed back)
```

| Step | What it gave us | Commit(s) |
|------|-----------------|-----------|
| 1-7 | explorer flows, sitemap, `propose`, critic + ratings, `verify` | `d91348a` ... `f485186` |
| 8 | `flowgen`: validated pytest specs from verified flows, no model | `60bc16c` |
| 9 | pytest results feed the ratings; `derive_status`, `stale` | `dac7d87` |
| 10 | multi-page journeys: per-step URLs, hop-aware errors, per-hop assertions | `a99c215` `52927d6` `1b7b682` `cc82ca1` |
| 11 | `flows list/show/approve/reject/reset` | `1692716` |
| 11b-f | the intent track: plain sentences first (`intents`, `expand`, `flows add/edit/drop`), multiselect, query assertions, sync | `ab2b1a4` `fd723ad` `a07c162` `f0ab857` `9959615` |
| 12 | report: User flows section, per-step failure attribution | `f44e1b7` |
| fixes | results-loaded / weak-outcome guards; one heuristics file for any site or language | `218e0f7` `3d63c62` |
| 13 | `verify` selection, control healing, option healing, explorer records 300 options | `bb58745` `9bca6da` `97c5d4b` |
| 14 | coverage metric, gaps fed to the AI, report section, `flows run`; an unreachable site is not a failed flow | `25da4a8` `fb2c52e` `0e12699` `474b4ed` `5c27beb` |
| 15 | `docs/FLOWS.md`, docs tests, this plan refreshed | this commit |

Commands: `crawl`, `explore`, `generate`, `propose`, `intents`, `expand`, `verify`, `flowgen`, `flows`, `execute`, `report`.
Every step is tagged `flows-step-<n>` (`git checkout flows-step-13b`, `git revert <commit>`).

## Working protocol for every remaining step

The user says "do step N" (or "next"). For each step:

1. **Orient:** `graphify query` on the area, read only what the step touches.
2. **Tests first** in `tests_python/` for the pure logic (the suite is ~0.2 s).
3. **Implement**, matching surrounding style. Keep the step small enough to revert alone.
4. **`python -m pytest tests_python -q` must be green** before committing.
5. **Real check where the step touches a browser or model:** run on the existing
   `runs/sat-stg.aljazeera.tv` data, capped at **3 URLs** for anything that crawls or
   explores (`verify`/`flowgen` on the 8 saved flows is cheap and fine). Report
   the real numbers. If a real run cannot be done, say so plainly.
6. **Any bug hit gets a short Bug / Cause note** (what it was, why it happened).
7. `graphify update .`, tick the step in the **Roadmap status** table below.
8. **One commit per step, never bundled.** Message = `type(flows): summary`, then a
   body with **Why / What / How verified**, then the Co-Authored-By trailer.
   (Avoid backticks in heredoc commit messages.)
9. Look at `git diff --cached` for secrets before pushing (a key leaked once; see
   history rewrite of 2026-08-31). `runs/` and `.env` stay git-ignored.
10. `git push origin master`, then `git tag flows-step-N` + `git push origin flows-step-N`
    so any step is one `git checkout` / `git revert` away.
11. **Report in <=100 words:** what changed, test result, real-run result, next step.

Rollback: `git revert <step commit>` undoes exactly one step; nothing later depends
on an earlier step's internals except through the JSON formats above.

---

## Step details (history; everything except step 16 is done)

### Step 8 - Emit a pytest spec from a verified flow (deterministic, no model) - DONE

**Why:** a verified flow already contains every selector, value and observed
outcome. Turning it into code with a *template* means zero hallucination and no
retry loop - the strongest form of the "nothing guessed" rule.

**Files:** new `website_test_pipeline/flowgen.py`; `validator.py` (accept a flow);
`cli.py` (new `flowgen` command); `tests_python/test_flowgen.py`.

- Input: flows with status `verified` or `approved` (never `candidate`/`rejected`).
- Output: `runs/<site>/tests/flow_<id>_test.py`, one test per flow: open start URL via
  `open_page`, then each step wrapped in `action_evidence` with kebab labels
  (`01-select-country`, `02-search`), mirroring how `runner._do_step` acts
  (selector if present, else role+name lookup; select by label; fill; click).
- Assertions come only from `flow["observed"]`:
  `navigates` -> `to_have_url(re.compile(<path>))`; `new_headings` -> one short
  heading (<=6 words, else a distinctive-word regex) visible; `results` -> results
  region visible. Never exact row counts or rotating text. `no-visible-change`
  flows are not emitted.
- Names stored cut to 40 chars: locate by substring (`exact=False`), and teach the
  validator that flow specs may do so. Build a synthetic inventory-like token set
  from the flow so `validate_python_spec` keeps full strictness.
- Every emitted spec must pass `validate_python_spec`; a flow that cannot is
  logged and skipped, not written.

**Verify:** unit tests (emit -> validate for each effect type; refuses candidates);
run `flowgen` on the 8 sat-stg flows, read 2 by hand, `pytest` the flow specs.
**Commit:** `feat(flows): flowgen - emit validated pytest specs from verified flows`

### Step 9 - Execution results feed the ratings - DONE

**Why:** "verified" should mean the flow works *and* its generated test passes.
**Files:** `report.py`/`cli.py` (execute path), `ratings.py` (helper), tests.

- After `execute`/`report`, map pytest results of `flow_*_test.py` back to flow ids
  and append `{source: "pytest", passed, at, test}` to `flow_ratings.json`.
- Add `derive_status(flow, ratings)`: latest evidence wins; human statuses never
  change; a previously `verified` flow whose latest run failed becomes `stale`.
  (`stale` is new; only execution can move a flow out of `candidate`.)

**Verify:** unit tests for `derive_status`; run `execute` on the flow specs and
check ratings gained pytest entries. **Commit:** `feat(flows): pytest results feed flow_ratings and status`

### Step 10 - Multi-page journeys (the original goal, now cheap)

**Why:** the proposer already sees every explored page, and `verify` already runs
steps on one continuous `page`, so cross-page flows mostly exist at the data level.
What is missing is *hop awareness*: nothing checks the flow is on the page each
step expects.
**Files:** `runner.py`, `proposer.py`/`critic.py` prompts, `flowgen.py`, tests.

- Runner: when a step fails on a later hop, the error says which page the step expects and
  where the browser is (`hop 2: step expects page /x, browser is on /y`). It is NOT a
  pre-step mismatch check: a working flow can legitimately differ from the recorded page
  (sat-stg redirects `/` to `/en`), so that would fail good flows.
- Record `step_urls` in `observed` so each hop's landing page is known.
- Flowgen: emit `expect(page).to_have_url(re.compile(...))` after each step that
  observed a navigation, so a failure names the hop that broke.
- Propose prompt: explicitly encourage chains across linked pages (A -> B -> C),
  bounded by the existing `MAX_STEPS`. No feature flag needed.

**Verify:** unit tests (drift detection, per-hop asserts); `propose` then `verify`
on sat-stg; confirm at least one multi-page flow is proposed, verified or cleanly
rejected. **Commit:** `feat(flows): hop-aware runner and specs for multi-page journeys`

### Step 11 - Human review commands - DONE

**Why:** today approving/rejecting a flow means hand-editing JSON.
**Files:** `cli.py` (`flows` subcommands), `flows.py`, `ratings.py`, tests.

- `flows list` (id, status, goal, last result), `flows show <id>` (steps, observed,
  rating history), `flows approve <id>`, `flows reject <id> --reason "..."`.
- Each decision sets the human status **and** appends a `source: human` rating with
  the reason. `flows add` is out of scope until needed.

**Verify:** unit tests; approve/reject one real flow and confirm `flowgen` respects it.
**Commit:** `feat(flows): flows list/show/approve/reject with recorded reasons`

### Steps 11b-11f - The intent track: plain sentences first

Added 2026-09-20 after Step 11. A human should not write steps like a machine. The
starting point becomes a file of **plain sentences** (`runs/<site>/intents.json`) that the
AI populates first and a person can add to or edit. A second AI pass expands each
sentence into concrete steps from the page DOM; code rejects anything invented; `verify`
and `flowgen` then work exactly as before. The sentence stays the source of truth, so a
broken step can be re-expanded from it.

```
intents.json (sentences) -> expand (AI + code validation) -> flows.json (candidate)
   -> verify (real browser) -> flowgen (only verified/approved) -> pytest
```

Guarantees that keep it safe: new files/commands only (existing commands ignore intents);
expanded flows enter as `candidate` and need a real verified run; a flow that cannot be
built records why on its intent instead of producing a weak test; editing a sentence
demotes its flow so a stale test is never kept.

- **11b** multiselect actually picks the named option (runner + `flowgen`, shared `pick_option`). The old runner only opened the menu.
- **11c** stronger outcome assertion: query-string parameters the run observed after a navigation.
- **11d** `intents.json` store, `flows add/edit/drop/intents`, and `intents` (AI writes the sentences).
- **11e** `expand`: sentence -> steps (reuses `validate_flow` + critic), links `intent_id` to the flow, records why on failure.
- **11f** sync + safeguards (edited sentence demotes its flow), end-to-end acceptance run, docs.

### Step 12 - Report: a Flows section - DONE

**Why:** flow results should be readable next to the page reports.
**Files:** `report.py`, tests.

- Per flow: goal, steps, hop URLs, predicted vs observed outcome, status, rating
  history, latest pytest result, and *which step/hop* failed.
- Flow tests count as behavioural (never flagged "shallow"); filed under the
  flow's start URL, other hops listed as evidence.

**Verify:** unit tests; `report` on sat-stg and open the `.docx`.
**Commit:** `feat(report): flows section with per-hop failure attribution`

### Step 13 - Staleness and healing - DONE

**Why:** sites change; a flow whose control disappeared should be flagged, then fixed.
**Files:** `runner.py`/`flows.py`, `cli.py`, tests.

- `verify --failed-only` re-runs only stale/candidate flows.
- On "control not found", look the control up by name/role in a fresh inventory of
  that page; if exactly one match, propose the healed step, re-verify, and only
  then update the flow (old step kept in history). Otherwise mark `stale`.
- Deterministic first; a model is only a suggestion source, never the judge.

**Verify:** unit tests with a renamed selector; real re-verify on sat-stg.
**Commit:** `feat(flows): stale detection and deterministic selector healing`

### Step 14 - Flow coverage and a closed loop - DONE

**Why:** know what journeys are still untested and let `propose` target them.
**Files:** `sitemap.py`, `proposer.py`, `report.py`, tests.

- Coverage = explored pages/controls touched by at least one verified flow.
- Feed the uncovered list into the propose prompt; show coverage in the report.
- One command `flows run` chains propose -> verify -> flowgen -> execute for a site
  (each stage still independently runnable and skippable).

**Verify:** unit tests; run the chain on sat-stg (<=3 URLs if it explores).
**Commit:** `feat(flows): flow coverage metric and propose-verify-flowgen-execute chain`

### Step 15 - Docs and cleanup - DONE

- **`docs/FLOWS.md`**: the reference for the whole flow workflow (pipeline, stages, every command, exit codes, the
  four file formats field by field, statuses and what changes them, what a test asserts, healing, coverage,
  outages, troubleshooting, known limits, how to develop and test it).
- **`tests_python/test_docs.py`**: every command, `flows` subcommand, stage, status and heuristics key the docs name
  must exist, and the documented `heuristics.json` example must load without a warning. (`review.SUBCOMMANDS` is the
  list the docs are checked against.)
- **README**: links to the reference; flow limits added to Known limitations.
- **Left alone on purpose:** `AI-TEST-GUIDE.md` is the prompt prefix the model receives when it writes *page* specs,
  so flow documentation there would change those prompts (flow specs are templated, not model-written).
  `EXPLAIN.md` is the user's own learning notes.
- **Decision on the two extra steps in the original list:** removing the multi-hop flag / single-hop paths is moot,
  because the flag was never built (the flows layer replaced it) and single-page testing is kept as designed.

### Step 16 - LAST: screenshots/vision and attached documents

Explicitly last, per the user. (a) Vision: send `evidence/*.png` to a vision-capable
model to *rate* a flow's end state (rating only, never marks verified). (b) Attached
requirement documents: extract user stories and feed them to `propose` as extra
evidence; code still rejects anything the explorer did not observe. Scope this in
detail when we reach it.

---

## Roadmap status

| Step | Status |
|------|--------|
| 1-7 (explorer flows, sitemap, propose, critic/ratings, verify) | done, pushed |
| 8 flowgen | done, pushed (`flowgen` command; all 8 sat-stg flows emit and pass live) |
| 9 results feed ratings | done, pushed (`flowresults.py`, `derive_status`, `stale`) |
| 10 multi-page hop awareness (4 commits) | done: |
| &nbsp;&nbsp;10a runner records landed_url + step_urls | done, pushed |
| &nbsp;&nbsp;10b hop-aware step errors | done, pushed |
| &nbsp;&nbsp;10c flowgen asserts every hop URL | done, pushed |
| &nbsp;&nbsp;10d propose encourages cross-page chains | done, pushed |
| 11 human review commands | done, pushed (`flows list/show/approve/reject/reset`, `review.py`) |
| 11b multiselect picks the named option | done, pushed |
| 11c query-string outcome assertion | done, pushed |
| 11d intents store + commands + AI sentences | done, pushed |
| 11e expand sentence -> steps | done, pushed |
| 11f sync, safeguards, acceptance, docs | done, pushed |
| 12 report flows section | done, pushed (`flowreport.py`, User flows section, failure attribution) |
| 13 staleness + healing (3 commits) | done: |
| &nbsp;&nbsp;13a `verify [id...] [--failed-only]` | done, pushed |
| &nbsp;&nbsp;13b heal a control that moved or was renamed | done, pushed |
| &nbsp;&nbsp;13c heal a default option that returns no content (+ explorer records up to 300 options) | done, pushed |
| 14 flow coverage + closed loop (4 commits, plus an outage fix) | done: |
| &nbsp;&nbsp;14a coverage metric + `flows coverage` | done, pushed |
| &nbsp;&nbsp;14b uncovered areas fed into the `intents` / `propose` prompts | done, pushed |
| &nbsp;&nbsp;14c coverage section in the Word report | done, pushed |
| &nbsp;&nbsp;14d `flows run` chain (intents, expand, verify, flowgen, execute) | done, pushed |
| 15 docs and cleanup | done, pushed (`docs/FLOWS.md`, `test_docs.py`) |
| 16 attached documents + vision (2 commits) | in progress: |
| &nbsp;&nbsp;16a `intents <file>`: journeys from requirement documents, each with a verified quote | done, pushed |
| &nbsp;&nbsp;16b `flows judge`: a vision model rates the final screenshot (never changes status) | todo |

## Non-goals

- Login / credentials / manual-login flows (independent; can land any time).
- Flows needing payment or a real person's data (the proposer already forbids them).
- Replacing per-page generation: pages with no chainable action keep single-page tests.

## Known limits

Maintained in [docs/FLOWS.md](docs/FLOWS.md) section 11 (the earlier per-step limits listed here were all resolved).

## Open questions (all decided; kept for the reasoning)

1. **Truncated (40-char) control names:** flows keep the cut name; the runner and the generated spec match the whole
   name, or the start of a name stored at the cut, so both behave the same way.
2. **When does a flow go `stale`:** after two failed executions in a row (runner or pytest, any mix); one failure is
   tolerated. A definite failure demotes to `candidate` at once. `stale` flows keep their spec so the failure stays loud.
3. **Hop depth:** `MAX_STEPS` (8) per flow; no per-site override was needed.
4. **How far may healing go:** it heals only when exactly one control can stand in, keeps the change only if the whole
   run passes, records the old values, and never rewrites an `approved` or `rejected` flow (it names the candidate instead).
