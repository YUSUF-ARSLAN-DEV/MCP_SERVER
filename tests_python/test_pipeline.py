import pytest
from pathlib import Path
from website_test_pipeline.generator import extract_code
from website_test_pipeline.validator import validate_python_spec, SpecError
from website_test_pipeline.models import PageInventory
from website_test_pipeline.urls import canonicalize, is_degenerate, merge_extra_urls
from website_test_pipeline.crawler import crawl

def test_extracts_python_code_block():
    assert extract_code('```python\nassert 1\n```') == 'assert 1\n'

def test_rejects_missing_url():
    with pytest.raises(SpecError, match='target URL'):
        validate_python_spec('assert True\n', 'https://example.test')

def test_rejects_action_without_evidence():
    with pytest.raises(SpecError, match='action_evidence'):
        validate_python_spec("page.click('button')\nassert True\nhttps://example.test", 'https://example.test')

def test_canonicalize_removes_fragment_and_trailing_slash():
    assert canonicalize('HTTPS://Example.TEST/path/#section') == 'https://example.test/path'

def test_rejects_unsafe_import():
    with pytest.raises(SpecError, match='unsafe system import'):
        validate_python_spec('import subprocess\nassert True\n# https://example.test', 'https://example.test')

def _inventory():
    return PageInventory('https://example.test', 'T', headings=[{'level': 'H1', 'text': 'Welcome'}],
                         controls=[{'selector': '#real-btn', 'name': 'Real'}])

def test_rejects_selector_not_in_inventory():
    src = "page.locator('#ghost-btn')\nassert True\n# https://example.test"
    with pytest.raises(SpecError, match='inventory'):
        validate_python_spec(src, 'https://example.test', _inventory())

def test_accepts_selector_in_inventory():
    src = "page.locator('#real-btn')\nassert True\n# https://example.test"
    validate_python_spec(src, 'https://example.test', _inventory())

def test_locator_match_does_not_span_lines():
    src = 'link = page.get_by_role("link", name="Real")\nexpect(link).to_be_visible()\nassert link\n# https://example.test'
    validate_python_spec(src, 'https://example.test', _inventory())

def test_rejects_test_without_evidence():
    src = 'def test_x(page, evidence_dir):\n    expect(page).to_have_url("https://example.test")\n'
    with pytest.raises(SpecError, match='no evidence'):
        validate_python_spec(src, 'https://example.test')

def test_accepts_test_with_observation_evidence():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    heading = page.get_by_role("heading", name="Welcome", exact=True)\n'
           '    observation_evidence(page, "seen", lambda: expect(heading).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test')

def test_rejects_glob_in_to_have_url():
    src = 'def test_x(page, evidence_dir):\n    observation_evidence(page, "u", lambda: expect(page).to_have_url("**/news/**"), evidence_dir)\n# https://example.test'
    with pytest.raises(SpecError, match='glob'):
        validate_python_spec(src, 'https://example.test')

def test_allows_exact_url_assertion_but_prompt_discourages_it():
    # exact to_have_url is no longer a hard reject (prompt/guide steer away from it);
    # only the un-runnable glob form is rejected.
    src = 'def test_x(page, evidence_dir):\n    observation_evidence(page, "u", lambda: expect(page).to_have_url("https://example.test"), evidence_dir)\n'
    validate_python_spec(src, 'https://example.test')

def test_rejects_invalid_aria_role():
    src = 'def test_x(page, evidence_dir):\n    v = page.get_by_role("video", name="Player")\n    observation_evidence(page, "v", lambda: expect(v).to_be_visible(), evidence_dir)\n# https://example.test'
    with pytest.raises(SpecError, match='ARIA role'):
        validate_python_spec(src, 'https://example.test')

def test_rejects_guessed_to_have_count():
    src = 'def test_x(page, evidence_dir):\n    observation_evidence(page, "c", lambda: expect(page.get_by_role("link")).to_have_count(15), evidence_dir)\n# https://example.test'
    with pytest.raises(SpecError, match='count'):
        validate_python_spec(src, 'https://example.test')

