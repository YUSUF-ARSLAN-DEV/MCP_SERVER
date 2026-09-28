import json
from pathlib import Path

from docx import Document
from PIL import Image

from website_test_pipeline.flowgen import file_name
from website_test_pipeline.report import create_report, load_run

START = "https://x.test/en"


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), "white").save(path)


def _text(path: Path) -> list[str]:
    doc = Document(str(path))
    lines = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        lines += [cell.text for row in table.rows for cell in row.cells]
    return lines


def _flow(fid, status="verified", **over):
    flow = {"id": fid, "goal": "A visitor picks a country and sees the results.", "source": "intent", "status": status,
            "start_url": START, "steps": [{"kind": "click", "selector": None, "name": "Search"}],
            "outcome": {"effect": "navigates", "to": "/en/find"},
            "observed": {"effect": "navigates", "url": "https://x.test/en/find", "landed_url": START,
                         "step_urls": ["https://x.test/en/find"], "new_headings": ["Results"]}}
    flow.update(over)
    return flow


def _workspace(tmp_path):
    artifacts, tests = tmp_path / "artifacts", tmp_path / "tests"
    artifacts.mkdir()
    tests.mkdir()

    # a page-level spec whose failing assertion queries a role the explorer never recorded for that control
    page_spec = tests / "https-x-test-en_test.py"
    page_spec.write_text(
        "from playwright.sync_api import expect\n"
        "def test_wizard_step(page):\n"
        "    expect(page.get_by_role('group', name='Passcode (default)')).to_be_visible()\n", encoding="utf-8")
    (artifacts / "page.inventory.json").write_text(json.dumps(
        {"url": START, "controls": [{"name": "Passcode (default)", "tag": "fieldset", "role": None}]}), encoding="utf-8")

    good = _flow("f--good")
    flapper = _flow("f--flap", goal="A visitor searches and sees the flaky results page.")
    (tests / file_name(good)).write_text("def test_flow_good(page): pass\n", encoding="utf-8")
    (tests / file_name(flapper)).write_text("def test_flow_flap(page): pass\n", encoding="utf-8")

    page_node = "tests/https-x-test-en_test.py::test_wizard_step[chromium]"
    good_node = f"tests/{file_name(good)}::test_flow_good[chromium]"
    flap_node = f"tests/{file_name(flapper)}::test_flow_flap[chromium]"
    rows = [
        {"nodeid": page_node, "title": "wizard step", "url": START, "status": "failed", "duration": 1.0,
         "error": "E   AssertionError: Locator expected to be visible\nE     - waiting for get_by_role(\"group\", name=\"Passcode (default)\")"},
        {"nodeid": good_node, "title": "", "url": START, "status": "passed", "duration": 1.0, "error": None},
        {"nodeid": flap_node, "title": "", "url": START, "status": "passed", "duration": 1.0, "error": None},
    ]
    (artifacts / "test_results.json").write_text(json.dumps({"finished_at": "t", "tests": rows}), encoding="utf-8")
    _png(artifacts / "evidence" / "runs-tests-https-x-test-en-test-py-test-wizard-step-chromium" / "01.png")
    _png(artifacts / "evidence" / "runs-tests-flow-f-good-test-py-test-flow-good-chromium" / "01.png")
    _png(artifacts / "evidence" / "runs-tests-flow-f-flap-test-py-test-flow-flap-chromium" / "01.png")

    (tmp_path / "flows.json").write_text(json.dumps({"version": 1, "flows": [good, flapper]}), encoding="utf-8")
    (tmp_path / "flow_ratings.json").write_text(json.dumps({"version": 1, "ratings": {
        "f--good": [{"source": "runner", "at": "t1", "passed": True}, {"source": "runner", "at": "t2", "passed": True}],
        "f--flap": [{"source": "runner", "at": "t1", "passed": True},
                    {"source": "runner", "at": "t2", "passed": False, "error": "no content shown"},
                    {"source": "runner", "at": "t3", "passed": True}],
    }}), encoding="utf-8")
    return artifacts, tests


