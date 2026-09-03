import ast
import pytest

from website_test_pipeline.autorepair import repair_spec
from website_test_pipeline.validator import validate_python_spec, SpecError
from website_test_pipeline.models import PageInventory


def _parses(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


# --------------------------------------------------------------- :has-text

def test_rewrites_has_text_button_to_get_by_role():
    src = 'x = page.locator("button:has-text(\'Search\')")\n'
    out, applied = repair_spec(src)
    assert 'get_by_role("button", name="Search", exact=True)' in out
    assert ":has-text" not in out
    assert applied and "has-text" in applied[0]

def test_rewrites_has_text_anchor_to_link_role():
    src = "x = page.locator('a:has-text(\"News\")')\n"
    out, _ = repair_spec(src)
    assert 'get_by_role("link", name="News", exact=True)' in out
    assert ":has-text" not in out

def test_has_text_with_apostrophe_in_text_uses_double_quotes():
    src = "x = page.locator(\"a:has-text('Fisherman\\\\'s report')\")\n"
    # apostrophe in the text -> code should not emit a broken single-quoted string
    out, applied = repair_spec(src)
    assert _parses(out)

def test_leaves_has_text_when_prefix_has_no_known_role():
    src = 'x = page.locator(".cta:has-text(\'Go\')")\n'
    out, applied = repair_spec(src)
    assert out == src and applied == []

def test_has_text_rewrite_survives_validator():
    inv = PageInventory('https://example.test', 'T',
                        controls=[{'name': 'Search', 'tag': 'button'}])
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    btn = page.locator("button:has-text(\'Search\')")\n'
           '    observation_evidence(page, "s", lambda: expect(btn).to_be_visible(), evidence_dir)\n')
    with pytest.raises(SpecError):
        validate_python_spec(src, 'https://example.test', inv)
    repaired, _ = repair_spec(src, inv)
    validate_python_spec(repaired, 'https://example.test', inv)


# --------------------------------------------------------------- and/or chain

def test_rewrites_and_chain_to_list():
    src = 'f = lambda: expect(a).to_be_visible() and expect(b).to_be_visible()\n'
    out, applied = repair_spec(src)
    assert out.strip() == 'f = lambda: [expect(a).to_be_visible(), expect(b).to_be_visible()]'
    assert applied and "and/or" in applied[0]

def test_rewrites_three_way_or_chain():
    src = 'f = lambda: expect(a).to_be_visible() or expect(b).to_be_visible() or expect(c).to_be_visible()\n'
    out, _ = repair_spec(src)
    assert out.strip() == 'f = lambda: [expect(a).to_be_visible(), expect(b).to_be_visible(), expect(c).to_be_visible()]'

def test_and_chain_rewrite_survives_validator():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    a = page.get_by_role("link", name="A", exact=True).first\n'
           '    b = page.get_by_role("link", name="B", exact=True).first\n'
           '    observation_evidence(page, "x",\n'
           '        lambda: expect(a).to_be_visible() and expect(b).to_be_visible(), evidence_dir)\n')
    with pytest.raises(SpecError, match="and/or"):
        validate_python_spec(src, 'https://example.test')
    repaired, _ = repair_spec(src)
    validate_python_spec(repaired, 'https://example.test')


# --------------------------------------------------------------- missing .first

def _amb_inv():
    return PageInventory('https://example.test', 'T', controls=[
        {'name': 'Next', 'tag': 'button', 'ambiguous': True, 'field_name': 'next'},
    ])

def test_adds_first_to_ambiguous_role_name_assignment():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    nxt = page.get_by_role("button", name="Next", exact=True)\n'
           '    observation_evidence(page, "n", lambda: expect(nxt).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src, _amb_inv())
    assert 'name="Next", exact=True).first' in out
    assert applied and ".first" in applied[-1]
    validate_python_spec(out, 'https://example.test', _amb_inv())

def test_adds_first_to_name_attr_locator():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    nxt = page.locator(\'input[name="next"]\')\n'
           '    observation_evidence(page, "n", lambda: expect(nxt).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src, _amb_inv())
    assert '\'input[name="next"]\').first' in out

def test_does_not_double_up_first():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    nxt = page.get_by_role("button", name="Next", exact=True).first\n'
           '    observation_evidence(page, "n", lambda: expect(nxt).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src, _amb_inv())
    assert out == src and applied == []

def test_adds_first_before_click_chain():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    def act():\n'
           '        page.get_by_role("button", name="Next", exact=True).click()\n'
           '    action_evidence(page, "n", act, lambda: expect(page).to_have_url(re.compile(r"/x")), evidence_dir)\n')
    out, _ = repair_spec(src, _amb_inv())
    assert 'name="Next", exact=True).first.click()' in out


# --------------------------------------------------------------- safety

def test_no_repairs_leaves_source_identical():
    src = 'def test_x(page):\n    expect(page).to_have_title("Hi")\n'
    out, applied = repair_spec(src)
    assert out == src and applied == []

def test_output_always_parses():
    src = ('def test_x(page, evidence_dir):\n'
           '    b = page.locator("button:has-text(\'Go\')")\n'
           '    f = lambda: expect(a).to_be_visible() and expect(b).to_be_visible()\n')
    out, applied = repair_spec(src)
    assert applied
    assert _parses(out)
