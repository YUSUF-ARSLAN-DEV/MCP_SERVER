# Plan: Multi-Hop Execution-Flow Testing

## Goal

Right now the pipeline tests **pages**, not **journeys**: each URL gets its own
`PageInventory`, its own generated spec, and (at best) one in-page primary flow
that stops the instant it causes a navigation (`_probe_primary_flow()`,
`website_test_pipeline/explorer.py:710-806`). The result is real coverage of
individual controls, but no test that reproduces what an actual user does:
start on page A, act, land on page B, keep going, land on page C, verify the
end state.

This plan extends the *existing* primary-flow mechanism to chain across pages
instead of stopping at the first navigation, while keeping every other
guarantee the pipeline already has: no hallucinated selectors, no guessed
outcomes, same validator strictness — just applied per hop instead of per
page.

**Non-goal for this plan:** the credentials/manual-login idea discussed
separately. That's independent and can land before, after, or interleaved
with this work without conflict.

## Current architecture (baseline, for reference)

- `crawler.py` — discovers same-origin URLs, writes `urls.txt`.
- `explorer.py` — `explore()` builds one `PageInventory` per URL, in
  isolation. `_probe_primary_flow()` already fills selects/inputs and clicks
  one action, but returns after the first navigation/reveal — it never keeps
  interacting with the destination page.
- `generator.py` — `generate_spec()` builds **one spec file per URL**
  (`{url}_test.py`) from that URL's single `PageInventory`. `prompt_for()`
  inserts a `PRIMARY FLOW` block only if `inventory.primary_flow` is set
  (`generator.py:346-347`), and `PRIMARY_FLOW_RULES` (`generator.py:71-93`)
  tells the model to make it test #1 — but it's still a single-page flow.
- `validator.py` — `validate_python_spec(source, url, inventory)` fact-checks
  every selector in a spec against **one flat inventory**
  (`_allowed_tokens(inventory)`, `validator.py:467`).
- `report.py` — `UrlReport` is one-page-scoped.

The gap is specifically: **explorer stops too early, and validator assumes
one page per spec.** Everything else (crawl, cli loop, autorepair) can stay
mostly as-is.

## Design summary

1. Explorer gains a multi-hop flow probe: after a step causes navigation,
   keep going on the new page (bounded depth) instead of returning.
2. Each hop's own `PageInventory` snapshot is kept *with* the flow, not
   discarded — this is what lets the validator stay strict per hop.
3. Generator builds a chained test from a multi-hop flow, with the prompt
   showing which controls belong to which hop.
4. Validator becomes step-aware: it maps each block of generated code to the
   hop it belongs to and checks selectors against that hop's inventory, not
   a merged pool (a merged pool would let a page-D selector wrongly validate
   an assertion that actually runs against page B).
5. The crawl/generate loop marks URLs "absorbed" into a longer flow so they
   don't also get a redundant shallow single-page spec.
6. Report gains hop-level attribution for flow test failures.

## Commit sequence and rollback strategy

Every phase below is designed to land as its **own commit** (or small commit
group), and the **old single-hop behavior stays the default and fully
intact** until Phase 7. That means at any point before Phase 7, a broken
phase can be undone with a single `git revert` of that phase's commit(s)
without touching anything before it — the pipeline falls straight back to
today's working single-page behavior.

Concretely:

- Add a `Settings` flag, e.g. `multi_hop_flows: bool = False` (default off),
  in `config.py` at the start of Phase 1. Every new code path in Phases 1-5
  is gated behind this flag. This means Phases 1-5 can be committed
  incrementally and merged into the main branch **without ever changing
  pipeline behavior for existing runs** — the flag is the rollback switch,
  not just the commit history.
- After **every** phase commit: run `pytest tests_python/ -q` (must stay
  green — this is the existing regression suite covering validator,
  autorepair, crawler, primary-flow probing) before moving to the next
  phase.
- After Phases 1, 2, 4, and 5 specifically: also run one real pipeline pass
  against `sat-stg.aljazeera.tv` (`generate` then `report`, flag on) and
  diff the output against the last known-good `full-report.docx` totals, so
  a regression in real generation quality is caught immediately, not just a
  unit-test pass.
- Tag the commit at the end of each phase (`git tag flow-plan-phase-1`, etc.)
  so you can `git diff`/`git checkout` between phase boundaries directly
  instead of hunting through history.
- Do not squash phase commits together. Keeping them separate is what makes
  "walk back the change that broke things" a one-line revert instead of an
  archaeology exercise.

---

## Phase 0 — Safety baseline

- Confirm `tests_python/` is green on current `master`.
- Tag current commit: `git tag pre-flow-plan`.
- No code changes.

## Phase 1 — `Settings.multi_hop_flows` flag + data model

**Files:** `config.py`, `models.py`

- Add `multi_hop_flows: bool` to `Settings` (default `False`), read from env
  like the rest of `Settings`.
- Extend the flow shape in `models.py`: today `primary_flow` is a single
  dict (`action`, `action_selector`, `steps`, effect fields). Add an optional
  `hops: list[dict]` field, where each hop dict is
  `{url, steps: [...], inventory_snapshot: PageInventory-shaped dict}`.
  Keep the existing single-hop fields untouched so old code paths (flag off)
  don't need to change at all.

