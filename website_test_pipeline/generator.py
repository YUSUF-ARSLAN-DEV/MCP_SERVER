from pathlib import Path
import re
from .models import PageInventory
from .validator import validate_python_spec
from .autorepair import repair_spec
from .llm import ModelError

SYSTEM = "Return exactly one complete Python pytest code block and no prose. Generate only tests supported by the observed page inventory."

EVIDENCE_RULES = (
    "EVERY test function MUST capture evidence, because the run produces a Word report a human uses to verify results:\n"
    "- import: from website_test_pipeline.evidence import action_evidence, observation_evidence\n"
    "- use the 'evidence_dir' fixture (a pathlib.Path); do NOT use tmp_path and do NOT open your own browser.\n"
    "- read-only test: call observation_evidence(page, label, verify, evidence_dir) at least once.\n"
    "- every click/fill/select/check/press: wrap it as action_evidence(page, label, action, verify, evidence_dir).\n"
    "- the 'verify' callback MUST itself call expect(...) - e.g. lambda: expect(field).to_have_value('x'). "
    "NEVER pass lambda: None or put the assertion only outside the callback; the screenshot is taken right after "
    "verify() runs, so the assertion has to be inside it.\n"
    "- 'label' is a short kebab-case step name like '01-form-visible' or '02-name-filled'."
)

SCOPE_RULES = (
    "Write 3 to 8 test functions, no more. Cover the highest-value user goals this page supports.\n"
    "One test = one user goal. Group only checks that belong to the SAME goal (e.g. all fields of one form). "
    "Keep navigation, search, and each distinct form as their own separate tests - do not fold unrelated checks into "
    "one big test, and never write one test per link, per heading, or per footer item.\n"
    "If a control opens a menu, panel, or dropdown whose contents are NOT in the CONTROLS list, only assert that the "
    "trigger button is visible and enabled - never click it and assert on guessed elements (no '# assuming ...' selectors)."
)

COVERAGE_RULES = (
    "Every CONTROL and HEADING is tagged [content] (specific to THIS page) or [chrome] (the site-wide "
    "header / nav / footer / newsletter that is identical on every page).\n"
    "- Write AT MOST ONE [chrome] test - a single check that the primary navigation and footer are present. "
    "Do not write separate footer / newsletter / language-switch / search tests on every page.\n"
    "- Every OTHER test must target a [content] or [other] control/heading (anything not [chrome]) - the thing that "
    "makes this page different from the rest of the site. Many sites never use <main>, so the real page content is "
    "tagged [other]; treat [other] exactly like [content].\n"
    "- If the page exposes only one or two [content] controls, write only one or two tests. A short, page-specific "
    "file beats a long file padded with repeated chrome checks.\n"
    "- At least half of your tests must DO something (click / fill / select / press) and assert the result. A file "
    "of pure expect(...).to_be_visible() checks has little value.\n"
    "- When REVEALED BY INTERACTION lists elements for a trigger, PREFER a test that clicks that trigger through "
    "action_evidence(...) and asserts one of the revealed elements is visible (or, for 'navigates', asserts a path "
    "regex with expect(page).to_have_url(re.compile(...))). That is a real behavioural test - write it instead of a "
    "bare visibility check on the trigger.\n"
    "- Aim for a MIX, not just visibility: (a) trigger -> revealed panel/menu appears; (b) navigation -> URL path "
    "regex; (c) form validation -> submit the form with required fields empty and assert an error region appears "
    "or a field is invalid; (d) a select/search flow -> choose the observed options, submit, assert the results "
    "container/heading appears. Only write the ones the inventory + REVEALED block actually support."
)

