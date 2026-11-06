"""Pricing analytics on top of the optimizer: cap menus, budget solving, fair value.

* :func:`cap_sweep` - fee (risk-free price) for each candidate cap -> the pricing menu.
* :func:`lowest_cap_for_budget` - the tightest cap a shipper can get for a max fee %.
* :class:`LognormalDieselModel` - actuarial expected payout to the shipper, so you can
  see how much of the risk-free fee is "fair value" vs. hedging friction + margin.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable

import numpy as np
from pydantic import BaseModel, Field
from scipy.special import ndtri

from .optimizer import HedgeOptimizer, HedgeSolverError, InfeasibleHedgeError, compute_exposure
from .schemas import Instrument, ShipperProfile

log = logging.getLogger(__name__)


class LognormalDieselModel(BaseModel):
    forward: float = Field(gt=0)  # expected EIA diesel at settlement
    annual_vol: float = Field(gt=0)
    horizon_days: int = Field(gt=0)
    n_quantiles: int = 20_000

    def sample(self) -> np.ndarray:
        T = self.horizon_days / 365.0
        sd = self.annual_vol * math.sqrt(T)
        u = (np.arange(self.n_quantiles) + 0.5) / self.n_quantiles
        return self.forward * np.exp(-0.5 * sd**2 + sd * ndtri(u))

    def expected_liability(self, optimizer: HedgeOptimizer, profile: ShipperProfile) -> float:
        prices = self.sample()
        return float(compute_exposure(profile, optimizer.tables, prices).liability.mean())

    def prob_cap_binds(self, optimizer: HedgeOptimizer, profile: ShipperProfile) -> float:
        prices = self.sample()
        return float((compute_exposure(profile, optimizer.tables, prices).liability > 0).mean())


class CapQuote(BaseModel):
    cap: float
    feasible: bool
    hedge_cost: float | None = None
    fee_dollars: float | None = None
    fee_pct: float | None = None
    all_in_max_pct: float | None = None  # cap + fee% : shipper's worst-case fuel cost
    expected_claims: float | None = None
    prob_binds: float | None = None
    reason: str = ""


def _with_cap(profile: ShipperProfile, cap: float) -> ShipperProfile:
    return profile.model_copy(update={"promised_cap": cap, "service_caps": {}})


def quote_cap(
    optimizer: HedgeOptimizer,
    profile: ShipperProfile,
    instruments: list[Instrument],
    cap: float,
    fair_value: LognormalDieselModel | None = None,
) -> CapQuote:
    p = _with_cap(profile, cap)
    ev = fair_value.expected_liability(optimizer, p) if fair_value else None
    pb = fair_value.prob_cap_binds(optimizer, p) if fair_value else None
    try:
        r = optimizer.solve(p, instruments)
    except InfeasibleHedgeError as exc:
        return CapQuote(cap=cap, feasible=False, expected_claims=ev, prob_binds=pb,
                        reason=exc.report.summary.splitlines()[0])
    except HedgeSolverError as exc:
        return CapQuote(cap=cap, feasible=False, expected_claims=ev, prob_binds=pb, reason=str(exc))
    return CapQuote(
        cap=cap, feasible=True, hedge_cost=r.hedge_cost, fee_dollars=r.fee_dollars,
        fee_pct=r.fee_pct, all_in_max_pct=cap * 100 + r.fee_pct,
        expected_claims=ev, prob_binds=pb,
    )


def cap_sweep(
    optimizer: HedgeOptimizer,
    profile: ShipperProfile,
    instruments: list[Instrument],
    caps: Iterable[float],
    fair_value: LognormalDieselModel | None = None,
) -> list[CapQuote]:
    return [quote_cap(optimizer, profile, instruments, round(c, 6), fair_value) for c in caps]


def cap_grid(lo: float, hi: float, step: float) -> list[float]:
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    return [round(lo + k * step, 6) for k in range(n)]


def _first_true(caps: list[float], pred) -> int | None:
    """Binary search for the first index where a monotone (F..F,T..T) predicate holds."""
    lo, hi, ans = 0, len(caps) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if pred(caps[mid]):
            ans, hi = mid, mid - 1
        else:
            lo = mid + 1
    return ans


def lowest_cap_for_budget(
    optimizer: HedgeOptimizer,
    profile: ShipperProfile,
    instruments: list[Instrument],
    max_fee_pct: float,
    caps: Iterable[float],
) -> CapQuote | None:
    """Tightest (lowest) cap whose risk-free fee is <= ``max_fee_pct`` percent of spend.

    Raising the cap lowers liability in every scenario, which enlarges the feasible set,
    so the minimum hedge cost (and fee) is non-increasing in the cap. That makes
    "fee <= budget" monotone in the cap and binary search exact on the grid.
    """
    grid = sorted(caps)
    cache: dict[float, CapQuote] = {}

    def ok(cap: float) -> bool:
        if not optimizer.is_feasible(_with_cap(profile, cap), instruments):
            return False
        cache[cap] = quote_cap(optimizer, profile, instruments, cap)
        q = cache[cap]
        return q.feasible and q.fee_pct is not None and q.fee_pct <= max_fee_pct + 1e-12

    idx = _first_true(grid, ok)
    return None if idx is None else cache[grid[idx]]


def lowest_feasible_cap(
    optimizer: HedgeOptimizer,
    profile: ShipperProfile,
    instruments: list[Instrument],
    caps: Iterable[float],
) -> CapQuote | None:
    """Lowest cap that available liquidity can guarantee (feasibility is monotone in the cap)."""
    grid = sorted(caps)
    idx = _first_true(grid, lambda c: optimizer.is_feasible(_with_cap(profile, c), instruments))
    return None if idx is None else quote_cap(optimizer, profile, instruments, grid[idx])
