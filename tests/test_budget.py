"""Cost guard: the pre-flight reserve rejects when the pool can't cover a run, actual
usage decrements the ledger, and the mid-run hard check trips at the cap. Redis is
faked (injected) so these run without a real Redis or the `redis` package's server."""

from __future__ import annotations

import pytest

pytest.importorskip("redis")  # budget.py imports the redis client at module load

from yapper_web.budget import Budget, BudgetExceeded  # noqa: E402
from yapper_web.settings import Settings  # noqa: E402


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, int] = {}

    def get(self, k):
        return None if k not in self.kv else str(self.kv[k]).encode()

    def set(self, k, v):
        self.kv[k] = int(v)

    def incrby(self, k, n):
        self.kv[k] = self.kv.get(k, 0) + int(n)
        return self.kv[k]

    def decrby(self, k, n):
        self.kv[k] = self.kv.get(k, 0) - int(n)
        return self.kv[k]


class _Usage:
    def __init__(self, pin, pout):
        self.prompt_tokens = pin
        self.completion_tokens = pout


def _budget(cap=3.0, est_in=150_000, est_out=32_000):
    s = Settings(
        llm_max_cost_usd=cap, llm_price_in_per_1k=0.0003, llm_price_out_per_1k=0.0011,
        llm_est_in_tokens=est_in, llm_est_out_tokens=est_out,
    )
    return Budget(client=_FakeRedis(), settings=s)


def test_reserve_holds_then_release_frees():
    b = _budget()
    est = b.s.run_cost_estimate_usd()
    held = b.reserve()
    assert held == pytest.approx(est)
    # ledger is integer milli-USD, so reserved reads back rounded to the nearest milli
    assert b.reserved_usd() == pytest.approx(est, abs=1e-3)
    assert b.remaining_usd() == pytest.approx(b.s.llm_max_cost_usd - est, abs=1e-3)
    b.release(held)
    assert b.reserved_usd() == 0
    assert b.remaining_usd() == pytest.approx(b.s.llm_max_cost_usd)


def test_reserve_rejects_when_insufficient():
    # Tiny cap so even one worst-case run can't be afforded.
    b = _budget(cap=0.001)
    with pytest.raises(BudgetExceeded):
        b.reserve()
    # nothing should remain reserved after a failed reserve
    assert b.reserved_usd() == 0


def test_record_usage_decrements_pool_and_hard_cap_trips():
    b = _budget(cap=0.10)
    cost = b.record_usage(_Usage(100_000, 20_000))  # 0.03 + 0.022 = 0.052
    assert cost == pytest.approx(0.052, abs=1e-6)
    assert b.spent_usd() == pytest.approx(0.052, abs=1e-6)
    b.check_can_spend()  # still under 0.10
    b.record_usage(_Usage(100_000, 20_000))  # now 0.104 > cap
    with pytest.raises(BudgetExceeded):
        b.check_can_spend()


def test_budget_guard_accumulates_for_run_accounting():
    from yapper_web.budget import BudgetGuard

    b = _budget()
    g = BudgetGuard(b)
    g.post(_Usage(1000, 2000))
    g.post(_Usage(3000, 4000))
    assert g.tokens_in == 4000
    assert g.tokens_out == 6000
    assert g.cost_usd > 0