VALIDATION_RULES = (
    "FORM VALIDATION / ERROR HANDLING - when the page has a form with required fields (see FORMS and the required "
    "flag in CONTROLS), or REVEALED shows a 'submit ... -> validation' entry:\n"
    "- Write a test that submits the form with required fields left empty (wrap the submit click in action_evidence) "
    "and asserts the error state. Use the error-container selector from the matching 'submit ... -> validation' entry "
    "in the REVEALED block VERBATIM - e.g. expect(page.locator('<that selector>')).to_be_visible(). Do NOT guess "
    "'[role=\"alert\"]' or '.error' when REVEALED lists a concrete selector token (the real error block often has no "
    "role - it is a plain <div> with a class like .messages--error). Only if REVEALED lists NO selector for the "
    "validation entry, fall back to expect(field).to_have_attribute('aria-invalid', 'true'). NEVER assert the error "
    "message text (you did not observe it and copy changes).\n"
    "- If REVEALED says N field(s) marked :invalid, you may also assert expect(field).to_be_visible() then rely on "
    "the submit being blocked - but the primary assertion is the error region.\n"
    "- If REVEALED shows 'submits (no client validation)', do NOT write an empty-submit test - the form posts and "
    "navigates; instead fill the observed fields with plausible values and assert the resulting URL / success region.\n"
    "- For a search/filter form (a <select> plus a search button), select a real listed option, submit via "
    "action_evidence, and assert a results region/heading appeared - do not assert specific result rows (they rotate)."
)

EMBED_RULES = (
    "THIRD-PARTY MAP / MEDIA EMBED - if PAGE EMBEDS lists a map (Google Maps canvas, Leaflet, a maps <iframe>) and "
    "the page has few or no [content] controls, this page is NOT meaningfully testable. Write exactly ONE test: "
    "expect(page.locator('<the embed selector>')).to_be_visible() wrapped in observation_evidence. Do NOT click the "
    "map, drag it, zoom it, or assert markers / tiles / pins / info-windows / coordinates - that canvas has no DOM "
    "you can query and no stable state. Do NOT reach into the iframe. If the embed has no selector, assert "
    "page.locator('iframe').first is visible. Still write the single [chrome] nav/footer test if the chrome is present.\n"
)

LOCATOR_RULES = (
    "For a <select> / combobox, ALWAYS locate it by its id or selector token from CONTROLS "
    "(e.g. page.locator('#countrylist')). NEVER use get_by_label or get_by_role(name=...) for a select - "
    "its accessible name is often the concatenation of every option and will not match.\n"
    "For any nav or footer link, scope to the landmark and put .first on the LINK (not the landmark): "
    "page.get_by_role('navigation').get_by_role('link', name='News', exact=True).first - "
    "pages routinely render the primary nav twice (desktop + mobile/mega-menu). Putting .first on "
    "get_by_role('navigation') itself often selects a hidden menu. Do the same for anything marked "
    "AMBIGUOUS in CONTROLS.\n"
    "Do NOT assert expect(locator).to_have_count(n) with a guessed n - use it only with 0 or 1, or a "
    "number you can literally count in CONTROLS. For 'these links exist' assert each one .first is visible.\n"
    "Only use roles that are real ARIA roles (button, link, heading, textbox, combobox, checkbox, navigation, "
    "banner, contentinfo, list, listitem, dialog, region, ...). There is no 'video' role - a <video> element "
    "has no role; locate a media player by an id/selector or a nearby button you actually observed.\n"
    "A control tagged VOLATILE-ID has an auto-generated id/name that changes on every page load - NEVER use #id "
    "or [name=...] for it; locate it with get_by_role / get_by_placeholder / get_by_label.\n"
    "A control tagged AMBIGUOUS (or any [name=...] / role+name that the CONTROLS list shows more than once, e.g. a "
    "wizard with several 'Next' buttons) MUST have .first (or .nth(i)) ON THE SAME LINE as the locator call, or "
    "Playwright raises a strict-mode violation.\n"
    "Define every locator variable INSIDE the test function that uses it. Tests never share variables - a name bound "
    "in one test is not visible in another.\n"
    "get_by_role(name=...) with exact=True needs the COMPLETE accessible name exactly as it appears in CONTROLS / "
    "ACCESSIBILITY SIGNALS - do not shorten it. If CONTROLS shows a link 'About Al Jazeera', use name='About Al "
    "Jazeera', never name='About'. A truncated name is rejected as not-in-inventory.\n"
    "A heading OR control tagged HIDDEN exists only for screen readers (sr-only, or a 1px offscreen node like a "
    "jQuery-UI multiselect checkbox) - assert expect(x).to_have_count(1), never to_be_visible() / to_be_checked().\n"
    "A widget that opens a menu/dropdown/listbox (multiselect, combobox, date picker) leaves it OPEN and covering the "
    "page. Within one test, after interacting with such a widget, close it (press Escape, or click its trigger again) "
    "BEFORE the next click elsewhere - an open menu intercepts pointer events and the next action times out. Prefer "
    "one widget interaction per test.\n"
    "Before asserting a footer / newsletter / any below-the-fold element, first call "
    "locator.scroll_into_view_if_needed() as a plain statement (not inside a lambda)."
)