def test_load_run_populates_findings_and_flapping_from_real_files(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    run = load_run(artifacts, tests)
    assert len(run.findings) == 1
    f = run.findings[0]
    assert f.severity == "P2" and f.kind == "test_defect" and 'role="group"' in f.summary and "no role recorded" in f.summary
    assert (len(run.flapping) == 1 and run.flapping[0].test == "A visitor searches and sees the flaky results page."
            and run.flapping[0].sequence == "P F P")


def test_the_combined_report_leads_with_a_findings_section(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    doc = Document(str(tmp_path / "report" / "full-report.docx"))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert headings.index("Findings") < headings.index("User flows") < headings.index("Flow coverage")
    joined = chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
    for part in ("Findings", "Browser: Chromium", "OS:", "Python:", "P2", 'role="group"',
                 "no role recorded", "1 failure(s), classified below", "Coverage:", "content controls acted on",
                 "Executed", "Blocked", "Pass rate"):
        assert part in joined, part


def test_reports_include_a_word_table_of_contents_field(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    doc = Document(str(tmp_path / "report" / "full-report.docx"))
    assert "Table of Contents" in [p.text for p in doc.paragraphs]
    assert 'TOC \\o "1-3" \\h \\z \\u' in doc.part.element.xml


def test_the_consolidated_table_lists_every_test_with_expected_observed_and_verdict(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    doc = Document(str(tmp_path / "report" / "full-report.docx"))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert headings.index("Test summary") < headings.index("Findings") < headings.index("User flows")
    table = next(t for t in doc.tables if [c.text for c in t.rows[0].cells] == ["Scope", "Test", "URL", "Expected", "Observed", "Verdict"])
    rows = [[c.text for c in r.cells] for r in table.rows[1:]]
    by_test = {r[1].split(" [")[0]: r for r in rows}
    assert by_test["wizard step"][0] == "page" and by_test["wizard step"][5] == "FAILED"
    assert "Passcode" in by_test["wizard step"][3] or "get_by_role" in by_test["wizard step"][3]
    assert by_test["wizard step"][4] and by_test["wizard step"][4] != "as expected"
    assert "[P2]" in rows[0][1] if rows[0][5] == "FAILED" else True             # the failing row carries its severity
    flow_row = next(r for r in rows if r[1] == "A visitor picks a country and sees the results.")
    assert flow_row[0] == "flow" and flow_row[3] == "navigates -> /en/find" and flow_row[5] == "PASSED"
    assert flow_row[4] and flow_row[4] != "(never ran)"


def test_the_failing_test_is_listed_before_the_passing_ones(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    doc = Document(str(tmp_path / "report" / "full-report.docx"))
    table = next(t for t in doc.tables if [c.text for c in t.rows[0].cells] == ["Scope", "Test", "URL", "Expected", "Observed", "Verdict"])
    verdicts = [r.cells[5].text for r in table.rows[1:]]
    assert verdicts[0] == "FAILED" and verdicts.count("FAILED") == 1
    assert all(v == "PASSED" for v in verdicts[1:])


def test_a_page_test_with_no_captured_assertion_says_so_plainly(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    (tests / "https-x-test-en_test.py").write_text(
        "def test_wizard_step(page):\n    pass\n", encoding="utf-8")    # no expect()/assert - nothing to extract
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    joined = chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
    assert "(no assertion captured)" in joined


def test_no_test_summary_heading_when_there_is_nothing_to_summarise(tmp_path):
    from website_test_pipeline.report import RunReport, _test_summary_section
    from docx import Document as _Doc
    document = _Doc()
    _test_summary_section(document, RunReport())
    assert [p.text for p in document.paragraphs if p.style.name.startswith("Heading")] == []


def test_a_run_with_nothing_failing_says_so_plainly(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    results = json.loads((artifacts / "test_results.json").read_text(encoding="utf-8"))
    results["tests"] = [r for r in results["tests"] if r["status"] == "passed"]
    (artifacts / "test_results.json").write_text(json.dumps(results), encoding="utf-8")
    (tmp_path / "flow_ratings.json").write_text(json.dumps({"version": 1, "ratings": {
        "f--good": [{"source": "runner", "at": "t1", "passed": True}]}}), encoding="utf-8")
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    joined = chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
    assert "No failures to triage this run." in joined
    assert "changed verdict across recent runs" not in joined


def test_evidence_caption_reads_like_a_person_wrote_it():
    from website_test_pipeline.report import _evidence_caption
    assert _evidence_caption("01-click-flow") == "Step 1: click flow"
    assert _evidence_caption("02_select_countrylist") == "Step 2: select countrylist"
    assert _evidence_caption("99-outcome") == "Final outcome"
    assert _evidence_caption("failure") == "failure"


def test_a_login_wall_is_reported_as_not_tested(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    (artifacts / "login.inventory.json").write_text(json.dumps(
        {"url": "https://x.test/en/account", "auth": [{"kind": "login", "fields": [
            {"type": "email", "label": "Email"}, {"type": "password", "label": "Password"}]}]}), encoding="utf-8")
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    joined = chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
    assert "Not tested - 1 login or sign-up form(s) found" in joined
    assert "https://x.test/en/account" in joined and "Email, Password" in joined


def test_no_login_wall_means_no_not_tested_block(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    assert "Not tested -" not in chr(10).join(_text(tmp_path / "report" / "full-report.docx"))


def test_the_report_shows_the_authentication_line_and_a_finding_when_login_never_worked(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    (artifacts / "auth_history.json").write_text(json.dumps([
        {"account": "default", "method": "popup", "status": "failed", "attempts": 3, "url": "https://x.test/login",
         "error": "Wrong password"},
    ]), encoding="utf-8")
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    joined = chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
    assert "Authentication:" in joined and "Could not sign in to the site" in joined
    assert "BUG-001" in joined and "Open" in joined and "Unassigned" in joined


def test_the_report_shows_a_first_attempt_line_when_the_login_worked(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    (artifacts / "auth_history.json").write_text(json.dumps([
        {"account": "default", "method": "env", "status": "signed-in-env", "attempts": 1, "url": "https://x.test/login"},
    ]), encoding="utf-8")
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    joined = chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
    assert "Signed in to the site on the first attempt (env)." in joined
    assert "login: default" not in joined                    # no auth_failure finding when it worked


def test_no_auth_history_means_no_authentication_line(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    assert "Authentication:" not in chr(10).join(_text(tmp_path / "report" / "full-report.docx"))


# ------------------------------------------------------------------ plain-language translation (no raw Playwright
# code or pytest traces in the main-body tables; the Appendix keeps the raw form)

def test_plain_assertion_turns_the_known_expect_shapes_into_english():
    from website_test_pipeline.report import _plain_assertion
    cases = {
        "expect(page.get_by_role('heading', name='Login Page', exact=True)).to_be_visible()":
            'The "Login Page" heading should be visible on the page.',
        "expect(control).not_to_have_value('')": "It should not be left empty.",
        "expect(control).to_have_value('standard_user')": 'It should show "standard_user".',
        "expect(checkbox).to_be_checked()": "It should be checked.",
        "expect(button).to_be_disabled()": "It should be disabled.",
        "expect(page.locator('#inventory_container').first).to_have_count(1)":
            "1 of the element matching #inventory_container should be present.",
    }
    for code, plain in cases.items():
        assert _plain_assertion(code) == plain, code


def test_plain_assertion_preserves_the_real_case_of_a_name_unlike_str_capitalize():
    from website_test_pipeline.report import _plain_assertion
    out = _plain_assertion("expect(page.get_by_role('group', name='Passcode (default)', exact=True)).to_be_visible()")
    assert "Passcode (default)" in out and "passcode" not in out


def test_plain_assertion_reads_a_url_pattern_that_has_a_nested_closing_paren():
    from website_test_pipeline.report import _plain_assertion
    code = "expect(page).to_have_url(re.compile('/web/index\\.php/dashboard/index/?(?:[?#].*)?$'))"
    assert _plain_assertion(code) == "The page should end up at /web/index.php/dashboard/index."


def test_an_assertion_shape_not_in_the_known_vocabulary_falls_back_to_a_shortened_raw_line():
    from website_test_pipeline.report import _plain_assertion
    assert _plain_assertion("assert something_custom(x) == y") == "assert something_custom(x) == y"


def test_plain_error_covers_the_common_playwright_failure_shapes():
    from website_test_pipeline.findings import plain_error
    assert plain_error("E   AssertionError: Locator expected to be visible\nE   Error: element(s) not found") == \
        "The expected element never appeared on the page."
    assert "logout" not in plain_error(
        "E   AssertionError: Page URL expected to be 're.compile(\'/logout\')'\nE   Actual value: https://x.test/login")
    assert plain_error("E   playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.") == \
        "The action timed out waiting for the page to respond."
    assert "2 matching elements" in plain_error(
        'E   AssertionError: strict mode violation: locator("#x") resolved to 2 elements')
    assert plain_error(None) == "failed with no captured reason"


def test_the_test_execution_summary_and_defect_report_contain_no_raw_playwright_code(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    lines = _text(tmp_path / "report" / "full-report.docx")
    main_body = chr(10).join(lines[:lines.index("Appendix: full evidence")])   # the Appendix keeps the raw form on purpose
    for jargon in ("get_by_role(", "expect(", "Locator.", "playwright._impl", "AssertionError:"):
        assert jargon not in main_body, jargon


# ------------------------------------------------------------------ "Needs a human" (CAPTCHA / verification code)

def test_a_captcha_page_is_listed_as_needing_a_human(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    (artifacts / "subscribe.inventory.json").write_text(json.dumps(
        {"url": "https://x.test/subscribe", "controls": [
            {"name": "What code is in the image?", "tag": "input", "selector": "#edit-captcha-response",
             "field_name": "captcha_response"}],
         "forms": [], "accessibility": ""}), encoding="utf-8")
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    joined = chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
    assert "Needs a human" in joined and "CAPTCHA" in joined and "https://x.test/subscribe" in joined


def test_a_verification_code_field_is_also_flagged(tmp_path):
    from website_test_pipeline.findings import human_input_pages
    inv = [{"url": "https://x.test/2fa", "controls": [{"name": "Enter your verification code", "tag": "input"}],
           "forms": [], "accessibility": ""}]
    pages = human_input_pages(inv)
    assert len(pages) == 1 and pages[0]["reason"] == "verification code"


def test_an_ordinary_page_is_never_flagged_as_needing_a_human(tmp_path):
    artifacts, tests = _workspace(tmp_path)
    create_report(artifacts, tests, tmp_path / "report", combined=True)
    assert "Needs a human" not in chr(10).join(_text(tmp_path / "report" / "full-report.docx"))
