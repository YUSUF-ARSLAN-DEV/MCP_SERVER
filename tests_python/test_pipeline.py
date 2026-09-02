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
    assert not _is_probe_trigger({"tag": "input", "type": "submit", "name": "Go", "region": "content"})
    assert not _is_probe_trigger({"tag": "button", "name": "Go", "region": "chrome"})

def test_compact_revealed_renders_trigger_and_controls():
    revealed = [
        {"trigger": "Show frequencies", "effect": "reveals",
         "controls": [{"tag": "div", "name": "Frequency results", "selector": "#freq-list", "region": "content"}]},
        {"trigger": "Open map", "effect": "navigates", "to": "https://site.test/map/full?x=1"},
    ]
    out = _compact_revealed(revealed)
    assert 'click "Show frequencies" -> reveals:' in out
    assert "#freq-list" in out
    assert 'click "Open map" -> navigates to /map/full' in out

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