ASSERTION_RULES = (
    "URL assertions: sites redirect, add a trailing slash, a locale segment, or tracking params, and "
    "to_have_url() does NOT accept a glob. To check a navigation landed, use "
    "expect(page).to_have_url(re.compile(r'/section')) (add 'import re' at the top) - a partial regex on the "
    "path. page.wait_for_url('**/section**') also works but only OUTSIDE a verify callback. "
    "Never pass an exact URL string or a '**' glob to to_have_url(). "
    "To confirm the page under test loaded, assert its main heading is visible - not its URL.\n"
    "Never assert a state that the action you just performed changes or removes: a submit button that becomes "
    "disabled or relabelled after click, an overlay that _open() already dismissed, a field that clears on submit. "
    "Verify the genuine post-state (a value you set, a panel that appeared, a URL glob).\n"
    "Never assert exact copy you did not observe (no guessed success/error messages). Do not assert the exact "
    "text of a heading tagged [feed] or any individual article/news headline - that content rotates between "
    "crawl and run. Assert only stable structural headings and labels.\n"
    "Language switch: the 'English' / 'العربية' toggle usually points to a different domain. Assert only that the "
    "toggle link is visible - never assert the URL after clicking it, and prefer not to test it at all.\n"
    "Never assert on page.locator('div'/'span'/'a'/'p') with no id, attribute, or role qualifier - it matches "
    "hundreds of nodes."
)

def _control_line(control: dict) -> str:
    parts: list[str] = [f"[{control.get('region') or 'other'}]", str(control.get("tag") or "?")]
    name = (control.get("name") or "").strip()
    if name:
        parts.append(f'"{name[:80]}"')
    for key in ("selector", "id", "testid", "field_name"):
        if control.get(key):
            parts.append(f"{key}={control[key]}")
            break
    href = str(control.get("href") or "")
    if href:
        parts.append(f"href={href}")
    if control.get("type"):
        parts.append(f"type={control['type']}")
    if control.get("options"):
        parts.append(f"options={list(control['options'])[:15]}")
    for flag in ("required", "disabled"):
        if control.get(flag):
            parts.append(flag)
    if control.get("hidden"):
        parts.append("HIDDEN(sr-only / 1px - assert to_have_count(1), never to_be_visible)")
    if control.get("volatile_id"):
        parts.append("VOLATILE-ID(no stable selector - use role/name/placeholder)")
    if control.get("ambiguous"):
        parts.append("AMBIGUOUS(name seen on >1 element - scope to a landmark and/or append .first)")
    if control.get("checked") is not None:
        parts.append(f"checked={control['checked']}")
    return " ".join(parts)

def _href_prefix(href: str) -> str:
    return "/".join(href.split("/")[:4]) if href.startswith("/") and href.count("/") >= 3 else ""

