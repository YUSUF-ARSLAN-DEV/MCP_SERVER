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

## What exists today (done, all pushed to `origin/master`)

| # | Step | Commit | What it gave us |
|---|------|--------|-----------------|
| 1 | Explorer records flows | `d91348a` | `flows.json` format (`flows.py`); `explore` writes each verified primary flow via `record_flow()` |
| 2 | Menu-close probe fix | `3c83a9a` | Open widget menus no longer block the Search click |
| 3 | Site map | `8e72238` | `sitemap.py`: deterministic evidence pack (pages, controls, links between pages) |
| 4 | `propose` | `ed7d23f` | Model suggests flows from the site map; `validate_flow()` rejects invented pages/controls/outcomes; accepted as `candidate` |
| 5 | Smarter proposals | `b7460d9` | Linked-page outcomes, duplicate filter, model critic (`critic.py`), `ratings.py` + `flow_ratings.json` |
| 6 | Single-click rule | `5bdccd9` | A 1-step flow is valid only if it navigates |
| 7 | `verify` | `f485186` | `runner.py`: run each flow step by step, snapshot after every step, diff, classify (`navigates`/`results`/`reveals`/`no-visible-change`), compare with the prediction, set `verified`/`candidate`, append a runner rating |

Current commands: `crawl`, `explore`, `generate`, `propose`, `verify`, `execute`, `report`.
Real data: `runs/sat-stg.aljazeera.tv/flows.json` (8 flows, all verified) and `flow_ratings.json`.

### Data shapes (source of truth: `flows.py`, `ratings.py`, `runner.py`)

```
flow = {id, goal, source: explorer|model|human, status: candidate|verified|approved|rejected,
        start_url, steps: [{kind: click|select|fill|multiselect|submit, selector, name, value, page?}],
        outcome: {effect: navigates|results|reveals|validation, to?}, evidence?, proposed_by?,
        observed?: {effect, url, new_headings, new_controls, results, step_effects}, last_run_at?}
rating entry = {source: model|runner|human|pytest, at, ...}
   model : scores{coherence, importance, outcome_strength}, reason, kept, model, prompt_version
   runner: passed, checks{steps_completed, outcome_matched, observed_effect}, error?
```

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

## Remaining steps

### Step 8 - Emit a pytest spec from a verified flow (deterministic, no model)

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

### Step 9 - Execution results feed the ratings

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

- Runner: before each step, compare the current path with the step's `page`; on a
  mismatch fail with `expected /x, was on /y` (a clear hop-level error).
- Record `step_urls` in `observed` so each hop's landing page is known.
- Flowgen: emit `expect(page).to_have_url(re.compile(...))` after each step that
  observed a navigation, so a failure names the hop that broke.
- Propose prompt: explicitly encourage chains across linked pages (A -> B -> C),
  bounded by the existing `MAX_STEPS`. No feature flag needed.

**Verify:** unit tests (drift detection, per-hop asserts); `propose` then `verify`
on sat-stg; confirm at least one multi-page flow is proposed, verified or cleanly
rejected. **Commit:** `feat(flows): hop-aware runner and specs for multi-page journeys`

### Step 11 - Human review commands

**Why:** today approving/rejecting a flow means hand-editing JSON.
**Files:** `cli.py` (`flows` subcommands), `flows.py`, `ratings.py`, tests.

- `flows list` (id, status, goal, last result), `flows show <id>` (steps, observed,
  rating history), `flows approve <id>`, `flows reject <id> --reason "..."`.
- Each decision sets the human status **and** appends a `source: human` rating with
  the reason. `flows add` is out of scope until needed.

**Verify:** unit tests; approve/reject one real flow and confirm `flowgen` respects it.
**Commit:** `feat(flows): flows list/show/approve/reject with recorded reasons`

### Step 12 - Report: a Flows section

**Why:** flow results should be readable next to the page reports.
**Files:** `report.py`, tests.

- Per flow: goal, steps, hop URLs, predicted vs observed outcome, status, rating
  history, latest pytest result, and *which step/hop* failed.
- Flow tests count as behavioural (never flagged "shallow"); filed under the
  flow's start URL, other hops listed as evidence.

**Verify:** unit tests; `report` on sat-stg and open the `.docx`.
**Commit:** `feat(report): flows section with per-hop failure attribution`

### Step 13 - Staleness and healing

**Why:** sites change; a flow whose control disappeared should be flagged, then fixed.
**Files:** `runner.py`/`flows.py`, `cli.py`, tests.

- `verify --failed-only` re-runs only stale/candidate flows.
- On "control not found", look the control up by name/role in a fresh inventory of
  that page; if exactly one match, propose the healed step, re-verify, and only
  then update the flow (old step kept in history). Otherwise mark `stale`.
- Deterministic first; a model is only a suggestion source, never the judge.

**Verify:** unit tests with a renamed selector; real re-verify on sat-stg.
**Commit:** `feat(flows): stale detection and deterministic selector healing`

### Step 14 - Flow coverage and a closed loop

**Why:** know what journeys are still untested and let `propose` target them.
**Files:** `sitemap.py`, `proposer.py`, `report.py`, tests.

- Coverage = explored pages/controls touched by at least one verified flow.
- Feed the uncovered list into the propose prompt; show coverage in the report.
- One command `flows run` chains propose -> verify -> flowgen -> execute for a site
  (each stage still independently runnable and skippable).

**Verify:** unit tests; run the chain on sat-stg (<=3 URLs if it explores).
**Commit:** `feat(flows): flow coverage metric and propose-verify-flowgen-execute chain`

### Step 15 - Docs and cleanup

`README.md`, `EXPLAIN.md`, and `AI-TEST-GUIDE.md` (flow specs section); document
the flow/ratings formats and the human-override rules; `graphify update .`; note
known limits (no login/credentials, see non-goals). **Commit:** `docs(flows): document the flows workflow`

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
| 8 flowgen | todo |
| 9 results feed ratings | todo |
| 10 multi-page hop awareness | todo |
| 11 human review commands | todo |
| 12 report flows section | todo |
| 13 staleness + healing | todo |
| 14 coverage + chain | todo |
| 15 docs | todo |
| 16 vision + attached docs | todo (last) |

## Non-goals

- Login / credentials / manual-login flows (independent; can land any time).
- Flows needing payment or a real person's data (the proposer already forbids them).
- Replacing per-page generation: pages with no chainable action keep single-page tests.

## Open questions (decide at the step where they matter)

1. **Step 8:** truncated (40-char) control names - substring match vs storing the full
   name in `flows.json`. Leaning: store full names going forward, substring for old data.
2. **Step 9:** should a failing flow *test* demote `verified` immediately, or only after
   two consecutive failures (flaky-network tolerance)? Leaning: two.
3. **Step 10:** max hop depth is `MAX_STEPS` (8) today; per-site override needed?
4. **Step 13:** how far may healing go before a human must approve the changed step?
   Leaning: never auto-heal an `approved` flow; propose the change instead.
