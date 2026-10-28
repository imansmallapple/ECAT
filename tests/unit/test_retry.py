"""Unit tests for ecat.retry — back-off mathematics."""

from unittest.mock import patch

import pytest

from ecat.errors import RetryableTransient
from ecat.retry import _backoff, retry


class _FlakyTransient(RetryableTransient):
    pass


def test_backoff_growth():
    d1 = _backoff(1, 2.0, 30.0, False)
    d2 = _backoff(2, 2.0, 30.0, False)
    d3 = _backoff(3, 2.0, 30.0, False)
    assert d1 < d2 < d3


def test_backoff_cap():
    assert _backoff(10, 2.0, 5.0, False) == 5.0


def test_backoff_jitter_range():
    for _ in range(50):
        d = _backoff(1, 2.0, 30.0, True)
        assert 1.0 <= d <= 2.0


def test_sync_retry_success_on_third():
    calls = []

    @retry(max_attempts=3, base_delay=0.0)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _FlakyTransient("oops")
        return "ok"

    with patch("time.sleep"):
        result = flaky()
    assert result == "ok"
    assert len(calls) == 3


def test_sync_retry_gives_up():
    @retry(max_attempts=2, base_delay=0.0)
    def always_fails():
        raise _FlakyTransient("always")

    with patch("time.sleep"), pytest.raises(_FlakyTransient):
        always_fails()


def test_non_retryable_not_retried():
    calls = []

    @retry(max_attempts=5, base_delay=0.0)
    def raise_value():
        calls.append(1)
        raise ValueError("not retryable")

    with patch("time.sleep"), pytest.raises(ValueError):
        raise_value()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_retry():
    calls = []

    @retry(max_attempts=3, base_delay=0.0)
    async def async_flaky():
        calls.append(1)
        if len(calls) < 2:
            raise _FlakyTransient("async oops")
        return "async_ok"

    with patch("asyncio.sleep"):
        result = await async_flaky()
    assert result == "async_ok"
    assert len(calls) == 2
