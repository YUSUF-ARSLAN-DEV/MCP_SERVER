"""Only a real search box gets a search term typed into it (found live: "search for 'the'" was typed into the Name
field of a subscription form and became a journey). Decided from the form's shape, not its words."""
from website_test_pipeline.explorer import _FORMS_JS, _probe_search_form, looks_like_search

SIGNUP = """<form id="sub"><input name="name"><input name="mail" type="email"><input name="city">
<input name="mobile" type="tel"><select name="country"><option>Qatar</option></select><button>تسجيل</button></form>"""
SEARCH_ROLE = """<form id="s1" role="search"><input name="q"><input name="where"><button>Go</button></form>"""
SEARCH_TYPE = """<form id="s2"><input type="search" name="k"><button>Go</button></form>"""
ONE_FIELD = """<form id="s3"><input name="s" placeholder="ابحث"><button>بحث</button></form>"""
NEWSLETTER = """<form id="n"><input name="email" type="email"><button>Join</button></form>"""


def _forms(page):
    page.set_content(SIGNUP + SEARCH_ROLE + SEARCH_TYPE + ONE_FIELD + NEWSLETTER)
    return {f["selector"]: f for f in page.locator("form").evaluate_all(_FORMS_JS)}


def test_the_shape_of_each_form_is_recorded(page):
    forms = _forms(page)
    assert forms["#sub"]["text_fields"] == 4 and forms["#sub"]["contact_fields"] == 2
    assert forms["#s1"]["search_role"] is True and forms["#s2"]["search_role"] is True
    assert forms["#s3"]["text_fields"] == 1 and forms["#s3"]["search_role"] is False


def test_only_search_shaped_forms_count_as_search(page):
    forms = _forms(page)
    assert [sel for sel, f in forms.items() if looks_like_search(f)] == ["#s1", "#s2", "#s3"]


def test_an_inventory_from_before_the_shape_was_recorded_keeps_the_old_behaviour():
    assert looks_like_search({"selector": "#f", "fields": ["s"]}) is True


class _Recorder:
    """Records whether the probe tried to open the page (it only does that for a form it means to type into)."""
    def __init__(self):
        self.visits = []

    def goto(self, url, **kw):
        self.visits.append(url)
        raise RuntimeError("no browser in this test")


def test_the_search_probe_never_types_into_a_subscription_form():
    page = _Recorder()
    signup = {"selector": "#sub", "fields": ["name", "mail", "city"], "has_password": False,
              "search_role": False, "text_fields": 4, "contact_fields": 2}
    assert _probe_search_form(page, "https://x.test/ar/subscribe", [signup], [], None) is None
    assert page.visits == []
    search = {"selector": "#s3", "fields": ["s"], "has_password": False, "search_role": False, "text_fields": 1, "contact_fields": 0}
    _probe_search_form(page, "https://x.test/", [search], [], None)
    assert page.visits == ["https://x.test/"]
