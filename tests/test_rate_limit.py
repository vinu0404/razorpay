import pytest

from zoho_inventory_connector.errors import DailyBudgetExhaustedError
from zoho_inventory_connector.rate_limit import DAY_SECONDS, DailyBudget, SlidingWindowLimiter


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.t += seconds


async def test_window_allows_up_to_limit_without_waiting():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(3, 60, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await limiter.acquire()
    assert clock.slept == []
    assert limiter.calls_in_window == 3


async def test_window_waits_for_oldest_call_to_expire():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, 60, clock=clock, sleep=clock.sleep)
    await limiter.acquire()
    clock.t += 10
    await limiter.acquire()
    await limiter.acquire()  # third call must wait until the first leaves the window
    assert clock.slept == [50.0]


async def test_cooldown_blocks_all_callers():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(100, 60, clock=clock, sleep=clock.sleep)
    limiter.cool_down(15)
    await limiter.acquire()
    assert clock.slept == [15.0]


async def test_shorter_cooldown_does_not_shrink_existing_one():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(100, 60, clock=clock, sleep=clock.sleep)
    limiter.cool_down(20)
    limiter.cool_down(5)
    await limiter.acquire()
    assert clock.slept == [20.0]


def test_daily_budget_exhausts_and_reports_reset(tmp_path):
    clock = FakeClock()
    budget = DailyBudget(2, tmp_path / "b.json", clock=clock)
    budget.consume()
    clock.t += 100
    budget.consume()
    with pytest.raises(DailyBudgetExhaustedError) as err:
        budget.consume()
    assert err.value.details["resets_in_seconds"] == DAY_SECONDS - 100 + 1


def test_daily_budget_is_rolling_and_persisted(tmp_path):
    clock = FakeClock()
    path = tmp_path / "b.json"
    DailyBudget(2, path, clock=clock).consume()
    reloaded = DailyBudget(2, path, clock=clock)
    assert reloaded.used == 1  # survives restart
    clock.t += DAY_SECONDS + 1
    assert reloaded.remaining == 2  # old call aged out


async def test_long_cooldown_fails_fast_instead_of_sleeping():
    from zoho_inventory_connector.errors import RateLimitedError

    clock = FakeClock()
    limiter = SlidingWindowLimiter(100, 60, max_wait=30, clock=clock, sleep=clock.sleep)
    limiter.cool_down(1800)
    with pytest.raises(RateLimitedError) as err:
        await limiter.acquire()
    assert clock.slept == []
    assert err.value.retry_after == 1800
