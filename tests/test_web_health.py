"""Tests for web app health endpoint and rate limiter"""
import pytest
from web.app import create_app, InMemoryRateLimiter


@pytest.fixture
def app():
    config = {
        "app": {"secret_key": "test", "db_path": ":memory:"},
        "platforms": {},
    }
    app = create_app(config)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "status" in data
    assert "db" in data


def test_login_page_reachable(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_rate_limiter_allows_requests():
    limiter = InMemoryRateLimiter(max_requests=3, window_seconds=60)
    assert limiter.is_allowed("ip1") is True
    assert limiter.is_allowed("ip1") is True
    assert limiter.is_allowed("ip1") is True


def test_rate_limiter_blocks_excess():
    limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60)
    limiter.is_allowed("ip1")
    limiter.is_allowed("ip1")
    assert limiter.is_allowed("ip1") is False


def test_rate_limiter_per_ip():
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60)
    assert limiter.is_allowed("ip1") is True
    assert limiter.is_allowed("ip2") is True
    assert limiter.is_allowed("ip1") is False
    assert limiter.is_allowed("ip2") is False
