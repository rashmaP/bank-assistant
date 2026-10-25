"""Retry-with-backoff and a circuit breaker for calls to downstream services (pure Python)."""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, TypeVar

from .bank_client import BankError

T = TypeVar("T")


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; lets one trial call through after `cooldown` seconds."""

    def __init__(self, threshold: int = 5, cooldown: float = 15.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown, self._clock = threshold, cooldown, clock
        self._fails, self._opened_at, self._lock = 0, None, threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self.cooldown:
                self._opened_at = self._clock()  # half-open: admit a trial, re-arm the timer
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self._fails, self._opened_at = 0, None

    def failure(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= self.threshold:
                self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    breaker: CircuitBreaker | None = None,
    on_retry: Callable[[int, BankError], None] | None = None,
) -> T:
    """Retry only errors flagged retryable (timeouts, 5xx). 4xx = the bank answered, so never retried.

    Safe for writes here because the cheque-book call carries an idempotency key.
    """
    for i in range(attempts):
        if breaker and not breaker.allow():
            raise BankError("BANK_UNAVAILABLE", "The banking service is temporarily unavailable.", retryable=True)
        try:
            result = fn()
        except BankError as e:
            if not e.retryable:
                if breaker:
                    breaker.success()  # a 4xx proves the service is up
                raise
            if breaker:
                breaker.failure()
            if i == attempts - 1:
                raise
            if on_retry:
                on_retry(i + 1, e)
            sleep(base_delay * (2 ** i) * (0.5 + jitter()))  # exponential backoff with jitter
        else:
            if breaker:
                breaker.success()
            return result
    raise AssertionError("unreachable")
