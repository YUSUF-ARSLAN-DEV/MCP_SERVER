"""Controls that are not the site's own are left out of what the AI may test (both found live on a production site):
a hidden skip link was clicked and reported as a High defect, and the "Terms" link inside an embedded Google Map was
looked for in the site's footer and reported as a missing footer."""
import pytest

from website_test_pipeline.explorer import _CONTROL_SEL, _CONTROLS_JS

PAGE = """
<a href="#main-content" style="position:absolute;top:-40px">تجاوز إلى المحتوى الرئيسي</a>
<header><nav><a href="/en">Home</a><a href="#footer-links">Contact</a></nav></header>
<main id="main-content">
  <h1>Satellite Frequencies</h1>
  <button>Search</button>
  <a href="#results">Jump to results</a>
  <div id="results">results</div>
  <div class="map"><div class="gm-style">
    <button aria-label="Map camera controls">cam</button>
    <a href="https://maps.google.com/terms">Terms (opens in new tab)</a>
  </div></div>
</main>
<footer id="footer-links"><a href="/en/privacy">Privacy</a></footer>
"""


@pytest.fixture(scope="module")
def controls():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium here: {exc}")
        page = browser.new_page()
        page.set_content(PAGE)
        found = {c["name"]: c for c in page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)}
        browser.close()
        return found


def test_a_skip_link_is_recognised_by_its_shape_in_any_language(controls):
    assert controls["تجاوز إلى المحتوى الرئيسي"]["skip_link"] is True


def test_an_ordinary_in_page_link_further_down_is_not_a_skip_link(controls):
    assert controls["Jump to results"]["skip_link"] is False        # an in-page anchor, but not one of the first stops
    assert controls["Search"]["skip_link"] is False


def test_controls_inside_an_embedded_map_belong_to_the_map_provider(controls):
    assert controls["Map camera controls"]["third_party"] is True
    assert controls["Terms (opens in new tab)"]["third_party"] is True
    assert controls["Privacy"]["third_party"] is False and controls["Search"]["third_party"] is False


def test_explore_leaves_them_out_of_the_inventory():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    from website_test_pipeline.explorer import explore
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium here: {exc}")
        page = browser.new_page()
        page.set_content(PAGE)
        inventory = explore(page, "https://x.test/en/map", probe_max=0)
        browser.close()
    names = {c["name"] for c in inventory.controls}
    assert {"Search", "Privacy", "Home"} <= names
    assert not names & {"تجاوز إلى المحتوى الرئيسي", "Map camera controls", "Terms (opens in new tab)"}
