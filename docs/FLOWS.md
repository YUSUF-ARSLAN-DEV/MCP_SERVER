# Flow testing - reference

Page tests check what a page offers. **Flow tests check what a visitor does**: pick a country, pick a
channel, search, land on the results. A flow is written as a plain sentence, turned into steps from the real
page DOM, run in a browser to see what really happens, and only then turned into a test. Nothing in a
generated test is guessed, and nothing here is specific to one website or language.

```
intents.json  ->  expand  ->  flows.json  ->  verify  ->  flowgen  ->  tests/flow_*_test.py  ->  execute / report
(sentences)      (AI + code)  (steps)         (browser)    (template)                           (results feed back)
```

Everything lives in `runs/<site>/`. The site is `SEED_URL` from `.env`, or `SITE=<host>` to pick another one for a
single command. All commands below are `python -m website_test_pipeline.cli <command>`.

## 1. Quick start for a new site

```powershell
python -m website_test_pipeline.cli crawl                # find the pages (writes urls.txt)
python -m website_test_pipeline.cli explore              # record what each page offers (and the flows it can prove)
python -m website_test_pipeline.cli flows run            # intents, expand, verify, flowgen, execute - then read the summary
python -m website_test_pipeline.cli report --combined    # the Word report, with a User flows and a Flow coverage section
```

Add your own journeys any time, in plain words: `flows add A visitor picks a country and searches for frequencies`.
Then `flows run` again: only new or reworded sentences are expanded, and only flows that need it are re-verified.