def _compact_controls(controls: list[dict], content_budget: int = 75, chrome_budget: int = 16) -> str:
    # content-region first (survives the cap); collapse feed-style link runs; keep a little chrome.
    rank = {"content": 0, "other": 1, "chrome": 2}
    ordered = sorted(controls, key=lambda c: rank.get(c.get("region"), 1))
    lines: list[str] = []
    used_content = used_chrome = external = 0
    prefix_seen: dict[str, int] = {}
    for control in ordered:
        chrome = control.get("region") == "chrome"
        if chrome and used_chrome >= chrome_budget:
            continue
        if not chrome and used_content >= content_budget:
            continue
        href = str(control.get("href") or "")
        prefix = _href_prefix(href)
        if prefix:
            prefix_seen[prefix] = prefix_seen.get(prefix, 0) + 1
            if prefix_seen[prefix] > 4:  # collapse a run of feed/article links
                continue
        if href.startswith("http"):
            external += 1
            if external > 10:
                continue
        lines.append(_control_line(control))
        if chrome:
            used_chrome += 1
        else:
            used_content += 1
    return "\n".join(lines)

def _compact_revealed(revealed: list[dict], limit: int = 10) -> str:
    from urllib.parse import urlsplit
    lines: list[str] = []
    for entry in revealed[:limit]:
        trigger = (entry.get("trigger") or "?").strip()[:80]
        effect = entry.get("effect")
        if effect in {"navigates", "submits-without-validation"}:
            path = urlsplit(str(entry.get("to") or "")).path or str(entry.get("to") or "")
            verb = "navigates to" if effect == "navigates" else "submits (no client validation), lands on"
            lines.append(f'"{trigger}" -> {verb} {path}')
            continue
        controls = entry.get("controls") or []
        if effect == "validation":
            native = entry.get("native_invalid_fields")
            head = f'"{trigger}" -> validation'
            if native:
                head += f' ({native} field(s) marked :invalid by the browser)'
            lines.append(head + (":" if controls else ""))
            for control in controls[:6]:
                lines.append("    " + _control_line(control) + (f' text="{(control.get("text") or "").strip()[:80]}"' if control.get("text") else ""))
            continue
        if not controls:
            continue
        lines.append(f'click "{trigger}" -> reveals:')
        for control in controls[:8]:
            line = "    " + _control_line(control)
            preview = (control.get("text") or "").strip()
            if preview and preview[:40] not in line:
                line += f' text="{preview[:80]}"'
            lines.append(line)
    return "\n".join(lines)

def _compact_embeds(embeds: list[dict]) -> str:
    lines: list[str] = []
    for e in embeds[:6]:
        sel = e.get("selector")
        loc = f"selector={sel}" if sel else f'{e.get("tag") or "element"} (no stable selector)'
        title = f' title="{(e.get("title") or "").strip()[:60]}"' if e.get("title") else ""
        lines.append(f'[{e.get("region") or "other"}] {e.get("kind") or "embed"} '
                     f'({e.get("provider") or "?"}) {loc}{title}')
    return "\n".join(lines)

def _compact_headings(headings: list[dict]) -> str:
    out: list[str] = []
    for h in headings:
        text = (h.get("text") or "").strip()
        if not text:
            continue
        tags = f"[{h.get('region') or 'other'}]"
        if h.get("hidden"):
            tags += "[hidden]"
        if h.get("in_feed"):
            tags += "[feed]"
        out.append(f"{tags} {h.get('level') or 'H?'}: {text[:120]}")
    return "\n".join(out)

