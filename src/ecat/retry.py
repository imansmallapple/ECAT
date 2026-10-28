"""Exponential back-off retry decorator with jitter.

Usage::

    from ecat.retry import retry

    @retry()
    def call_llm():
        ...

    # Or with custom settings:
    @retry(max_attempts=5, base_delay=1.0)
    async def async_call():
        ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

from ecat.errors import RetryableTransient

F = TypeVar("F", bound=Callable[..., Any])

log = logging.getLogger(__name__)


def _should_retry(exc: BaseException) -> bool:
    return isinstance(exc, RetryableTransient)


def retry(
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retryable: Callable[[BaseException], bool] = _should_retry,
) -> Callable[[F], F]:
    """Return a decorator that retries *func* on retryable errors."""

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempt = 0
                while True:
                    try:
                        return await func(*args, **kwargs)
                    except Exception as exc:
                        attempt += 1
                        if attempt >= max_attempts or not retryable(exc):
                            raise
                        delay = _backoff(attempt, base_delay, max_delay, jitter)
                        log.warning(
                            "Retryable error on attempt %d/%d: %s. Sleeping %.1fs.",
                            attempt,
                            max_attempts,
                            exc,
                            delay,
                        )
                        await asyncio.sleep(delay)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    attempt += 1
                    if attempt >= max_attempts or not retryable(exc):
                        raise
                    delay = _backoff(attempt, base_delay, max_delay, jitter)
                    log.warning(
                        "Retryable error on attempt %d/%d: %s. Sleeping %.1fs.",
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _backoff(attempt: int, base: float, cap: float, jitter: bool) -> float:
    delay: float = min(base * (2 ** (attempt - 1)), cap)
    if jitter:
        delay *= 0.5 + random.random() * 0.5  # ∈ [delay/2, delay]
    return delay