def test_rejects_bare_tag_locator():
    src = 'def test_x(page, evidence_dir):\n    observation_evidence(page, "d", lambda: expect(page.locator("div")).to_contain_text("hi"), evidence_dir)\n# https://example.test'
    with pytest.raises(SpecError, match='every'):
        validate_python_spec(src, 'https://example.test')

def test_rejects_volatile_id_locator():
    src = 'def test_x(page, evidence_dir):\n    f = page.locator("#newsletter-email-768180")\n    observation_evidence(page, "f", lambda: expect(f).to_be_visible(), evidence_dir)\n# https://example.test'
    with pytest.raises(SpecError, match='auto-generated'):
        validate_python_spec(src, 'https://example.test')

def test_rejects_empty_verify_callback():
    src = ('def test_x(page, evidence_dir):\n'
           '    link = page.get_by_role("link", name="Real", exact=True)\n'
           '    expect(link).to_be_visible()\n'
           '    observation_evidence(page, "x", lambda: None, evidence_dir)\n# https://example.test')
    with pytest.raises(SpecError, match='asserts nothing'):
        validate_python_spec(src, 'https://example.test', _inventory())

def test_region_tags_render_and_order_content_first():
    from website_test_pipeline.generator import _compact_controls
    controls = [
        {"tag": "a", "name": "Privacy", "region": "chrome", "href": "/privacy"},
        {"tag": "button", "name": "Play episode", "region": "content"},
        {"tag": "input", "name": "email", "region": "content", "volatile_id": True},
    ]
    out = _compact_controls(controls)
    lines = out.splitlines()
    assert lines[0].startswith("[content]")           # content sorted first
    assert "VOLATILE-ID" in out and "[chrome]" in out

def test_accepts_url_regex_and_wait_for_url():
    src = ('import re\n'
           'def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    def act():\n'
           '        page.get_by_role("link", name="Real", exact=True).first.click()\n'
           '        page.wait_for_url("**/b**")\n'
           '    action_evidence(page, "go", act, lambda: expect(page).to_have_url(re.compile(r"/b")), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test', _inventory())


class _FakePage:
    def __init__(self, links):
        self.links = links  # {url: [href, ...]}
        self._current = None

    def set_default_navigation_timeout(self, ms):
        pass

    def goto(self, url, **kwargs):
        self._current = url

    def locator(self, selector):
        return self

    def evaluate_all(self, script):
        return self.links.get(self._current, [])


def test_crawl_stays_same_origin_and_dedupes():
    page = _FakePage({
        'https://site.test/a': [
            'https://site.test/a',            # self, already seen
            'https://site.test/b',
            'https://site.test/b#frag',       # canonicalizes to /b, deduped
            'https://other.test/x',           # different origin, dropped
            'mailto:hi@site.test',            # non-http, dropped
        ],
        'https://site.test/b': ['https://site.test/c'],
    })
    found = crawl(page, 'https://site.test/a', max_depth=2, max_pages=10)
    assert found == ['https://site.test/a', 'https://site.test/b', 'https://site.test/c']


def test_crawl_respects_max_pages():
    page = _FakePage({'https://site.test/a': [
        'https://site.test/1', 'https://site.test/2', 'https://site.test/3',
    ]})
    found = crawl(page, 'https://site.test/a', max_depth=5, max_pages=2)
    assert found == ['https://site.test/a', 'https://site.test/1']


def test_crawl_depth_zero_visits_only_seed():
    page = _FakePage({'https://site.test/a': ['https://site.test/b']})
    found = crawl(page, 'https://site.test/a', max_depth=0, max_pages=10)
    assert found == ['https://site.test/a']


# --------------------------------------------------------------- degenerate URLs

@pytest.mark.parametrize("url", [
    "https://sat.aljazeera.net/en/map-search-results/-1/null",
    "https://site.test/user/undefined",
    "https://site.test/p/NaN/detail",
    "https://site.test/item/{id}",
    "https://site.test/x/:slug",
])
def test_is_degenerate_flags_placeholders(url):
    assert is_degenerate(url)