def prompt_for(guide: str, persona: str, inventory: PageInventory, feedback: str = "") -> str:
    correction = f"\n\nA PREVIOUS ATTEMPT WAS REJECTED OR FAILED: {feedback}\nFix exactly that problem and return a corrected module." if feedback else ""
    return (
        f"{guide}\n\nPERSONA:\n{persona}\n\n"
        f"PAGE URL: {inventory.url}\nPAGE TITLE: {inventory.title}\n\n"
        f"HEADINGS:\n{_compact_headings(inventory.headings)}\n\n"
        f"CONTROLS (one per line; use the id/selector token verbatim when present, else get_by_role with the quoted name; never invent a selector):\n{_compact_controls(inventory.controls)}\n\n"
        f"REVEALED BY INTERACTION (the explorer clicked the named trigger / submitted the named form and saw these - "
        f"treat them as observed; click/submit via action_evidence and assert the listed result):\n"
        f"{_compact_revealed(inventory.revealed) or '(none - the probe found no state change)'}\n\n"
        f"FORMS:\n{inventory.forms}\n\n"
        + (f"PAGE EMBEDS:\n{_compact_embeds(inventory.embeds)}\n\n" if getattr(inventory, "embeds", None) else "")
        + f"ACCESSIBILITY SIGNALS:\n{inventory.accessibility[:6000]}\n\n"
        "Generate dynamic, page-specific smoke tests as one pytest module. Only test controls that appear above; skip any marked disabled. "
        "Respect required/checked state and use only the listed select options. Use the pytest-playwright 'page' fixture. Never invent controls or outcomes.\n"
        "Start every test with _open(page); define _open as a one-liner: open_page(page, URL) "
        "(from website_test_pipeline.pageutils) - it navigates, waits for load, and clears consent dialogs and ad interstitials.\n"
        "Pass exact=True to every get_by_role(name=...); scope nav-link checks to get_by_role('navigation') "
        "and footer checks to get_by_role('contentinfo'), then put .first on the LINK, never on the landmark "
        "(the first navigation/contentinfo landmark is often a hidden mobile or skip-link menu): "
        "page.get_by_role('navigation').get_by_role('link', name='News', exact=True).first\n\n"
        f"{SCOPE_RULES}\n\n{COVERAGE_RULES}\n\n{VALIDATION_RULES}\n\n{EMBED_RULES}\n\n{LOCATOR_RULES}\n\n{ASSERTION_RULES}\n\n{EVIDENCE_RULES}{correction}"
    )

def extract_code(raw: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
    if not match: raise ValueError("model did not return a Python code block")
    return match.group(1).strip() + "\n"

def _skip_stub(inventory: PageInventory, reason: str) -> str:
    safe = re.sub(r"\s+", " ", reason).strip().replace('"', "'")[:200]
    return (
        "import pytest\n\n"
        f'URL = "{inventory.url}"\n\n'
        f'@pytest.mark.skip(reason="NOT TESTABLE: no spec passed validation - {safe}")\n'
        "def test_page_not_testable():\n"
        "    pass\n"
    )

def generate_spec(client, guide: str, persona: str, inventory: PageInventory, output: Path,
                  attempts: int = 5, log=None, seed_feedback: str = "") -> None:
    last = None
    feedback = seed_feedback
    for _ in range(attempts):
        try:
            code = extract_code(client.generate(prompt_for(guide, persona, inventory, feedback), SYSTEM))
            code, repairs = repair_spec(code, inventory)
            if repairs and log:
                log.info("autorepair %s: %s", inventory.url, "; ".join(repairs))
            validate_python_spec(code, inventory.url, inventory); output.write_text(code, encoding='utf-8'); return
        except ModelError as exc:
            last = exc
            if exc.status in {401, 403, 429, 500, 502, 503, 504, 524, 530}:
                break
        except Exception as exc: last = exc; feedback = str(exc)
    if isinstance(last, ModelError): raise last
    # The model never produced a valid spec for this page - ship a skipped module
    # so the page is still recorded and `execute` stays green instead of crashing.
    output.write_text(_skip_stub(inventory, str(last)), encoding="utf-8")
    raise RuntimeError(f"spec rejected after {attempts} attempts, wrote skip stub: {last}")
