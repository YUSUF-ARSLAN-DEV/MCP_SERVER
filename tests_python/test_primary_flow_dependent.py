"""A list that fills in only after an earlier choice (country -> that country's channels) must be waited for, and a
journey whose choices could not all be made is not recorded (found live: "Search + 1 step -> no visible result"
became an unverified journey in every run, because the channel list was still empty when the probe looked)."""
from website_test_pipeline.explorer import _CONTROL_SEL, _CONTROLS_JS, _probe_primary_flow

PAGE = """<!doctype html><html><body><main>
<h1>Frequencies</h1>
<select id="countrylist"><option value="">Please select a country</option><option value="af">Afghanistan</option>
  <option value="eg">Egypt</option></select>
<button id="chan" type="button" onclick="var m=document.getElementById('menu');m.style.display=m.style.display==='block'?'none':'block'">Please select a channel</button>
<div id="menu" class="ui-multiselect-menu" style="display:none;position:absolute;background:#fff"><ul id="opts"></ul></div>
<button id="go" type="button" onclick="show()">Search</button>
<div id="results"></div>
</main>
<script>
document.getElementById('countrylist').addEventListener('change', function () {
  if (window.NEVER_LOADS) return;
  setTimeout(function () {                                  // the channels arrive from the server a little later
    document.getElementById('opts').innerHTML =
      '<li><label><input type="checkbox"> Al Jazeera Arabic</label></li><li><label><input type="checkbox"> Al Jazeera English</label></li>';
  }, 1500);
});
document.getElementById('opts').addEventListener('click', function (e) { this.dataset.picked = '1'; });
function show() {
  if (!document.getElementById('countrylist').value || !document.getElementById('opts').dataset.picked) return;
  document.getElementById('results').innerHTML = '<table>' +
    ['11766 H 27500', '11843 V 27500', '12034 H 27500', '12111 V 27500'].map(function (f) { return '<tr><td>' + f + ' frequency row</td></tr>'; }).join('') + '</table>';
}
</script></body></html>"""


def _probe(page, tmp_path, html):
    path = tmp_path / "freq.html"
    path.write_text(html, encoding="utf-8")
    url = path.as_uri()
    page.goto(url)
    controls = page.locator(_CONTROL_SEL).evaluate_all(_CONTROLS_JS)
    return _probe_primary_flow(page, url, controls, [], [])


def test_the_probe_waits_for_a_list_that_fills_in_after_an_earlier_choice(page, tmp_path):
    flow = _probe(page, tmp_path, PAGE)
    assert flow is not None
    assert [s["kind"] for s in flow["steps"]] == ["select", "multiselect"]
    assert flow["steps"][1]["value"] == "Al Jazeera Arabic"
    assert flow["effect"] == "results"


def test_a_journey_whose_choices_could_not_all_be_made_is_not_recorded(page, tmp_path):
    never = PAGE.replace("<script>", "<script>window.NEVER_LOADS = true;")
    assert _probe(page, tmp_path, never) is None