@pytest.mark.parametrize("url", [
    "https://site.test/en/map",
    "https://site.test/covid-19/latest",
    "https://site.test/article/12345",
    "https://site.test/",
])
def test_is_degenerate_passes_real_urls(url):
    assert not is_degenerate(url)

def test_crawl_drops_degenerate_urls():
    page = _FakePage({'https://site.test/a': [
        'https://site.test/good',
        'https://site.test/map-results/-1/null',
        'https://site.test/user/undefined',
    ]})
    found = crawl(page, 'https://site.test/a', max_depth=2, max_pages=10)
    assert found == ['https://site.test/a', 'https://site.test/good']

def test_merge_extra_urls_adds_same_origin_and_skips_junk(monkeypatch, tmp_path):
    monkeypatch.setenv("EXTRA_URLS", "https://site.test/deep/1/2 , https://evil.test/x")
    seeds = tmp_path / "seeds.txt"
    seeds.write_text("# operator seeds\nhttps://site.test/wizard/step-2\nhttps://site.test/bad/null\n", encoding="utf-8")
    merged = merge_extra_urls(['https://site.test/a'], 'https://site.test/', seeds)
    assert merged == [
        'https://site.test/a',
        'https://site.test/deep/1/2',
        'https://site.test/wizard/step-2',
    ]


# ---------------------------------------------------------- explore interaction probe

from website_test_pipeline.explorer import _probe_interactions, _PANEL_SEL, _is_probe_trigger
from website_test_pipeline.generator import _compact_revealed


class _ProbeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
    @property
    def first(self):
        return self
    def count(self):
        return 0
    def is_visible(self):
        return False
    def evaluate_all(self, script, *args):
        if self.selector == _PANEL_SEL:
            return self.page._panels()
        return self.page._controls()
    def click(self, **kwargs):
        self.page._do_click()
    def wait_for(self, **kwargs):
        pass


class _ProbePage:
    def __init__(self, before, after, navigate_to=None, panels_before=None, panels_after=None):
        self._before, self._after, self._navigate_to = before, after, navigate_to
        self._panels_before = panels_before or []
        self._panels_after = panels_after or []
        self._url = "https://site.test/map"
        self._clicked = False
    def goto(self, url, **kwargs):
        self._url = url
        self._clicked = False
    def set_default_navigation_timeout(self, ms):
        pass
    def wait_for_load_state(self, *a, **k):
        pass
    def wait_for_timeout(self, *a, **k):
        pass
    @property
    def url(self):
        return self._url
    def locator(self, selector):
        return _ProbeLocator(self, selector)
    def get_by_role(self, *a, **k):
        return _ProbeLocator(self, "role")
    def _controls(self):
        return self._after if self._clicked else self._before
    def _panels(self):
        return self._panels_after if self._clicked else self._panels_before
    def _do_click(self):
        self._clicked = True
        if self._navigate_to:
            self._url = self._navigate_to


_TRIGGER = {"tag": "button", "name": "Show frequencies", "selector": "#show-freq", "region": "content"}

def test_probe_records_revealed_controls():
    revealed_ctrl = {"tag": "div", "role": "list", "name": "Frequency results",
                     "selector": "#freq-list", "region": "content"}
    page = _ProbePage([_TRIGGER], [_TRIGGER, revealed_ctrl])
    out = _probe_interactions(page, "https://site.test/map", [_TRIGGER], limit=5)
    assert out == [{"trigger": "Show frequencies", "effect": "reveals", "controls": [revealed_ctrl]}]

def test_probe_records_navigation():
    page = _ProbePage([_TRIGGER], [_TRIGGER], navigate_to="https://site.test/map/detail")
    out = _probe_interactions(page, "https://site.test/map", [_TRIGGER], limit=5)
    assert out == [{"trigger": "Show frequencies", "effect": "navigates", "to": "https://site.test/map/detail"}]

