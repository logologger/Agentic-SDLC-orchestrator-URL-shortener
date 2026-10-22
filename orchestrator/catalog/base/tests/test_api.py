import pytest
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.ratelimit import TokenBucketLimiter


@pytest.fixture
def client():
    with TestClient(create_app(":memory:")) as c:
        yield c


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_create_redirect_stats_flow(client):
    r = client.post("/api/v1/urls", json={"url": "https://example.com/landing"})
    assert r.status_code == 201
    body = r.json()
    assert body["short_url"].endswith("/" + body["code"])

    hop = client.get("/" + body["code"], follow_redirects=False)
    assert hop.status_code == 302
    assert hop.headers["location"] == "https://example.com/landing"

    stats = client.get(f"/api/v1/urls/{body['code']}/stats").json()
    assert stats["total_clicks"] == 1


def test_error_mapping(client):
    assert client.post("/api/v1/urls", json={"url": "http://localhost/x"}).status_code == 422
    assert client.post("/api/v1/urls", json={}).status_code == 422
    client.post("/api/v1/urls", json={"url": "https://example.com", "alias": "taken"})
    assert client.post("/api/v1/urls", json={"url": "https://example.com", "alias": "taken"}).status_code == 409
    assert client.get("/missing1", follow_redirects=False).status_code == 404
    assert client.get("/api/v1/urls/missing1/stats").status_code == 404


def test_rate_limit_returns_429_with_retry_after():
    app = create_app(":memory:", limiter=TokenBucketLimiter(capacity=2, refill_per_sec=0.0))
    with TestClient(app) as c:
        codes = [c.post("/api/v1/urls", json={"url": "https://example.com"}).status_code for _ in range(3)]
        assert codes == [201, 201, 429]
        assert c.post("/api/v1/urls", json={"url": "https://example.com"}).headers["Retry-After"]
