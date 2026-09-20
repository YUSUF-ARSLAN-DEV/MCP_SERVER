import json

import pytest

from website_test_pipeline import heuristics
from website_test_pipeline.flowgen import _query_expects
from website_test_pipeline.intents import IntentError, add_intent
from website_test_pipeline.pageutils import _loader_js, menu_selector, pick_option
from website_test_pipeline.runner import apply_result, sentence_expects_content


@pytest.fixture(autouse=True)
def _defaults_again():
    heuristics.configure(None)
    yield
    heuristics.configure(None)


def _file(tmp_path, data):
    path = tmp_path / "heuristics.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ------------------------------------------------------------------ the defaults are generic

def test_the_defaults_name_no_website_and_no_product():
    blob = json.dumps(heuristics.DEFAULTS).lower()
    for word in ("jazeera", "frequenc", "satellite", "sat-stg", "countrylist", "channel"):
        assert word not in blob, word


def test_the_default_word_lists_still_work_as_before():
    assert heuristics.has_word("content_words", "A visitor searches and sees the results page")
    assert heuristics.has_word("content_words", "shows a list of orders")
    assert not heuristics.has_word("content_words", "A visitor opens the subscribe page")
    assert heuristics.all_option_regex().match("Select All") and heuristics.all_option_regex().match("none")
    assert not heuristics.all_option_regex().match("Al Jazeera 2")
    assert heuristics.is_volatile_param("utm_source") and heuristics.is_volatile_param("sid") and not heuristics.is_volatile_param("country")
    assert not heuristics.is_volatile_param("sidebar")            # "=sid" means the exact name only


def test_a_trailing_star_matches_any_ending_and_words_match_as_whole_words():
    assert heuristics.has_word("content_words", "these results are shown") and heuristics.has_word("content_words", "resulting page")
    assert not heuristics.has_word("content_words", "the seesaw")   # "see" must not match inside "seesaw"


# ------------------------------------------------------------------ a site can adapt without code changes

def test_a_site_can_add_words_in_its_own_language(tmp_path):
    heuristics.configure(_file(tmp_path, {"content_words": ["résultats", "liste"], "pick_verbs": ["choisit"]}))
    assert heuristics.has_word("content_words", "Le visiteur voit les résultats de la recherche")
    assert heuristics.has_word("content_words", "shows a list")                    # the defaults are still there
    assert heuristics.word_regex("pick_verbs").search("Le visiteur choisit un pays")


def test_replace_swaps_a_list_instead_of_extending_it(tmp_path):
    heuristics.configure(_file(tmp_path, {"content_words": ["résultats"], "replace": ["content_words"]}))
    assert heuristics.has_word("content_words", "les résultats") and not heuristics.has_word("content_words", "shows the results")


def test_a_promised_content_check_follows_the_sites_own_words(tmp_path):
    flow = {"source": "intent", "goal": "Le visiteur voit les résultats de la recherche", "status": "candidate",
            "outcome": {"effect": "navigates"}}
    assert not sentence_expects_content(flow)                                      # unknown language: nothing is promised
    heuristics.configure(_file(tmp_path, {"content_words": ["résultats"]}))
    assert sentence_expects_content(flow)
    observed = {"effect": "navigates", "url": "https://x.test/fr/r", "new_headings": [], "new_controls": [], "results": []}
    result = {"ok": True, "steps_done": 1, "steps_total": 1, "error": None, "step_effects": ["navigates"], "observed": observed}
    assert apply_result(flow, result, "now")["passed"] is False


def test_the_unsuitable_rules_can_be_extended_for_a_site(tmp_path):
    sentence = "Le visiteur remplit la connexion puis voit son espace personnel."
    assert add_intent({"intents": []}, sentence, "ai", "t")
    heuristics.configure(_file(tmp_path, {"unsuitable": [{"pattern": "connexion", "reason": "needs credentials"}]}))
    with pytest.raises(IntentError, match="needs credentials"):
        add_intent({"intents": []}, sentence, "ai", "t")
    assert add_intent({"intents": []}, sentence, "human", "t")                     # a person is never held to it