def test_probe_disabled_by_zero_limit():
    page = _ProbePage([_TRIGGER], [_TRIGGER, {"tag": "div", "name": "x", "region": "content"}])
    assert _probe_interactions(page, "https://site.test/map", [_TRIGGER], limit=0) == []

def test_probe_skips_form_and_destructive_triggers():
    triggers = [
        {"tag": "button", "name": "Delete account", "region": "content"},
        {"tag": "button", "name": "Search", "region": "content", "in_form": True},
        {"tag": "a", "name": "Some link", "region": "content"},
        {"tag": "input", "type": "submit", "name": "Send", "region": "content"},
    ]
    page = _ProbePage(triggers, triggers + [{"tag": "div", "name": "new", "region": "content"}])
    assert _probe_interactions(page, "https://site.test/map", triggers, limit=5) == []

def test_probe_clicks_input_type_button_and_reads_panel():
    trigger = {"tag": "input", "type": "button", "name": "Next", "region": "content", "in_form": True}
    panel = {"tag": "fieldset", "role": None, "name": "Manual search", "selector": None,
             "region": "content", "text": "Manual search for Aljazeera frequencies"}
    page = _ProbePage([trigger], [trigger], panels_before=[], panels_after=[panel])
    out = _probe_interactions(page, "https://site.test/tune", [trigger], limit=5)
    assert out == [{"trigger": "Next", "effect": "reveals", "controls": [panel]}]

def test_is_probe_trigger_accepts_role_button_rejects_submit():
    assert _is_probe_trigger({"tag": "input", "type": "button", "name": "Go", "region": "content"})
    assert _is_probe_trigger({"tag": "span", "role": "button", "name": "Toggle", "region": "content"})
    assert _is_probe_trigger({"tag": "button", "name": "Go", "region": "other"})  # sites without <main>
    assert not _is_probe_trigger({"tag": "input", "type": "submit", "name": "Go", "region": "content"})
    assert not _is_probe_trigger({"tag": "button", "name": "Go", "region": "chrome"})
    assert not _is_probe_trigger({"tag": "button", "name": "Go", "region": "content", "hidden": True})

def test_probe_drops_hidden_revealed_controls():
    # jQuery-UI multiselect: opening the menu "reveals" 1px sr-only checkboxes.
    visible_ctrl = {"tag": "div", "role": "list", "name": "Channel list",
                    "selector": "#chan-list", "region": "content"}
    hidden_ctrl = {"tag": "input", "type": "checkbox", "name": "BBC One",
                   "selector": None, "region": "content", "hidden": True}
    page = _ProbePage([_TRIGGER], [_TRIGGER, visible_ctrl, hidden_ctrl])
    out = _probe_interactions(page, "https://site.test/map", [_TRIGGER], limit=5)
    assert out == [{"trigger": "Show frequencies", "effect": "reveals", "controls": [visible_ctrl]}]

def test_control_line_marks_hidden():
    from website_test_pipeline.generator import _control_line
    line = _control_line({"tag": "input", "name": "BBC One", "region": "content", "hidden": True})
    assert "HIDDEN" in line

def test_compact_revealed_renders_trigger_and_controls():
    revealed = [
        {"trigger": "Show frequencies", "effect": "reveals",
         "controls": [{"tag": "div", "name": "Frequency results", "selector": "#freq-list", "region": "content"}]},
        {"trigger": "Open map", "effect": "navigates", "to": "https://site.test/map/full?x=1"},
    ]
    out = _compact_revealed(revealed)
    assert 'click "Show frequencies" -> reveals:' in out
    assert "#freq-list" in out
    assert '"Open map" -> navigates to /map/full' in out

def test_compact_revealed_renders_form_validation():
    revealed = [{
        "trigger": "submit #f with no input", "effect": "validation",
        "native_invalid_fields": 2,
        "controls": [{"tag": "div", "role": "alert", "name": "err", "selector": "#e", "region": "content"}],
    }]
    out = _compact_revealed(revealed)
    assert "validation" in out and "#e" in out and ":invalid" in out

