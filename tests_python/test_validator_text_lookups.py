import pytest

from website_test_pipeline.validator import SpecError, validate_python_spec

URL = "https://x.test/en"
INV = {"controls": [{"name": "Passcode (default)", "tag": "fieldset"}], "headings": [{"text": "Satellite Frequencies"}], "revealed": []}


def _spec(locator: str) -> str:
    return ("from playwright.sync_api import expect\n"
            f"URL = {URL!r}\n"
            "def test_page(page):\n"
            f"    page.goto(URL)\n"
            f"    expect({locator}).to_be_visible()\n"
            "    observation_evidence(page, 'x')\n")


def test_an_exact_text_lookup_of_a_name_the_page_really_has_is_accepted():
    validate_python_spec(_spec("page.get_by_text('Passcode (default)', exact=True).first"), URL, INV)


def test_a_heading_the_page_really_has_is_accepted_too():
    validate_python_spec(_spec("page.get_by_text('Satellite Frequencies', exact=True).first"), URL, INV)


def test_a_text_lookup_of_a_name_the_page_never_had_is_still_rejected():
    with pytest.raises(SpecError, match="unstable text selector"):
        validate_python_spec(_spec("page.get_by_text('Something invented', exact=True).first"), URL, INV)


def test_a_fuzzy_text_lookup_without_exact_is_still_rejected_even_for_a_real_name():
    with pytest.raises(SpecError, match="unstable text selector"):
        validate_python_spec(_spec("page.get_by_text('Passcode (default)').first"), URL, INV)


def test_without_an_inventory_every_text_lookup_stays_rejected_and_has_text_always_is():
    with pytest.raises(SpecError, match="unstable text selector"):
        validate_python_spec(_spec("page.get_by_text('Passcode (default)', exact=True).first"), URL, None)
    with pytest.raises(SpecError, match="unstable text selector"):
        validate_python_spec(_spec("page.locator('button:has-text(\"Go\")')"), URL, INV)
