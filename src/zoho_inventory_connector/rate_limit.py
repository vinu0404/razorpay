"""Client-side rate limiting for Zoho's three limits.

1. Per-minute: sliding 60s window (Zoho: 100 req/min per org).
2. Cooldown: when any call receives a 429, every call waits, not only the one
   that got it — otherwise the other in-flight calls keep hammering Zoho.
   Zoho's penalty for exceeding the per-minute limit is an org-wide block with
   Retry-After: 1800 (observed). Cooldowns longer than `max_wait` fail fast
   instead of leaving a tool call hanging for half an hour.
3. Daily: rolling 24h budget persisted to disk (Zoho: 1000/day on the free
   plan). Zoho does not document when its daily counter resets, so a rolling
   window is the conservative choice: it can never exceed the limit.

Concurrency (Zoho: 5 on the free plan) is a semaphore in the client.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from .errors import DailyBudgetExhaustedError, RateLimitedError
from .logging_config import get_logger

logger = get_logger(__name__)

DAY_SECONDS = 24 * 60 * 60


class SlidingWindowLimiter:
    def __init__(
        self,
        max_calls: int,
        period: float = 60.0,
        max_wait: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.max_calls = max_calls
        self.period = period
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        self._calls: deque[float] = deque()
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    def cool_down(self, seconds: float) -> None:
        until = self.clock() + seconds
        if until > self._cooldown_until:
            self._cooldown_until = until
            logger.warning("rate limit cooldown started", extra={"seconds": round(seconds, 1)})

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self.clock()
                if now < self._cooldown_until:
                    remaining = self._cooldown_until - now
                    if remaining > self.max_wait:
                        raise RateLimitedError(
                            "Zoho has temporarily blocked API calls for this organization",
                            retry_after=round(remaining),
                        )
                    await self.sleep(remaining)
                    continue
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0])
                logger.info("per-minute limit reached; waiting", extra={"wait_seconds": round(wait, 2)})
                await self.sleep(wait)

    @property
    def cooldown_remaining(self) -> float:
        return max(self._cooldown_until - self.clock(), 0.0)

    @property
    def calls_in_window(self) -> int:
        now = self.clock()
        return sum(1 for t in self._calls if now - t < self.period)


class DailyBudget:
    """Rolling 24h request counter persisted as a list of timestamps."""

    def __init__(self, limit: int, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.limit = limit
        self.path = path
        self.clock = clock
        self._stamps: deque[float] = deque(self._load())

    def consume(self) -> None:
        self._prune()
        if len(self._stamps) >= self.limit:
            resets_in = int(self._stamps[0] + DAY_SECONDS - self.clock()) + 1
            raise DailyBudgetExhaustedError(self.limit, resets_in)
        self._stamps.append(self.clock())
        self._save()

    @property
    def used(self) -> int:
        self._prune()
        return len(self._stamps)

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def _prune(self) -> None:
        cutoff = self.clock() - DAY_SECONDS
        while self._stamps and self._stamps[0] <= cutoff:
            self._stamps.popleft()

    def _load(self) -> list[float]:
        try:
            return [float(t) for t in json.loads(self.path.read_text())]
        except (FileNotFoundError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._stamps)))
        os.replace(tmp, self.path)
