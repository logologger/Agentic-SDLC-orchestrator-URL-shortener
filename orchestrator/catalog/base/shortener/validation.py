"""Input validation for URLs and aliases. Pure functions, no I/O."""
import ipaddress
import re
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED_ALIASES = frozenset({"api", "healthz", "docs", "redoc", "openapi.json"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


class ValidationError(ValueError):
    """Raised when caller-supplied input is rejected."""


def _is_private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a DNS name; resolution-time checks are out of scope (see docs)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def validate_url(raw: str) -> str:
    """Return the normalised URL or raise ValidationError (scheme allow-list + SSRF guard)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("url must be a non-empty string")
    url = raw.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"url exceeds {MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise ValidationError("url is malformed") from None
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValidationError("only http and https urls are allowed")
    if not host:
        raise ValidationError("url must include a host")
    if _is_private_host(host):
        raise ValidationError("urls pointing at private or loopback hosts are not allowed")
    return url


def validate_alias(alias: str) -> str:
    if not isinstance(alias, str) or not ALIAS_RE.match(alias):
        raise ValidationError("alias must be 3-32 characters of letters, digits, '-' or '_'")
    if alias.lower() in RESERVED_ALIASES:
        raise ValidationError("alias is reserved")
    return alias