**Commit:** "flow-plan: add multi_hop_flows flag + hop data model (inert)"
**Verify:** `pytest tests_python/ -q` green (nothing exercises the new field
yet, so this should be a no-op change to behavior).

## Phase 2 — Multi-hop explorer probe

**Files:** `explorer.py`

- Add `_probe_primary_flow_multi_hop()` alongside (not replacing)
  `_probe_primary_flow()`. Reuse the existing single-hop logic for hop 1,
  but instead of returning on `effect == "navigates"`, when
  `settings.multi_hop_flows` is on:
  - re-run `settle_page()` / `dismiss_overlays()` on the new URL,
  - re-run inventory collection for the new page (reuse whatever `explore()`
    calls internally for controls/headings — factor that inventory-building
    part out of `explore()` into a small helper if it isn't already
    separable, so it can be called mid-flow without a fresh `page.goto`),
  - look for the next verb-named action button on the new page the same way
    hop 1 did,
  - repeat, bounded by `max_hops` (start with 3 — homepage → results page →
    detail page is the common case; make it a `Settings` field, not a magic
    number),
  - stop and keep whatever hops succeeded so far if a hop fails (mirrors the
    existing "don't build a test from a dead flow" philosophy — a 2-hop
    partial flow is still useful, don't discard it because hop 3 failed).
- Log every hop transition and every stop reason, same style as the existing
  `primary-flow: ...` log lines — this is what let us diagnose the
  homepage's 2500ms Search-click timeout earlier, keep that visibility.
- `explore()` calls the multi-hop version instead of the single-hop version
  only when `settings.multi_hop_flows` is true; otherwise unchanged.

**Commit:** "flow-plan: multi-hop primary-flow probe behind flag"
**Verify:** `pytest tests_python/ -q` green. Add new unit tests
(`tests_python/test_primary_flow.py` already has the pattern) for: 2-hop
success, hop-3 failure keeping hops 1-2, max-hops cutoff, hop-1-fails
falls back to today's `None` behavior.

## Phase 3 — Crawl-loop coordination (absorbed URLs)

**Files:** `cli.py`, `urls.py`

- After exploring with multi-hop on, if a flow's hops include other URLs
  from `urls.txt`, mark those URLs "absorbed" in the run manifest
  (`manifest['urls'][url] = {'status': 'absorbed', 'into': origin_url}`)
  instead of generating a redundant shallow standalone spec for them.
- Still explore them independently first (you don't know a URL is going to
  be absorbed until some other page's flow reaches it), but skip the
  `generate_spec()` call for absorbed URLs.
- This needs a small ordering decision: process flow-generation for a URL
  only after all hops it might land on have been through `explore()` at
  least once for their own primary-flow attempt (so you know whether they'd
  have generated a meaningful standalone spec) — or, simpler for v1: always
  generate the standalone spec too, and let a human/report note flag
  "this page also appears as hop 2 of flow X" without suppressing anything
  yet. **Recommend starting with the simpler non-suppressing version** —
  suppression is an optimization, not a correctness requirement, and it's
  easy to get the ordering wrong and silently drop a page's only test.

**Commit:** "flow-plan: mark absorbed URLs in run manifest (non-suppressing)"
**Verify:** `pytest tests_python/ -q` green. Manual run on sat-stg: confirm
manifest correctly flags e.g. `/en/frequency-search` as reachable via the
homepage's flow, without losing its standalone spec.

## Phase 4 — Generator: chained multi-page spec

**Files:** `generator.py`

- Extend `PRIMARY_FLOW_RULES` (or add a new `MULTI_HOP_FLOW_RULES` block,
  cleaner than overloading the existing one) to cover the multi-page case:
  the model must treat each hop's controls as only valid *after* that hop's
  navigation, must call `_open`/rely on the already-navigated `page` object
  rather than re-opening between hops, and must assert something concrete
  on the **final** hop's landing state (not just "we got here").
- `prompt_for()` needs to render each hop's compacted controls separately
  and labeled ("HOP 1 (https://.../): ...", "HOP 2 (https://.../en/frequency-search):
  ..."), reusing `_compact_controls()` per hop rather than one merged block —
  this is what keeps the model from mixing up which selector belongs to
  which page.
- Only activate this path when `inventory.hops` is present (i.e., flag on
  and multi-hop probe succeeded with ≥2 hops); otherwise fall through to
  today's single-page `prompt_for()` unchanged.

**Commit:** "flow-plan: chained multi-hop prompt + generation path"
**Verify:** `pytest tests_python/ -q` green. Manually generate a spec for
the sat-stg homepage flow with the flag on, read the output by hand, confirm
it references hop-2 controls only after the hop-1 navigation in the code.

## Phase 5 — Validator: step-aware fact-checking

**Files:** `validator.py`

This is the highest-risk phase — it's where "same strictness" either holds
or quietly breaks.

- Add `validate_multi_hop_spec(source, hops: list[dict])` alongside (not
  replacing) `validate_python_spec()`.
- Reuse every pattern-only check as-is (syntax, action_evidence requirement,
  no unsafe imports, etc. — these don't care about page boundaries).
- For the inventory fact-check section: split the source into blocks by
  hop boundary. The generator (Phase 4) should emit an unambiguous marker
  comment per hop (e.g. `# --- hop 2: https://... ---`) specifically so the
  validator can split on it reliably — don't try to infer hop boundaries
  from `page.goto`/URL-assertion calls, that's fragile. Then run the
  existing `_allowed_tokens()` / selector cross-check
  (`validator.py:467-481`) **per block, against that hop's own
  `inventory_snapshot`**, not the union of all hops' tokens.
- Keep the union-of-all-hops check as a secondary/looser fallback only
  for the case where the marker comments are missing or malformed (should
  raise `SpecError` instead — don't silently fall back to the looser
  merged-pool check, since that's exactly the strictness regression this
  phase exists to prevent).
- `generate_spec()` in `generator.py` calls `validate_multi_hop_spec()`
  instead of `validate_python_spec()` when generating from a multi-hop
  flow (mirrors Phase 4's branch).

**Commit:** "flow-plan: step-aware validator for multi-hop specs"
**Verify:** `pytest tests_python/ -q` green, plus new unit tests mirroring
`tests_python/test_pipeline.py`'s validator coverage: a spec where hop-2 code
uses a hop-1-only selector must be **rejected** (this is the core regression
test for this phase — write it before writing the fix, confirm it fails
without the per-hop check and passes with it).

## Phase 6 — Report: hop-level attribution

**Files:** `report.py`

- `UrlReport`/`TestOutcome` gain an optional `hops: list[str]` field so a
  flow test's report entry can show which URLs it touched.
- `_behaviour_ceiling()` and the shallow-coverage warning logic
  (`report.py:255+`) should treat a multi-hop test as inherently
  behavioural (it can't exist without at least one real navigation-driven
  action) — don't let it get flagged as shallow just because it's one test
  covering what used to be three separate elements.
- `build_url_docx()` / `build_combined_docx()` — decide (and note in the doc
  itself) which "page" a flow test's report entry is filed under; simplest
  is the flow's origin URL, with the other hops listed in the test's
  evidence section.

**Commit:** "flow-plan: hop-aware report attribution"
**Verify:** `pytest tests_python/ -q` green. Manual run: confirm a flow
test's failure clearly shows which hop failed in the `.docx` output, not
just "test failed" with no indication of which page in the chain broke.

## Phase 7 — Flip the default, real-site validation

- Run the full pipeline against `sat-stg.aljazeera.tv` with
  `multi_hop_flows=True` end to end (crawl → explore → generate → report).
- Compare against the last known-good baseline run: total test count,
  pass/fail ratio, and manually read at least 2-3 generated flow specs for
  quality (same bar as the earlier "these are genuine tests" review).
- Only after this looks right: flip `Settings.multi_hop_flows` default to
  `True` in `config.py`.

**Commit:** "flow-plan: enable multi-hop flows by default"
**Verify:** Full `pytest tests_python/ -q` + full real-site run compared
against Phase-0 baseline numbers.

## Phase 8 — Cleanup

- Once stable for a while (your call on how long), remove the flag and the
  now-dead single-hop-only code paths it was guarding, or keep both
  permanently if single-hop remains useful for pages where a full flow
  doesn't make sense (e.g. the map/embed pages that already skip primary-flow
  entirely). **Recommend keeping both** — multi-hop is additive to the
  existing single-page testing, not a replacement for it; plenty of pages
  (the ones with no chainable action) will always fall back to single-page
  element tests, and that's correct behavior, not a gap.

**Commit:** "flow-plan: remove flag, multi-hop flows are now the default
path" (only if you decide to remove the flag).

---

## Open questions to resolve before/while implementing

1. **Max hop depth** — start at 3, but should it be per-site configurable
   (some journeys are legitimately 4-5 steps, e.g. add-to-cart → cart →
   checkout → confirm)?
2. **Absorbed-URL suppression (Phase 3)** — start non-suppressing as
   recommended, revisit once you can see how noisy the "also tested via
   flow X" duplication actually is in practice.
3. **Hop inventory freshness** — if a flow probe explores page B mid-flow,
   and page B was *also* crawled and explored standalone earlier in the run,
   should the flow reuse that earlier inventory snapshot (faster, but could
   be stale if the flow's navigation state differs, e.g. logged-in vs
   logged-out) or always re-derive it fresh mid-flow (slower, always
   accurate to the actual flow state)? **Recommend always fresh** — the
   whole point of strictness here is not trusting stale/context-mismatched
   observations (this is exactly the bug class that caused the
   `subscribe-now` failure from the earlier report review).
4. **Naming convention for flow specs** — one file per flow
   (`{origin-url}_flow_test.py`) separate from the page's own
   `{url}_test.py`, or merged into the same file? Recommend separate files —
   keeps the "absorbed but still independently testable" pages clean and
   makes flow-specific failures easy to isolate in the report.