def test_rejects_name_defined_in_sibling_test():
    src = (
        'def test_a(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    field = page.get_by_label("Name")\n'
        '    observation_evidence(page, "n", lambda: expect(field).to_be_visible(), evidence_dir)\n'
        'def test_b(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    observation_evidence(page, "n", lambda: expect(field).to_have_value("x"), evidence_dir)\n'
    )
    with pytest.raises(SpecError, match="never defined"):
        validate_python_spec(src, 'https://example.test')

def test_accepts_locator_defined_in_same_test():
    src = (
        'def test_a(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    field = page.get_by_label("Name")\n'
        '    observation_evidence(page, "n", lambda: expect(field).to_have_value("x"), evidence_dir)\n'
    )
    validate_python_spec(src, 'https://example.test')

def test_rejects_ambiguous_role_name_without_first():
    inv = PageInventory('https://example.test', 'T', controls=[
        {'name': 'Next', 'tag': 'button', 'ambiguous': True},
    ])
    src = (
        'def test_next(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    button = page.get_by_role("button", name="Next", exact=True)\n'
        '    observation_evidence(page, "n", lambda: expect(button).to_be_visible(), evidence_dir)\n'
    )
    with pytest.raises(SpecError, match="AMBIGUOUS"):
        validate_python_spec(src, 'https://example.test', inv)

def test_rejects_unscoped_name_attr_locator_without_inventory():
    src = (
        'def test_next(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    button = page.locator(\'input[name="next"]\')\n'
        '    observation_evidence(page, "n", lambda: expect(button).to_be_visible(), evidence_dir)\n'
    )
    with pytest.raises(SpecError, match="rarely unique"):
        validate_python_spec(src, 'https://example.test')

def test_accepts_name_attr_locator_with_first():
    src = (
        'def test_next(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    button = page.locator(\'input[name="next"]\').first\n'
        '    observation_evidence(page, "n", lambda: expect(button).to_be_visible(), evidence_dir)\n'
    )
    validate_python_spec(src, 'https://example.test')

def test_name_attr_locator_ok_when_inventory_has_single_control():
    inv = PageInventory('https://example.test', 'T',
                        controls=[{'field_name': 'password', 'name': 'Password'}])
    src = (
        'def test_pw(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    field = page.locator(\'input[name="password"]\')\n'
        '    observation_evidence(page, "p", lambda: expect(field).to_be_visible(), evidence_dir)\n'
    )
    validate_python_spec(src, 'https://example.test', inv)

def test_name_attr_locator_rejected_when_inventory_marks_ambiguous():
    inv = PageInventory('https://example.test', 'T',
                        controls=[{'field_name': 'next', 'name': 'Next', 'ambiguous': True}])
    src = (
        'def test_next(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    button = page.locator(\'input[name="next"]\')\n'
        '    observation_evidence(page, "n", lambda: expect(button).to_be_visible(), evidence_dir)\n'
    )
    with pytest.raises(SpecError, match="AMBIGUOUS"):
        validate_python_spec(src, 'https://example.test', inv)

def test_accepts_ambiguous_role_name_with_first_on_line():
    inv = PageInventory('https://example.test', 'T', controls=[
        {'name': 'Next', 'tag': 'button', 'ambiguous': True},
    ])
    src = (
        'def test_next(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    button = page.get_by_role("button", name="Next", exact=True).first\n'
        '    observation_evidence(page, "n", lambda: expect(button).to_be_visible(), evidence_dir)\n'
    )
    validate_python_spec(src, 'https://example.test', inv)

def test_rejects_with_observation_evidence_context_manager():
    src = (
        'def test_x(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    h = page.get_by_role("heading", name="Hi", exact=True)\n'
        '    with observation_evidence(page, "x", lambda: expect(h).to_be_visible(), evidence_dir):\n'
        '        pass\n'
    )
    with pytest.raises(SpecError, match="context manager"):
        validate_python_spec(src, 'https://example.test')

