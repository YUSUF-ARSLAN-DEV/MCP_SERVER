import pytest

from api import security
from api.security import UrlRejected, validate_url

PUBLIC = lambda host: ["93.184.216.34"]


def test_a_public_address_is_cleaned_and_a_missing_scheme_means_https():
    assert validate_url("example.com", PUBLIC) == "https://example.com/"
    assert validate_url("HTTP://Example.com:8080/a?b=1#frag", PUBLIC) == "http://example.com:8080/a?b=1"


def test_credentials_in_the_url_are_dropped():
    assert validate_url("https://user:secret@example.com/x", PUBLIC) == "https://example.com/x"


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://LOCALHOST:8000", "http://127.0.0.1/", "http://127.1.2.3/", "http://10.0.0.5/",
    "http://172.16.0.1/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "http://[::1]/",
    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://printer.local/", "http://db.internal/",
])
def test_the_servers_own_network_and_the_cloud_metadata_address_are_refused(url):
    with pytest.raises(UrlRejected):
        validate_url(url, PUBLIC)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "", "   ", "http://", "http://example.com:99999/"])
def test_other_schemes_and_malformed_addresses_are_refused(url):
    with pytest.raises(UrlRejected):
        validate_url(url, PUBLIC)


def test_a_name_that_resolves_to_a_private_address_is_refused_even_when_another_address_is_public():
    with pytest.raises(UrlRejected):
        validate_url("https://rebind.example/", lambda host: ["93.184.216.34", "10.0.0.7"])


def test_a_name_that_does_not_resolve_is_refused():
    def nowhere(host):
        raise UrlRejected("not found")
    with pytest.raises(UrlRejected):
        validate_url("https://nope.example/", nowhere)


def test_an_overlong_address_is_refused():
    with pytest.raises(UrlRejected):
        validate_url("https://example.com/" + "a" * 2100, PUBLIC)


def test_a_session_token_verifies_until_it_expires_and_never_after_tampering():
    token = security.make_token("k", now=1000.0)
    assert security.verify_token("k", token, now=1001.0)
    assert not security.verify_token("other", token, now=1001.0)
    assert not security.verify_token("k", token, now=1000.0 + security.SESSION_TTL_S + 1)
    expires, signature = token.split(".")
    assert not security.verify_token("k", f"{int(expires) + 99999}.{signature}", now=1001.0)
    assert not security.verify_token("k", None) and not security.verify_token("k", "garbage")


def test_the_access_code_comparison_needs_a_configured_code():
    assert security.code_matches("abc", "abc")
    assert not security.code_matches("abc", "abd") and not security.code_matches("abc", None)
    assert not security.code_matches("", "")


def test_the_sliding_window_allows_a_limit_per_key_per_window():
    window = security.SlidingWindow(limit=2, window_s=10)
    assert window.allow("a", now=0) and window.allow("a", now=1)
    assert not window.allow("a", now=2)
    assert window.allow("b", now=2)
    assert window.allow("a", now=12)