def test_volatile_query_parameters_can_be_extended(tmp_path):
    url = "https://x.test/r?country=EG&trk=abc&page=2"
    assert len(_query_expects(url)) == 3
    heuristics.configure(_file(tmp_path, {"volatile_params": ["trk", "=page"]}))
    assert len(_query_expects(url)) == 1 and "country" in _query_expects(url)[0]


# ------------------------------------------------------------------ bad files never break the tool

def test_a_missing_or_broken_file_means_the_defaults(tmp_path):
    assert heuristics.configure(tmp_path / "nope.json") == []
    broken = tmp_path / "heuristics.json"
    broken.write_text("not json", encoding="utf-8")
    assert "ignored" in heuristics.configure(broken)[0]
    assert heuristics.has_word("content_words", "sees the results")
    broken.write_text("[1, 2]", encoding="utf-8")
    assert "JSON object" in heuristics.configure(broken)[0]


def test_unknown_keys_bad_types_and_bad_regexes_are_reported_and_skipped(tmp_path):
    notes = heuristics.configure(_file(tmp_path, {
        "made_up": ["x"], "content_words": "not a list", "unsuitable": [{"pattern": "([", "reason": "bad"}, {"pattern": "ok", "reason": "fine"}]}))
    text = " | ".join(notes)
    assert "unknown heuristics key 'made_up'" in text and "'content_words' must be a list" in text and "ignored" in text
    assert any(reason == "fine" for _, reason in heuristics.unsuitable_rules())
    assert heuristics.has_word("content_words", "the results")                     # the defaults survived the bad entry


def test_the_environment_variable_configures_the_specs_that_pytest_runs_separately(tmp_path, monkeypatch):
    path = _file(tmp_path, {"menu_selectors": ["[data-open-menu]"]})
    monkeypatch.setenv("WTP_HEURISTICS", str(path))
    heuristics._config = None                                                      # as in a fresh pytest process
    assert "[data-open-menu]:visible" in menu_selector()


# ------------------------------------------------------------------ pages: standard ARIA first, extendable

def test_the_default_menu_and_option_selectors_cover_standard_aria_widgets():
    menu, option = heuristics.menu_selector(), heuristics.option_selector()
    for part in ('[role="listbox"]:visible', '[role="menu"]:visible', ".ui-multiselect-menu:visible"):
        assert part in menu
    for part in ('[role="option"]', '[role="menuitemcheckbox"]', '[role="menuitem"]', "li label"):
        assert part in option


def test_loading_indicators_are_found_by_configurable_class_hints_and_aria(tmp_path):
    assert 'aria-busy' in _loader_js() and 'progressbar' in _loader_js() and "loading" in _loader_js()
    heuristics.configure(_file(tmp_path, {"loader_hints": ["chargement"]}))
    assert "chargement" in _loader_js()


class _Loc:
    def __init__(self, page, name, count=1):
        self.page, self.name, self._count, self.first = page, name, count, self

    def count(self): return self._count
    def locator(self, sel): return _Loc(self.page, "option:" + sel)
    def filter(self, has_text): return self
    def click(self, timeout=None): self.page.log.append(self.name)
    def get_attribute(self, name): return self.page.attrs.get(name)


class _AriaPage:
    def __init__(self, attrs):
        self.attrs, self.log, self.selectors = attrs, [], []

    def locator(self, selector):
        self.selectors.append(selector)
        return _Loc(self, "menu:" + selector, 1 if selector.startswith("[id=") or "visible" in selector else 0)

    def wait_for_timeout(self, ms): pass


def test_pick_option_uses_the_menu_the_button_points_to_with_aria_controls():
    page = _AriaPage({"aria-controls": "menu-7"})
    pick_option(page, _Loc(page, "trigger"), "Blue Widget")
    assert any(s == '[id="menu-7"]' for s in page.selectors)                       # the owned menu, not "the first open one"
    assert "option:" + heuristics.option_selector() in page.log


def test_pick_option_falls_back_to_the_first_open_menu_without_aria_links():
    page = _AriaPage({})
    pick_option(page, _Loc(page, "trigger"), "Blue Widget")
    assert not any(s.startswith("[id=") for s in page.selectors) and any("visible" in s for s in page.selectors)