def test_rejects_guessed_disabled_when_nothing_disabled():
    inv = PageInventory('https://example.test', 'T',
                        controls=[{'name': 'Search', 'tag': 'button'}])
    src = (
        'def test_x(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    b = page.get_by_role("button", name="Search", exact=True)\n'
        '    observation_evidence(page, "x", lambda: expect(b).to_be_disabled(), evidence_dir)\n'
    )
    with pytest.raises(SpecError, match="disabled"):
        validate_python_spec(src, 'https://example.test', inv)

def test_accepts_disabled_assertion_when_control_is_disabled():
    inv = PageInventory('https://example.test', 'T',
                        controls=[{'name': 'Search', 'tag': 'button', 'disabled': True}])
    src = (
        'def test_x(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    b = page.get_by_role("button", name="Search", exact=True)\n'
        '    observation_evidence(page, "x", lambda: expect(b).to_be_disabled(), evidence_dir)\n'
    )
    validate_python_spec(src, 'https://example.test', inv)

def test_rejects_verify_lambda_chaining_asserts_with_and():
    src = (
        'def test_x(page, evidence_dir):\n'
        '    # https://example.test\n'
        '    a = page.get_by_role("link", name="A", exact=True).first\n'
        '    b = page.get_by_role("link", name="B", exact=True).first\n'
        '    observation_evidence(page, "x",\n'
        '        lambda: expect(a).to_be_visible() and expect(b).to_be_visible(), evidence_dir)\n'
    )
    with pytest.raises(SpecError, match="and/or"):
        validate_python_spec(src, 'https://example.test')

def test_behavioural_count_credits_action_evidence(tmp_path):
    from website_test_pipeline.report import validate_url, UrlReport, TestOutcome
    spec = tmp_path / "s_test.py"
    spec.write_text(
        'def test_click_reveals(page, evidence_dir):\n'
        '    action_evidence(page, "c", lambda: page.locator("#b").click(),\n'
        '                    lambda: expect(page.locator("#panel")).to_be_visible(), evidence_dir)\n'
        'def test_just_visible(page, evidence_dir):\n'
        '    observation_evidence(page, "v", lambda: expect(page.locator("#h")).to_be_visible(), evidence_dir)\n',
        encoding="utf-8")
    rep = UrlReport(url="https://x.test", spec_path=str(spec))
    rep.outcomes = [
        TestOutcome(nodeid="", title="test_click_reveals", url="https://x.test", status="passed",
                    duration=0.0, assertions=['expect(page.locator("#panel")).to_be_visible()']),
        TestOutcome(nodeid="", title="test_just_visible", url="https://x.test", status="passed",
                    duration=0.0, assertions=['expect(page.locator("#h")).to_be_visible()']),
    ]
    validate_url(rep)
    # 1 of 2 is behavioural (the action_evidence one) -> total 2 < 4 so no warning,
    # but the point is the click test is not miscounted as pure-visibility.
    from website_test_pipeline.report import _title_calls_action
    from pathlib import Path
    assert _title_calls_action(Path(spec), "test_click_reveals")
    assert not _title_calls_action(Path(spec), "test_just_visible")

def _visibility_only_report(ceiling):
    from website_test_pipeline.report import UrlReport, TestOutcome
    rep = UrlReport(url="https://x.test", spec_path=None, generated_status="generated")
    rep.coverage_ceiling = ceiling
    rep.outcomes = [
        TestOutcome(nodeid="", title=f"test_h{i}", url="https://x.test", status="passed",
                    duration=0.0, assertions=['expect(page.locator("#h")).to_be_visible()'])
        for i in range(5)
    ]
    return rep

def test_constrained_page_gets_a_note_not_a_shallow_coverage_warning():
    from website_test_pipeline.report import validate_url
    rep = _visibility_only_report("the form is CAPTCHA-protected, so a real end-to-end submit cannot be automated")
    validate_url(rep)
    assert any("not a coverage gap" in w and "CAPTCHA" in w for w in rep.warnings)
    assert not any("shallow coverage" in w for w in rep.warnings)