Working without the model or the site (at home, VPN down)? `flows run` skips what cannot run and says so; see
[section 9](#9-outages-and-the-model).

## 2. The stages

| Stage | Needs the model | Reads | Writes |
|-------|-----------------|-------|--------|
| `explore` | no | `urls.txt` | `artifacts/*.inventory.json`, flows it can prove in `flows.json` |
| `intents` | yes | inventories, `flows.json`, `intents.json` | new sentences in `intents.json` |
| `expand` | yes | `intents.json`, inventories | flows in `flows.json` (status `candidate`), critic ratings |
| `verify` | no (browser) | `flows.json` | what really happened, status, `flow_ratings.json`, healed steps |
| `flowgen` | no | `flows.json`, inventories | `tests/flow_<id>_test.py` |
| `execute` | no (browser) | `tests/` | `artifacts/test_results.json`, pytest entries in `flow_ratings.json` |
| `report` | no | all of the above | `artifacts/report/*.docx` |

`propose` is an older way to get flows: the model suggests flows directly (sentence and steps in one call). It uses
the same checks and also produces `candidate` flows.

## 3. Commands

| Command | What it does |
|---------|--------------|
| `intents` | The AI writes up to 8 plain sentences from the explored pages, aimed at what no tested flow covers yet. |
| `intents <file> ...` | The AI reads requirement documents (`.txt`, `.md`, `.rst`, `.docx`, `.pdf`) and writes journeys that test what they state; each must carry an exact quote from the document. |
| `expand [i-003 ...]` | Turns sentences that are new or reworded into steps. Names force a rebuild of those sentences. |
| `verify [id-fragment ...] [--failed-only]` | Runs flows in a real browser. Fragments pick flows; `--failed-only` runs only `candidate` and `stale` ones. |
| `flowgen` | Writes a spec for every `verified`, `approved` or `stale` flow and removes specs whose flow no longer qualifies. |
| `execute` | Runs all specs under pytest and records each flow test's result. |
| `report [--combined] [--repair] [--commit]` | Runs the tests and builds the Word report. |
| `flows list [--status S]` | Every flow, its status, its last real run. |
| `flows show <id>` | Steps, expected vs observed, the pages it visited, the full history, any heals. |
| `flows coverage [N]` | What the tested flows touch; N untouched controls shown per page. |
| `flows add <sentence>` | A person adds a sentence (quotes optional). |
| `flows edit <i-id> <sentence>` | Reword a sentence: its flow is demoted and its test removed until it is rebuilt. |
| `flows drop <i-id> [--reason ...]` | Stop testing a sentence; the record stays. |
| `flows intents` | The sentences, their status, and why one could not be built. |
| `flows approve <id> [--reason ...]` | A person's decision: never overwritten by the tool. |
| `flows reject <id> --reason ...` | A person's decision: not verified, its test removed. A reason is required. |
| `flows reset <id>` | Hand the flow back to the tool, which derives its status from the recorded runs. |
| `flows run [--skip a,b] [--only a,b]` | The five stages in order; stages are `intents`, `expand`, `verify`, `flowgen`, `execute`. |

`<id>` may be any unique fragment of a flow id. An ambiguous fragment lists the candidates and changes nothing.

### Exit codes

| Command | 0 | 1 | 2 | 3 | 4 |
|---------|---|---|---|---|---|
| `intents`, `expand` | done | model answer unusable, or a file error | no explored pages (`explore` first) | - | model unavailable, nothing changed |
| `verify` | done | - | no flows, or a fragment matched nothing | no flow could be reached (site down), nothing recorded | - |
| `flowgen` | done | flows file unreadable | no flows | - | - |
| `flows ...` | done | a file is unreadable | user error (unknown id, missing reason) | - | - |
| `flows run` | fine | the tests ran and some failed | a stage could not run, or no explored pages | - | - |

## 4. Files

### `intents.json` - the plain sentences

| Field | Meaning |
|-------|---------|
| `id` | `i-001`, never reused |
| `sentence` | the journey in plain words; **the source of truth** |
| `source` | `ai`, `human`, or `doc` (written by the AI from a requirements document) |
| `status` | see [section 5](#5-statuses-and-what-changes-them) |
| `flow_id` | the flow built from it (or the existing flow that already covers it) |
| `expanded_hash` | hash of the sentence when it was last expanded; a reworded sentence no longer matches and is expanded again |
| `reason` | why it is `unbuildable` or `dropped` |
| `start_path`, `evidence`, `proposed_by` | for AI sentences: the page it starts on, why the AI believes it exists, model and prompt version |
| `from_doc`, `quote` | for `doc` sentences: the file, and the exact sentence copied from it that the journey rests on |

### `flows.json` - the journeys

| Field | Meaning |
|-------|---------|
| `id`, `goal` | identity; for a flow built from a sentence the goal **is** the sentence |
| `source` | `explorer`, `model`, `human` or `intent` |
| `status` | see [section 5](#5-statuses-and-what-changes-them) |
| `start_url` | where the journey begins |
| `steps[]` | `kind` (`click`, `select`, `fill`, `multiselect`, `submit`), `selector`, `name` (cut to 40 characters), `value`, optional `page` and `role` |
| `outcome` | what was expected: `effect` (`navigates`, `results`, `reveals`, `validation`) and `to` |
| `observed` | what `verify` saw: `effect`, final `url`, `landed_url`, `step_urls` (the URL after each step), `step_effects`, `new_headings`, `new_controls`, `results` |
| `intent_id`, `intent_hash`, `intent_state` | link to the sentence; `intent_state` is `edited` or `dropped` while the flow waits for a rebuild |
| `heal_history[]` | every automatic repair, with the old values kept |
| `last_run_at` | time of the last `verify` |

### `flow_ratings.json` - append-only history

One list per flow id; entries are never edited or deleted.

| `source` | Fields |
|----------|--------|
| `model` | the critic: `scores` (`coherence`, `importance`, `outcome_strength`, 1-5), `reason`, `kept`, `model`, `prompt_version` |
| `runner` | `passed`, `checks` (steps completed, outcome matched, observed effect), `error`, `healed` / `heal_not_kept`, `definite` |
| `pytest` | `passed`, `test`, `error` |
| `human` | `by`, `decision` (`approved`, `rejected`, `reset`), `reason` |

### `heuristics.json` - adapting to a site or language (optional)

The word lists and selectors the tool uses to read sentences and unfamiliar pages. Defaults are generic and name no
site. Entries you list are **added** to the defaults; keys named under `"replace"` **replace** them. A trailing `*`
matches any ending (`result*`). A missing or malformed file means the defaults, with a warning.

| Key | What it is for |
|-----|----------------|
| `content_words` | words that make a sentence promise content ("sees the results") |
| `pick_verbs` | verbs meaning "choose an option" |
| `trigger_stopwords` | words in a menu button's name that say nothing about its options |
| `all_option_words` | options meaning "everything" (never chosen by default) |
| `all_option_prefixes` | verbs that can precede them ("Select") |
| `volatile_params` | URL parameters that change every visit (never asserted); `=name` means the exact name |
| `loader_hints` | class fragments of loading spinners |
| `menu_selectors` | containers of an open dropdown or menu |
| `option_selectors` | what an option inside one looks like |
| `unsuitable` | sentences the AI may not write: `{"pattern": regex, "reason": text}` |

```json
{ "content_words": ["résultats", "liste"], "pick_verbs": ["choisit"], "loader_hints": ["chargement"],
  "unsuitable": [{ "pattern": "connexion", "reason": "needs credentials" }], "replace": ["trigger_stopwords"] }
```

## 5. Statuses and what changes them

**Flow status**

| Status | Meaning | Gets a test |
|--------|---------|-------------|
| `candidate` | written but not (or no longer) backed by a passing run | no |
| `verified` | a real run did every step and saw the expected outcome | yes |
| `stale` | was verified, then failed twice in a row | yes (failing is the alarm) |
| `approved` | a person accepted it; never overwritten | yes |
| `rejected` | a person refused it; skipped everywhere | no |

- One failed run is tolerated (a flaky network is not a broken flow); two in a row make a verified flow `stale`.
- A **definite** failure (a flow built from a sentence promising content that only changed the URL) demotes to
  `candidate` at once: repeating it would not change the result.
- Only a real run makes a flow `verified`. A model can suggest and rate; it cannot verify.
- `approved` and `rejected` are never changed by any command except `flows reset`.

**Intent status**: `new` (not expanded), `expanded` (has a flow), `unbuildable` (the reason is stored with it), `covered` (an existing
flow already does this), `dropped` (a person removed it).

## 6. What a generated test asserts

Everything comes from what `verify` observed. Nothing is predicted.

- Each step runs inside `action_evidence` and is followed by a check: a select has a value, a fill holds its text, a
  navigation lands on the observed page path.
- After the last step: up to two new headings (short ones; long ones by one distinctive word; none with digits, which
  rotate), a results region, or the query parameters the search produced (names only, never values, never tracking
  or session parameters).
- A flow whose outcome would prove only "the URL changed" is flagged in the report as weak.
- A spec is written only if it passes the same validator as model-written specs; otherwise the flow is skipped and
  the reason logged. Controls are found the way `verify` found them: the whole accessible name, or the start of a
  name stored at the 40-character cut.

## 7. Healing and rebuilding

- **A control moved or was renamed.** If a step's control is gone, `verify` retries with the one control on the page
  that can stand in for it (same name with a new selector or role, or a name that is nearly the same). Several
  candidates, or none, means no heal. It is kept only if the whole run then passes, with the old values in
  `heal_history`. A flow a person decided is never rewritten; its error names the control that looks like the
  missing one.
- **A default shows nothing.** A sentence-built flow whose select has no chosen value runs with the first real
  option. If the page then shows nothing, up to 4 other options are tried and the first that shows content becomes
  the flow's explicit value.
- **A sentence changed.** Editing or dropping a sentence marks its flow `edited`/`dropped` and demotes it; its test is
  removed by the next `verify` or `flowgen` until `expand` rebuilds it. Case and spacing changes do nothing.

## 8. Coverage

`flows coverage` and the report's **Flow coverage** section count, per explored page, whether a tested flow visits it
and how many of its content controls (visible, enabled links, buttons, selects, text fields; site chrome left out) a
step acts on. Candidate and stale flows are counted separately and never as coverage. A start page that redirects to
another explored page is counted once. `intents` and `propose` are told the uncovered parts, so new journeys aim there.

## 9. Outages and the model

- **Model unreachable, or up but timing out** (gateway 524, network error): `intents` and `expand` stop early, change
  nothing, and `flows run` skips them and continues.
- **Site unreachable** (DNS, refused or timed-out connection): `verify` skips the flow and records nothing (exit 3);
  `execute` records no failures caused by the outage. An outage is not a failed flow.
- **A Word report file open in Word:** the report is written as `<name>-new.docx` instead of failing.

## 10. Troubleshooting

| You see | Cause | What to do |
|---------|-------|------------|
| a sentence `unbuildable`: *control "X" not found* | the AI named a control the page does not have | reword the sentence with the names visible on the page |
| *no link between them* | a step on page B follows page A but the site map has no link A to B | check the journey exists; explore more pages |
| *option "X" was never seen* / *nothing can be picked* | a named option was never recorded in that menu | run `explore` again, or name an option that exists |
| *a single step is only a journey when it navigates* | a one-action idea that stays on the page | add a second action, or leave it to the page tests |
| *critic: the steps do not match the sentence* | the model's steps drifted from your wording | reword, then `expand <id>` |
| a flow stays `candidate` after `verify` | a step failed, the outcome did not match, or the sentence promised content that never appeared | `flows show <id>` shows the reason |
| a flow is `stale` | two failed executions in a row | fix the site, or `verify --failed-only`; healing may repair it |
| a test disappeared after `flows edit` | its flow is waiting for the reworded sentence | `expand`, then `verify`, then `flowgen` |
| a document journey *rejected: its quote is not in the file* | the AI paraphrased or invented a requirement | nothing to do: only journeys resting on the document's own words are kept |
| *has no readable text* for a document | a scanned or image-only PDF | attach a text version |
| *intents/expand skipped: model not reachable* | offline, VPN, or the model is down | run again later; the other stages already ran |

## 11. Known limits

- No login, credentials or payment flows: the AI may not write them, and `heuristics.json` can extend that list.
  A person may write any sentence and takes responsibility for it.
- Options in custom dropdowns are found through standard roles and common menu classes; a widget that uses neither
  needs its selectors added to `menu_selectors` / `option_selectors`.
- The explorer records the first 300 options of a select, and a menu's options only if opening it revealed them.
- Option healing applies to flows built from sentences that promise content; other flows are only reported.
- Explorer-recorded flows can be poor (for example a probe that fills a field the page does not really offer);
  they stay `candidate` and never produce a test.
- The default word lists are English. Other languages work by adding words to `heuristics.json`.
- Documents: text is extracted by code (no OCR for scanned files); at most the first 4 parts (about 6,000 characters each)
  of a document are read per run. A requirement the explored pages cannot show is skipped, never invented.
- Chromium only. One page per run; no multi-tab flows.

## 12. Developing and testing

- `python -m pytest tests_python -q` runs the unit tests (about a second). They include checks that this document
  names only commands, stages, statuses and heuristic keys that exist.
- Features that touch the browser are checked against a small local page served from `127.0.0.1` (a few lines of
  `http.server` in a thread), so they need no internet, no model and no particular site. That is also the proof that
  nothing depends on one site.
- Every step of the flow work is one commit, tagged `flows-step-<n>`; `git revert <commit>` undoes one step.
