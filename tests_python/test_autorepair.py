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

def test_does_not_double_up_first_with_non_ascii_name():
    # ast col_offsets are utf-8 byte offsets; the "already has .first?" check must
    # not misfire on Arabic text and produce `.first.first`.
    inv = PageInventory('https://example.test', 'T', controls=[
        {'name': 'التالي', 'tag': 'button', 'ambiguous': True},
    ])
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    nxt = page.get_by_role("button", name="التالي", exact=True).first\n'
           '    observation_evidence(page, "n", lambda: expect(nxt).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src, inv)
    assert out == src and applied == []
    assert ".first.first" not in out

def test_adds_first_before_click_chain():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://example.test\n'
           '    def act():\n'
           '        page.get_by_role("button", name="Next", exact=True).click()\n'
           '    action_evidence(page, "n", act, lambda: expect(page).to_have_url(re.compile(r"/x")), evidence_dir)\n')
    out, _ = repair_spec(src, _amb_inv())
    assert 'name="Next", exact=True).first.click()' in out


# ----------------------------------------------- opaque <select> value assertions

def test_downgrades_select_value_assert_in_action_evidence():
    src = ('def test_flow(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    def act():\n'
           '        page.locator("#countrylist").select_option(label="Egypt")\n'
           '    action_evidence(page, "01-country", act,\n'
           '        lambda: expect(page.locator("#countrylist")).to_have_value("37"), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert '.not_to_have_value("")' in out
    assert 'to_have_value("37")' not in out
    assert applied and "select-value" in applied[-1]
    assert _parses(out)

def test_downgrades_select_value_assert_via_variable():
    src = ('def test_flow(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    sel = page.locator("#c")\n'
           '    sel.select_option(label="Egypt")\n'
           '    observation_evidence(page, "c", lambda: expect(sel).to_have_value("-16"), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert 'expect(sel).not_to_have_value("")' in out
    assert applied

def test_leaves_input_fill_value_assert_alone():
    # a real, passing pattern: fill a number field then assert it - no select_option, untouched
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    page.locator("#qty").fill("1")\n'
           '    observation_evidence(page, "q", lambda: expect(page.locator("#qty")).to_have_value("1"), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert out == src and applied == []

def test_downgrades_non_numeric_select_value_assert():
    # select_option(label="Weekly") -> the value attr may be "3", "weekly", anything;
    # it is unknowable from the page, so any to_have_value assert is downgraded.
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    page.locator("#c").select_option(label="Weekly")\n'
           '    observation_evidence(page, "c", lambda: expect(page.locator("#c")).to_have_value("weekly"), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert 'not_to_have_value("")' in out and 'to_have_value("weekly")' not in out
    assert applied

def test_downgrades_select_label_value_assert_matching_label_text():
    # the regression: option text is now shown to the model, so it writes
    # to_have_value("<label>") - but the <option value> is a numeric id.
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    c = page.locator("#countrylist")\n'
           '    c.select_option(label="Aruba")\n'
           '    observation_evidence(page, "c", lambda: expect(c).to_have_value("Aruba"), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert 'not_to_have_value("")' in out and 'to_have_value("Aruba")' not in out

def test_leaves_input_value_assert_alone_still():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    page.locator("#qty").fill("5")\n'
           '    observation_evidence(page, "q", lambda: expect(page.locator("#qty")).to_have_value("5"), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert out == src and applied == []


# ----------------------------------------------- sr-only multiselect option asserts

def test_downgrades_multiselect_option_visibility():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    opt = page.locator("#ui-multiselect-channellist-option-1")\n'
           '    observation_evidence(page, "o", lambda: expect(opt).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert 'to_have_count(1)' in out and 'to_be_visible' not in out
    assert any("multiselect-option" in a for a in applied)

def test_downgrades_multiselect_option_checked():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    observation_evidence(page, "o",\n'
           '        lambda: expect(page.locator("#ui-multiselect-channellist-option-2")).to_be_checked(), evidence_dir)\n')
    out, _ = repair_spec(src)
    assert 'to_have_count(1)' in out and 'to_be_checked' not in out

def test_retargets_multiselect_option_click_to_label():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    action_evidence(page, "c",\n'
           '        lambda: page.locator("#ui-multiselect-channellist-option-1").click(),\n'
           '        lambda: expect(page.locator("#ui-multiselect-channellist-option-1")).to_have_count(1), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert "label[for='ui-multiselect-channellist-option-1']" in out
    assert any("label" in a for a in applied)
    assert _parses(out)

def test_retargets_multiselect_option_click_via_variable():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    channel_checkbox = page.locator("#ui-multiselect-channellist-option-1")\n'
           '    action_evidence(page, "c", lambda: channel_checkbox.click(),\n'
           '        lambda: expect(channel_checkbox).to_have_count(1), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert "label[for='ui-multiselect-channellist-option-1']" in out
    assert "#ui-multiselect-channellist-option-1" not in out
    assert _parses(out)

def test_no_escape_before_in_menu_label_click():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    action_evidence(page, "open", lambda: page.get_by_role("button", name="Pick", exact=True).click(),\n'
           '        lambda: expect(page.locator("#ui-multiselect-x-option-1")).to_have_count(1), evidence_dir)\n'
           '    action_evidence(page, "sel", lambda: page.locator("label[for=\'ui-multiselect-x-option-1\']").click(),\n'
           '        lambda: expect(page.locator("#ui-multiselect-x-option-1")).to_have_count(1), evidence_dir)\n')
    out, _ = repair_spec(src)
    assert "Escape" not in out   # next click is into the menu - don't close it

def test_leaves_normal_visibility_assert_alone():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    observation_evidence(page, "o", lambda: expect(page.locator("#real-panel")).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert out == src and applied == []


# --------------------------------------------------------------- safety

def _map_inv():
    return PageInventory('https://x.test/map', 'Map',
                         embeds=[{"kind": "map", "provider": "google-maps-js",
                                  "selector": "#map", "content_selector": "#map .gm-style"}])

def test_injects_map_settle_when_missing():
    src = ('def test_map(page, evidence_dir):\n'
           '    _open(page)\n'
           '    observation_evidence(page, "m", lambda: expect(page.locator("#map .gm-style")).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src, _map_inv())
    assert "page.wait_for_timeout(3000)" in out
    assert out.index("wait_for_timeout") < out.index("observation_evidence")
    assert applied and "map settle" in applied[-1]
    assert _parses(out)

def test_does_not_add_map_settle_when_already_present():
    src = ('def test_map(page, evidence_dir):\n'
           '    _open(page)\n'
           '    page.wait_for_timeout(5000)\n'
           '    observation_evidence(page, "m", lambda: expect(page.locator("#map .gm-style")).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src, _map_inv())
    assert out == src and applied == []

def test_no_map_settle_without_map_embed():
    src = ('def test_x(page, evidence_dir):\n'
           '    _open(page)\n'
           '    observation_evidence(page, "c", lambda: expect(page.locator("#thing canvas")).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src, PageInventory('https://x.test', 'T'))
    assert out == src and applied == []

# ----------------------------------------------- fragile exact-heading names

_LONG_EN = ("Discover how we help organizations achieve their goals through "
            "practical people focused technology")
_LONG_AR = "خطوات توليف جهاز استقبال الاقمار الصناعية لقنوات الجزيرة"

def test_rewrites_long_english_heading_to_regex():
    src = (f'def test_x(page, evidence_dir):\n'
           f'    # https://x.test\n'
           f'    h = page.get_by_role("heading", name="{_LONG_EN}", exact=True)\n'
           f'    observation_evidence(page, "h", lambda: expect(h).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert "re.compile(r'" in out and _LONG_EN not in out
    assert applied and "re.compile" in applied[-1]
    assert _parses(out)

def test_rewrites_long_arabic_heading_and_survives_validator():
    src = ('import re\n'
           'def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           f'    h = page.get_by_role("heading", name="{_LONG_AR}", exact=True)\n'
           '    observation_evidence(page, "h", lambda: expect(h).to_be_visible(), evidence_dir)\n')
    with pytest.raises(SpecError, match="fragile"):
        validate_python_spec(src, "https://x.test")
    out, applied = repair_spec(src)
    assert "re.compile(r'" in out
    validate_python_spec(out, "https://x.test")   # no longer fragile

def test_leaves_short_heading_alone():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    h = page.get_by_role("heading", name="Contact us", exact=True)\n'
           '    observation_evidence(page, "h", lambda: expect(h).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert out == src and applied == []


# ----------------------------------------------- guessed select_option(label=)

def _channel_inv():
    return PageInventory('https://x.test/sub', 'Sub', controls=[
        {'tag': 'select', 'id': 'edit-field-channel-sub-und',
         'selector': '#edit-field-channel-sub-und',
         'options': ['- Select -', 'Al Jazeera Arabic', 'Al Jazeera Mubasher', 'AJ+']},
    ])

def test_repoints_guessed_channel_label_to_index():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test/sub\n'
           '    def act():\n'
           '        page.locator("#edit-field-channel-sub-und").select_option(label="Al Jazeera News")\n'
           '    action_evidence(page, "c", act,\n'
           '        lambda: expect(page.locator("#edit-field-channel-sub-und")).not_to_have_value(""), evidence_dir)\n')
    out, applied = repair_spec(src, _channel_inv())
    assert 'select_option(index=1)' in out and 'label="Al Jazeera News"' not in out
    assert applied and "index=1" in applied[-1]
    assert _parses(out)

def test_leaves_real_observed_option_label_alone():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test/sub\n'
           '    page.locator("#edit-field-channel-sub-und").select_option(label="Al Jazeera Arabic")\n'
           '    observation_evidence(page, "c", lambda: expect(page.locator("#edit-field-channel-sub-und")).not_to_have_value(""), evidence_dir)\n')
    out, applied = repair_spec(src, _channel_inv())
    assert out == src and applied == []

def test_bad_select_label_noop_without_inventory():
    src = ('page.locator("#c").select_option(label="Whatever")\n')
    out, applied = repair_spec(src)
    assert out == src and applied == []


# ----------------------------------------------- unclosed menu overlay

def test_inserts_escape_between_menu_open_and_next_click():
    src = ('def test_search(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    channel = page.get_by_role("button", name="Select a channel", exact=True)\n'
           '    action_evidence(page, "01-open", lambda: channel.click(),\n'
           '        lambda: expect(page.locator("#ui-multiselect-channellist-option-1")).to_be_visible(), evidence_dir)\n'
           '    search = page.get_by_role("button", name="Search", exact=True)\n'
           '    action_evidence(page, "02-search", lambda: search.click(),\n'
           '        lambda: expect(page.get_by_role("heading", name="Results", exact=True)).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert 'page.keyboard.press("Escape")' in out
    assert out.index("Escape") < out.index('"02-search"')
    assert any("Escape" in a for a in applied)
    assert _parses(out)
    assert out.count('keyboard.press("Escape")') == 1   # exactly one, not a runaway

def test_escape_not_reinserted_when_already_present():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    b = page.get_by_role("button", name="Open", exact=True)\n'
           '    action_evidence(page, "o", lambda: b.click(),\n'
           '        lambda: expect(page.locator(".dropdown-menu")).to_be_visible(), evidence_dir)\n'
           '    page.keyboard.press("Escape")\n'
           '    action_evidence(page, "s", lambda: page.get_by_role("button", name="Go", exact=True).click(),\n'
           '        lambda: expect(page.get_by_role("heading", name="R", exact=True)).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert out.count('keyboard.press("Escape")') == 1 and applied == []

def test_no_escape_when_menu_step_is_last_click():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    b = page.get_by_role("button", name="Open", exact=True)\n'
           '    action_evidence(page, "o", lambda: b.click(),\n'
           '        lambda: expect(page.locator(".dropdown-menu")).to_be_visible(), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert "Escape" not in out and applied == []

def test_no_escape_for_plain_link_clicks():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    action_evidence(page, "a", lambda: page.get_by_role("link", name="A", exact=True).first.click(),\n'
           '        lambda: expect(page).to_have_url(re.compile(r"/a")), evidence_dir)\n'
           '    action_evidence(page, "b", lambda: page.get_by_role("link", name="B", exact=True).first.click(),\n'
           '        lambda: expect(page).to_have_url(re.compile(r"/b")), evidence_dir)\n')
    out, applied = repair_spec(src)
    assert "Escape" not in out


def _ro_inv():
    return PageInventory('https://x.test/wiz', 'Wiz', revealed=[
        {'trigger': 'Next', 'effect': 'reveals', 'controls': [
            {'tag': 'input', 'field_name': 'password', 'selector': 'input[name="password"]',
             'name': '0000', 'readonly': True},
        ]},
    ])

def test_converts_readonly_fill_to_visibility_check():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test/wiz\n'
           '    password_field = page.locator(\'input[name="password"]\')\n'
           '    action_evidence(page, "03-enter-password",\n'
           '        lambda: password_field.fill("0000"),\n'
           '        lambda: expect(password_field).to_have_value("0000"), evidence_dir)\n')
    out, applied = repair_spec(src, _ro_inv())
    assert '.fill(' not in out and 'observation_evidence(page, "03-enter-password"' in out
    assert 'to_be_visible()' in out
    assert applied and "readonly" in applied[-1]
    assert _parses(out)

def test_readonly_fill_rejected_by_validator_when_not_repaired():
    # inline .fill on a readonly id the autorepair regex can't shape-match still gets caught
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test/wiz\n'
           '    action_evidence(page, "p", lambda: page.locator(\'input[name="password"]\').fill("x"),\n'
           '        lambda: expect(page.locator(\'input[name="password"]\')).to_be_visible(), evidence_dir)\n')
    with pytest.raises(SpecError, match="READONLY"):
        validate_python_spec(src, 'https://x.test/wiz', _ro_inv())

def test_readonly_repair_noop_without_readonly_controls():
    src = ('def test_x(page, evidence_dir):\n'
           '    # https://x.test\n'
           '    f = page.locator("#name")\n'
           '    action_evidence(page, "n", lambda: f.fill("Jo"),\n'
           '        lambda: expect(f).to_have_value("Jo"), evidence_dir)\n')
    out, applied = repair_spec(src, PageInventory('https://x.test', 'T'))
    assert out == src and applied == []


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
