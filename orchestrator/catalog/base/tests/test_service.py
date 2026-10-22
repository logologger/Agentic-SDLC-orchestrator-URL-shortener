import itertools

import pytest

from shortener.ratelimit import TokenBucketLimiter
from shortener.repository import LinkRepository
from shortener.service import AliasTaken, CodeGenerationError, NotFound, ShortenerService


@pytest.fixture
def repo():
    r = LinkRepository(":memory:")
    yield r
    r.close()


def test_create_and_resolve_records_clicks(repo):
    clock = itertools.count(1000.0)
    svc = ShortenerService(repo, clock=lambda: next(clock))
    link = svc.create("https://example.com/page")
    assert len(link.code) == 7
    assert svc.resolve(link.code) == "https://example.com/page"
    svc.resolve(link.code)
    stats = svc.stats(link.code)
    assert stats.total_clicks == 2
    assert stats.last_clicked_at is not None


def test_custom_alias_conflict(repo):
    svc = ShortenerService(repo)
    svc.create("https://example.com/1", alias="promo")
    with pytest.raises(AliasTaken):
        svc.create("https://example.com/2", alias="promo")


def test_collision_retry_then_success(repo):
    codes = iter(["dup0000", "dup0000", "fresh01"])
    svc = ShortenerService(repo, code_factory=lambda: next(codes))
    svc.create("https://example.com/a")
    assert svc.create("https://example.com/b").code == "fresh01"


def test_collision_budget_exhausted(repo):
    svc = ShortenerService(repo, code_factory=lambda: "same000", max_attempts=3)
    svc.create("https://example.com/a")
    with pytest.raises(CodeGenerationError):
        svc.create("https://example.com/b")


def test_unknown_code(repo):
    svc = ShortenerService(repo)
    with pytest.raises(NotFound):
        svc.resolve("nope123")
    with pytest.raises(NotFound):
        svc.stats("nope123")


def test_rate_limiter_refills_and_isolates_keys():
    now = [0.0]
    lim = TokenBucketLimiter(capacity=2, refill_per_sec=1.0, clock=lambda: now[0])
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a")
    assert lim.allow("b")
    now[0] += 1.0
    assert lim.allow("a")