def test_rich_page_with_lazy_tests_still_warns_shallow():
    from website_test_pipeline.report import validate_url
    rep = _visibility_only_report(None)
    validate_url(rep)
    assert any("shallow coverage" in w for w in rep.warnings)
    assert not any("not a coverage gap" in w for w in rep.warnings)

def test_behaviour_ceiling_detects_captcha_and_thin_and_none(tmp_path):
    from website_test_pipeline.report import _behaviour_ceiling
    assert "CAPTCHA" in (_behaviour_ceiling({"controls": [{"name": "Enter the CAPTCHA code", "tag": "input"}]}) or "")
    assert "interactive control" in (_behaviour_ceiling({"controls": [{"tag": "button", "name": "X", "region": "content"}]}) or "")
    rich = {"controls": [{"tag": "button", "name": f"b{i}", "region": "content"} for i in range(8)]}
    assert _behaviour_ceiling(rich) is None
    assert _behaviour_ceiling(None) is None

def test_validator_accepts_locator_from_revealed_inventory():
    inv = PageInventory('https://example.test', 'T',
                        controls=[{'selector': '#trigger', 'name': 'Reveal'}],
                        revealed=[{"trigger": "Reveal", "effect": "reveals",
                                   "controls": [{"selector": "#panel", "name": "Result panel"}]}])
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    panel = page.locator("#panel")\n'
           '    observation_evidence(page, "panel", lambda: expect(panel).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test', inv)

def test_validator_accepts_class_error_selector_from_revealed_validation():
    # Drupal's error block has no id; explorer emits a class selector like
    # '.messages--error' - the model must be free to use it verbatim.
    inv = PageInventory('https://example.test', 'T',
                        controls=[{'field_name': 'email', 'name': 'Email', 'required': True}],
                        forms=[{'selector': 'form#subscribe', 'fields': ['email']}],
                        revealed=[{"trigger": "submit form#subscribe with no input",
                                   "effect": "validation",
                                   "controls": [{"tag": "div", "role": None, "name": "error",
                                                 "selector": ".messages.messages--error",
                                                 "region": "content"}]}])
    src = ('def test_subscribe_requires_email(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    def act():\n'
           '        page.locator(\'form#subscribe button[type="submit"]\').first.click()\n'
           '    action_evidence(page, "submit empty", act,\n'
           '        lambda: expect(page.locator(".messages.messages--error")).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test', inv)

# ------------------------------------------- reject to_be_visible on hidden element

def _sr_only_inv():
    return PageInventory('https://example.test', 'T',
        headings=[{'level': 'H1', 'text': 'Al Jazeera Media Network', 'hidden': True}],
        controls=[{'tag': 'input', 'type': 'checkbox', 'name': 'BBC One',
                   'selector': '#chan-bbc', 'hidden': True}])

def test_rejects_to_be_visible_on_sr_only_heading():
    src = ('def test_h1(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    h = page.get_by_role("heading", name="Al Jazeera Media Network", exact=True)\n'
           '    observation_evidence(page, "h", lambda: expect(h).to_be_visible(), evidence_dir)\n')
    with pytest.raises(SpecError, match='HIDDEN'):
        validate_python_spec(src, 'https://example.test', _sr_only_inv())

def test_accepts_to_have_count_on_sr_only_heading():
    src = ('def test_h1(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    h = page.get_by_role("heading", name="Al Jazeera Media Network", exact=True)\n'
           '    observation_evidence(page, "h", lambda: expect(h).to_have_count(1), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test', _sr_only_inv())

def test_rejects_to_be_checked_on_hidden_checkbox_inline():
    src = ('def test_c(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    observation_evidence(page, "c", lambda: expect(page.locator("#chan-bbc")).to_be_checked(), evidence_dir)\n')
    with pytest.raises(SpecError, match='HIDDEN'):
        validate_python_spec(src, 'https://example.test', _sr_only_inv())

# --------------------------------------------------- strict get_by_role name match

