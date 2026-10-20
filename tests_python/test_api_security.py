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


def test_the_refusal_says_which_host_resolved_to_which_private_address_and_what_to_do():
    with pytest.raises(UrlRejected) as refused:
        validate_url("https://sat-stg.example.tv/ar", lambda host: ["172.27.25.2"])
    message = str(refused.value)
    assert "sat-stg.example.tv resolves to 172.27.25.2" in message and "ALLOW_PRIVATE_TARGETS" in message
    with pytest.raises(UrlRejected, match=r"localhost.*ALLOW_PRIVATE_TARGETS"):
        validate_url("http://localhost:8000/", PUBLIC)


def test_an_operator_can_allow_private_addresses_but_a_bad_scheme_is_still_refused():
    private = lambda host: ["172.27.25.2"]
    assert validate_url("https://sat-stg.example.tv/ar", private, allow_private=True) == "https://sat-stg.example.tv/ar"
    assert validate_url("http://localhost:8000", PUBLIC, allow_private=True) == "http://localhost:8000/"
    assert validate_url("http://169.254.169.254/latest", PUBLIC, allow_private=True).startswith("http://169.254.169.254")
    for bad in ("ftp://sat-stg.example.tv/", "file:///etc/passwd", "http://"):
        with pytest.raises(UrlRejected):
            validate_url(bad, private, allow_private=True)


def test_a_name_that_does_not_resolve_is_refused():
    def nowhere(host):
        raise UrlRejected("not found")
    with pytest.raises(UrlRejected):
        validate_url("https://nope.example/", nowhere)


def test_an_overlong_address_is_refused():
    with pytest.raises(UrlRejected):
        validate_url("https://example.com/" + "a" * 2100, PUBLIC)


def test_a_session_token_names_the_user_until_it_expires_and_never_after_tampering():
    token = security.make_user_token("k", "user1", 3, now=1000.0)
    assert security.read_user_token("k", token, now=1001.0) == ("user1", 3)
    assert security.read_user_token("other-secret", token, now=1001.0) is None                       # signed by someone else
    assert security.read_user_token("k", token, now=1000.0 + security.SESSION_TTL_S + 1) is None     # expired
    tag, user, version, expires, signature = token.split(".")
    for forged in (f"{tag}.user2.{version}.{expires}.{signature}", f"{tag}.{user}.9.{expires}.{signature}",
                   f"{tag}.{user}.{version}.{int(expires) + 99999}.{signature}"):
        assert security.read_user_token("k", forged, now=1001.0) is None
    assert security.read_user_token("k", None) is None and security.read_user_token("k", "garbage") is None


def test_the_failure_counter_looks_without_counting_and_only_blocks_at_the_limit():
    window = security.SlidingWindow(limit=2, window_s=10)
    assert not window.blocked("a", now=0)
    window.record("a", now=0)
    window.record("a", now=1)
    assert window.blocked("a", now=2) and not window.blocked("b", now=2)
    assert not window.blocked("a", now=12)                                                               # the window moved on


def test_the_sliding_window_allows_a_limit_per_key_per_window():
    window = security.SlidingWindow(limit=2, window_s=10)
    assert window.allow("a", now=0) and window.allow("a", now=1)
    assert not window.allow("a", now=2)
    assert window.allow("b", now=2)
    assert window.allow("a", now=12)
