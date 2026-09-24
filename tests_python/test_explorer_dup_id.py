import pytest

from website_test_pipeline.explorer import _CONTROL_SEL, _CONTROLS_JS


@pytest.fixture(scope="module")
def page():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium here: {exc}")
        yield browser.new_page()
        browser.close()


def test_an_id_used_by_two_elements_is_flagged_so_a_bare_locator_gets_first(page):
    page.set_content('<main><div id="box" role="main">a</div><button id="go">Go</button><button id="dup">One</button>'
                     '<button id="dup">Two</button></main>')
    controls = {c["id"]: c for c in page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS) if c.get("id")}
    assert controls["dup"]["dup_id"] is True
    assert controls["go"]["dup_id"] is False
