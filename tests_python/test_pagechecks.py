import pytest

from website_test_pipeline.pagechecks import PageHealth, health_findings, _same_site


def _inv(health, **extra):
    return {"url": "https://shop.example.com/", "headings": [{"text": "Hi"}], "controls": [{"name": "Go"}], "health": health, **extra}


def _kinds(inv):
    return {f.kind: f.severity for f in health_findings(inv)}


def test_a_page_with_no_recorded_health_gives_no_findings():
    assert health_findings({"url": "https://a.example/"}) == []


def test_the_page_itself_answering_500_is_p1():
    assert _kinds(_inv({"main_status": 500})) == {"page_http_error": "P1"}


def test_a_script_error_is_p2_and_names_the_message():
    findings = health_findings(_inv({"page_errors": [{"text": "TypeError: x is undefined"}]}))
    assert [(f.kind, f.severity) for f in findings] == [("script_error", "P2")]
    assert "TypeError: x is undefined" in findings[0].summary


def test_only_the_sites_own_failed_requests_count_third_party_noise_is_ignored():
    own = {"url": "https://shop.example.com/app.js", "status": 404, "type": "script", "same_site": True}
    ads = {"url": "https://ads.tracker.net/x.js", "status": 500, "type": "script", "same_site": False}
    kinds = [f.kind for f in health_findings(_inv({"bad_responses": [own, ads]}))]
    assert kinds == ["failed_request"]


def test_a_404_on_a_plain_own_image_is_only_p3_but_a_5xx_is_p2():
    img404 = {"url": "https://shop.example.com/a.png", "status": 404, "type": "image", "same_site": True}
    img500 = {"url": "https://shop.example.com/b.png", "status": 503, "type": "image", "same_site": True}
    assert [f.severity for f in health_findings(_inv({"bad_responses": [img404]}))] == ["P3"]
    assert [f.severity for f in health_findings(_inv({"bad_responses": [img500]}))] == ["P2"]


def test_third_party_console_errors_are_not_reported():
    err = {"text": "blocked", "source": "https://ads.tracker.net/x.js", "same_site": False}
    assert health_findings(_inv({"console_errors": [err]})) == []


def test_measured_quality_problems_are_p3_each_with_a_sample():
    measured = {"title": "", "h1_count": 0, "body_text_chars": 500, "has_lang": False,
                "controls_without_name_count": 2, "controls_without_name": ["<button></button>"],
                "fields_without_label_count": 1, "fields_without_label": ["<input>"],
                "images_without_alt_count": 1, "images_without_alt": ["https://shop.example.com/a.png"],
                "broken_image_count": 1, "broken_images": ["https://shop.example.com/gone.png"]}
    kinds = _kinds(_inv({"measured": measured}))
    assert kinds == {"broken_image": "P2", "missing_title": "P3", "missing_h1": "P3", "unnamed_control": "P3",
                     "unlabeled_field": "P3", "image_without_alt": "P3", "missing_lang": "P3"}


def test_a_blank_page_is_p1_but_a_short_page_with_controls_is_not():
    blank = {"measured": {"body_text_chars": 0, "title": "t", "has_lang": True}}
    assert _kinds({"url": "https://a.example/", "controls": [], "headings": [], "health": blank}) == {"blank_page": "P1"}
    assert "blank_page" not in _kinds(_inv(blank))


@pytest.mark.parametrize("a,b,expected", [
    ("https://www.shop.example.com/x", "https://shop.example.com/", True),
    ("https://cdn.shop.example.com/a.js", "https://shop.example.com/", True),
    ("https://ads.tracker.net/a.js", "https://shop.example.com/", False),
    ("", "https://shop.example.com/", False),
])
def test_same_site_treats_www_and_subdomains_as_the_site(a, b, expected):
    assert _same_site(a, b) is expected


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


def test_measure_counts_unnamed_controls_unlabeled_fields_and_missing_alt(page):
    page.set_content('<html><head><title>T</title></head><body><h1>Hi</h1><button></button><button>Ok</button>'
                     '<input type="text"><label for="n">Name</label><input id="n"><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
                     '<p>enough visible text to not be blank</p></body></html>')
    health = PageHealth(page, "https://shop.example.com/")
    health.measure(page)
    m = health.measured
    assert (m["controls_without_name_count"], m["fields_without_label_count"], m["images_without_alt_count"]) == (1, 1, 1)
    assert m["h1_count"] == 1 and m["title"] == "T" and m["has_lang"] is False


def test_a_script_error_is_recorded_while_listening_and_not_after_freeze(page):
    health = PageHealth(page, "https://shop.example.com/")
    page.set_content("<body><script>throw new Error('boom')</script></body>")
    page.wait_for_timeout(100)
    assert [e["text"] for e in health.page_errors] == ["Error: boom"]
    health.freeze()
    page.set_content("<body><script>throw new Error('later')</script></body>")
    page.wait_for_timeout(100)
    assert len(health.page_errors) == 1
