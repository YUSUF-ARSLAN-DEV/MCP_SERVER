import pytest
from pathlib import Path
from website_test_pipeline.generator import extract_code
from website_test_pipeline.validator import validate_python_spec, SpecError
from website_test_pipeline.models import PageInventory
from website_test_pipeline.urls import canonicalize
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

def test_rejects_bare_tag_locator():
    src = 'def test_x(page, evidence_dir):\n    observation_evidence(page, "d", lambda: expect(page.locator("div")).to_contain_text("hi"), evidence_dir)\n# https://example.test'
    with pytest.raises(SpecError, match='every'):
        validate_python_spec(src, 'https://example.test')

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
