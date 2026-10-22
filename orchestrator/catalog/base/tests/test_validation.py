import pytest

from shortener.validation import ValidationError, validate_alias, validate_url


@pytest.mark.parametrize("url", ["https://example.com/a?b=1", "http://example.org", " https://example.com "])
def test_accepts_public_http_urls(url):
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://example.com",
        "javascript:alert(1)",
        "http://",
        "http://localhost/admin",
        "http://127.0.0.1:8080",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://service.internal/",
        "https://example.com/" + "a" * 2100,
    ],
)
def test_rejects_unsafe_or_malformed_urls(url):
    with pytest.raises(ValidationError):
        validate_url(url)


@pytest.mark.parametrize("alias", ["my-link", "A_b-9", "abc"])
def test_accepts_valid_alias(alias):
    assert validate_alias(alias) == alias


@pytest.mark.parametrize("alias", ["ab", "has space", "x" * 33, "api", "HEALTHZ", "semi;colon"])
def test_rejects_invalid_alias(alias):
    with pytest.raises(ValidationError):
        validate_alias(alias)