def _footer_inv():
    return PageInventory('https://example.test', 'T',
        headings=[{'level': 'H1', 'text': 'Welcome'}],
        controls=[
            {'tag': 'a', 'name': 'About Al Jazeera', 'href': '/about', 'region': 'chrome'},
            {'tag': 'a', 'name': 'Connect With Us', 'href': '/connect', 'region': 'chrome'},
            {'tag': 'input', 'field_name': 'email', 'name': 'Email address', 'placeholder': 'Email address'},
        ])

def test_rejects_truncated_role_name_hallucinated_footer_link():
    src = ('def test_footer(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    link = page.get_by_role("link", name="About", exact=True).first\n'
           '    observation_evidence(page, "a", lambda: expect(link).to_be_visible(), evidence_dir)\n')
    with pytest.raises(SpecError, match='not in observed inventory'):
        validate_python_spec(src, 'https://example.test', _footer_inv())

def test_accepts_full_role_name():
    src = ('def test_footer(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    link = page.get_by_role("link", name="About Al Jazeera", exact=True).first\n'
           '    observation_evidence(page, "a", lambda: expect(link).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test', _footer_inv())

def test_accepts_full_role_name_with_trailing_punctuation():
    src = ('def test_footer(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    link = page.get_by_role("link", name="Connect With Us:", exact=True).first\n'
           '    observation_evidence(page, "c", lambda: expect(link).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test', _footer_inv())

def test_get_by_label_still_allows_substring_match():
    # Playwright get_by_label matches as a substring - "Email" is fine for "Email address".
    src = ('def test_form(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    field = page.get_by_label("Email")\n'
           '    observation_evidence(page, "e", lambda: expect(field).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test', _footer_inv())

# ------------------------------------------------------------ map / media embeds

def test_compact_embeds_renders_provider_and_selector():
    from website_test_pipeline.generator import _compact_embeds
    out = _compact_embeds([
        {"kind": "map", "provider": "google-maps-js", "selector": "#map", "region": "other", "big": True},
        {"kind": "map", "provider": "google.com/maps", "selector": "iframe", "region": "content",
         "title": "Al Jazeera coverage map"},
    ])
    assert "#map" in out and "google-maps-js" in out
    assert 'title="Al Jazeera coverage map"' in out

def test_prompt_includes_embeds_block_and_rule_when_map_present():
    from website_test_pipeline.generator import prompt_for
    inv = PageInventory('https://example.test/map', 'Map',
                        embeds=[{"kind": "map", "provider": "canvas", "selector": "#map",
                                 "region": "other", "big": True}])
    prompt = prompt_for("GUIDE", "persona", inv)
    assert "PAGE EMBEDS:" in prompt and "#map" in prompt
    assert "THIRD-PARTY MAP / MEDIA EMBED" in prompt

def test_prompt_omits_embeds_block_when_none():
    from website_test_pipeline.generator import prompt_for
    inv = PageInventory('https://example.test', 'T')
    assert "PAGE EMBEDS:" not in prompt_for("G", "p", inv)

def test_validator_accepts_map_container_selector_from_embeds():
    inv = PageInventory('https://example.test/map', 'Map',
                        embeds=[{"kind": "map", "provider": "google-maps-js",
                                 "selector": "#map", "region": "other"}])
    src = ('def test_map_renders(page, evidence_dir):\n'
           '    # https://example.test/map\n'
           '    observation_evidence(page, "map", lambda: expect(page.locator("#map")).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test/map', inv)

def test_validator_accepts_iframe_fallback_for_selectorless_embed():
    inv = PageInventory('https://example.test/map', 'Map',
                        embeds=[{"kind": "map", "provider": "google.com/maps",
                                 "selector": "iframe", "region": "content"}])
    src = ('def test_map_iframe(page, evidence_dir):\n'
           '    # https://example.test/map\n'
           '    observation_evidence(page, "m", lambda: expect(page.locator("iframe").first).to_be_visible(), evidence_dir)\n')
    validate_python_spec(src, 'https://example.test/map', inv)
