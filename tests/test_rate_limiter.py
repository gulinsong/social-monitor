"""Tests for core.rate_limiter"""
import pytest
from core.rate_limiter import RateLimiter, CircuitBreakerError


def test_defaults():
    rl = RateLimiter({})
    assert rl.min_delay == 3.0
    assert rl.max_delay == 8.0
    assert rl.max_per_hour == 60
    assert rl.consecutive_failures == 0


def test_custom_config():
    rl = RateLimiter({
        "request_delay": {"min": 5.0, "max": 15.0},
        "max_requests_per_hour": 30,
    })
    assert rl.min_delay == 5.0
    assert rl.max_delay == 15.0
    assert rl.max_per_hour == 30


def test_record_success_resets_failures():
    rl = RateLimiter({})
    rl.consecutive_failures = 3
    rl.record_success()
    assert rl.consecutive_failures == 0


def test_circuit_breaker_triggers():
    rl = RateLimiter({"max_failures": 3})
    # Override max_failures for faster test
    rl.max_failures = 3
    rl.record_failure()
    rl.record_failure()
    assert rl.consecutive_failures == 2
    with pytest.raises(CircuitBreakerError):
        rl.record_failure()
    assert rl.consecutive_failures == 3


def test_record_failure_increments():
    rl = RateLimiter({})
    rl.record_failure()
    rl.record_failure()
    assert rl.consecutive_failures == 2
