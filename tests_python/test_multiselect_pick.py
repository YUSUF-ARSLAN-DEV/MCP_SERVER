import threading

import pytest

from website_test_pipeline.explorer import _pick_multiselect

# jQuery UI multiselect shape: a header list of links (check all / uncheck all / a hidden close link) comes BEFORE the options.
PAGE = """<!doctype html><html><body>
<button type="button" id="ms" onclick="document.getElementById('menu').style.display='block'">Please select a channel</button>
<div class="ui-multiselect-menu" id="menu" style="display:none">
  <div class="ui-multiselect-header"><ul>
    <li><a class="ui-multiselect-all" href="#">Check all</a></li>
    <li><a class="ui-multiselect-none" href="#">Uncheck all</a></li>
    <li class="ui-multiselect-close"><a class="ui-multiselect-close" href="#" style="display:none">close</a></li>
  </ul></div>
  <ul class="ui-multiselect-checkboxes">
    <li><label><input type="checkbox" value="all"> Select All</label></li>
    <li><label><input type="checkbox" value="1" onchange="document.getElementById('out').textContent='picked '+this.value"> Al Jazeera 2 Channel</label></li>
    <li><label><input type="checkbox" value="2"> Aljazeera English Channel HD</label></li>
  </ul>
</div><p id="out"></p></body></html>"""


def _run(check):
    box = {}

    def work():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch()
                except Exception as exc:
                    box["skip"] = f"no chromium here: {exc}"
                    return
                page = browser.new_page()
                page.set_content(PAGE)
                box["value"] = check(page)
                browser.close()
        except BaseException as exc:
            box["error"] = exc
    thread = threading.Thread(target=work)
    thread.start(); thread.join()
    if "skip" in box:
        pytest.skip(box["skip"])
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_the_picker_takes_one_real_option_not_the_menu_headers_hidden_links_and_not_select_all():
    def check(page):
        picked = _pick_multiselect(page, {"tag": "button", "name": "Please select a channel", "role": None, "selector": None})
        return picked, page.locator("#out").inner_text()
    picked, out = _run(check)
    assert picked == "Al Jazeera 2 Channel" and out == "picked 1"
