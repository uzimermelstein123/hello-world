r"""Core MILP hedge optimizer (HiGHS via ``scipy.optimize.milp``).

Problem
-------
    min   sum_i c_i x_i                                     (hedge procurement cost)
    s.t.  sum_i Payout_i(s) x_i >= Liability(s) + eps       for every scenario s with Liability(s) > 0
          0 <= x_i <= L_i,  x_i integer

    EffSurcharge(s) = CarrierTable(s) * (1 - carrier_fuel_discount)
    Liability(s)    = V * max(0, EffSurcharge(s) - promised_cap)     (summed over the service mix)

Fee charged to the shipper: P_$ = K_hedge + min_profit_dollars, so in every scenario
    Net P&L(s) = P_$ - K_hedge + Payout(s) - Liability(s) >= min_profit_dollars.

Continuous guarantee
--------------------
Liability is a right-continuous step function of diesel (carrier bands are [lo, hi));
binary payouts are step functions; call payouts are continuous and non-decreasing.
Between consecutive breakpoints every term is constant except calls (increasing), so
Payout - Liability attains its minimum on each interval at the left breakpoint. Adding
all carrier-band edges and instrument strikes (mapped to diesel) to the $0.05 grid
therefore makes the guarantee hold for *every* diesel price in [min, max].

Feasibility
-----------
All payouts are non-negative, so buying every available contract (x = L) maximizes the
payout in *every* scenario simultaneously. The MILP is feasible iff
``Payout @ L >= Liability`` row-wise - which gives an exact, solver-independent
infeasibility diagnosis.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .carrier_grid import CarrierFuelTable
from .config import EngineSettings
from .schemas import (
    AuditRow,
    ExhaustedInstrument,
    HedgeResult,
    InfeasibilityReport,
    Instrument,
    InstrumentType,
    OrderLine,
    ScenarioShortfall,
    ShipperProfile,
    SolverStats,
    Venue,
    round_price,
)

log = logging.getLogger(__name__)


class HedgeSolverError(RuntimeError):
    """Solver failed for a reason other than infeasibility."""


class InfeasibleHedgeError(RuntimeError):
    """Not enough market depth to guarantee zero loss in every scenario."""

    def __init__(self, report: InfeasibilityReport) -> None:
        super().__init__(report.summary)
        self.report = report


@dataclass
class Scenarios:
    prices: np.ndarray
    on_grid: np.ndarray  # bool mask: True for the user's $0.05 grid points


@dataclass
class Exposure:
    nominal: np.ndarray  # blended (spend-weighted) carrier surcharge
    effective: np.ndarray  # blended after discount
    charged: np.ndarray  # blended surcharge the shipper actually pays under the cap
    liability: np.ndarray  # $ owed to the shipper


def build_scenarios(
    settings: EngineSettings,
    tables: Mapping[str, CarrierFuelTable],
    instruments: list[Instrument],
    services: list[str],
) -> Scenarios:
    lo, hi, step = settings.diesel_min, settings.diesel_max, settings.diesel_step
    if hi <= lo:
        raise ValueError("diesel_max must exceed diesel_min")
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    grid = round_price(lo + step * np.arange(n))
    if grid[-1] < hi - 1e-9:
        grid = np.append(grid, round_price(hi))
    extra: list[float] = []
    if settings.continuous_guarantee:
        for svc in services:
            extra.extend(tables[svc].breakpoints(lo, hi).tolist())
        for inst in instruments:
            extra.extend(p for p in inst.carrier_breakpoints() if lo <= p <= hi)
    prices = np.unique(np.concatenate([grid, round_price(np.array(extra, dtype=float))]))
    on_grid = np.isin(prices, grid)
    return Scenarios(prices=prices, on_grid=on_grid)


def compute_exposure(
    profile: ShipperProfile, tables: Mapping[str, CarrierFuelTable], prices: np.ndarray
) -> Exposure:
    nominal = np.zeros_like(prices, dtype=float)
    effective = np.zeros_like(prices, dtype=float)
    charged = np.zeros_like(prices, dtype=float)
    liability = np.zeros_like(prices, dtype=float)
    for svc, w in profile.service_mix.items():
        if w == 0:
            continue
        if svc not in tables:
            raise KeyError(f"no carrier table loaded for service '{svc}'")
        nom = tables[svc].surcharge_at(prices)
        eff = np.round(nom * (1.0 - profile.carrier_fuel_discount), 10)
        cap = profile.cap_for(svc)
        excess = np.maximum(0.0, np.round(eff - cap, 10))
        nominal += w * nom
        effective += w * eff
        charged += w * np.minimum(eff, cap)
        liability += profile.monthly_spend * w * excess
    return Exposure(nominal, effective, charged, np.round(liability, 2))


def payout_matrix(instruments: list[Instrument], prices: np.ndarray) -> np.ndarray:
    """S x N matrix of $ payout per contract."""
    if not instruments:
        return np.zeros((len(prices), 0))
    return np.column_stack([inst.payout(prices) for inst in instruments])


def order_fee(inst: Instrument, qty: int) -> float:
    """Actual fee for a fill. Kalshi rounds each fill's fee *up* to the next cent."""
    raw = inst.fee_per_contract * qty
    if inst.venue is Venue.KALSHI:
        return math.ceil(raw * 100 - 1e-9) / 100
    return round(raw, 2)


class HedgeOptimizer:
    def __init__(self, tables: Mapping[str, CarrierFuelTable], settings: EngineSettings) -> None:
        self.tables = dict(tables)
        self.s = settings

    # ------------------------------------------------------------------ public ----
    def solve(self, profile: ShipperProfile, instruments: list[Instrument]) -> HedgeResult:
        warnings: list[str] = []
        insts = [i for i in instruments if i.depth > 0 and i.unit_cost > 0]
        dropped = len(instruments) - len(insts)
        if dropped:
            log.debug("dropped %d instruments with zero depth or zero cost", dropped)

        services = [s for s, w in profile.service_mix.items() if w > 0]
        sc = build_scenarios(self.s, self.tables, insts, services)
        exp = compute_exposure(profile, self.tables, sc.prices)
        A = payout_matrix(insts, sc.prices)
        L = np.array([i.depth for i in insts], dtype=float)
        c = np.array([i.unit_cost for i in insts], dtype=float)
        need = exp.liability > 0
        target = np.where(need, exp.liability + self.s.solver_safety_margin, 0.0)

        if exp.liability[0] > 0:
            warnings.append(
                f"Cap already binds at the bottom of the scenario range "
                f"(${sc.prices[0]:.2f}): liability below that price is not verified."
            )

        log.info(
            "Scenarios: %d (%d grid + %d breakpoints), %d with liability; instruments: %d",
            len(sc.prices), int(sc.on_grid.sum()), int((~sc.on_grid).sum()), int(need.sum()), len(insts),
        )

        if not need.any():
            x = np.zeros(len(insts), dtype=int)
            stats = None
            warnings.append("Effective surcharge never exceeds the cap in range: no hedge required.")
        else:
            self._check_feasible(profile, insts, sc, exp, A, L, target)
            x, stats = self._run_milp(c, A[need], target[need], L)
            if stats.status == 1:
                warnings.append(
                    "Solver hit its time limit: the hedge is valid (audited below) but may not be cost-optimal."
                )

        return self._assemble(profile, insts, sc, exp, A, x, stats, warnings)

    def max_coverage_gap(self, profile: ShipperProfile, instruments: list[Instrument]) -> float:
        """Largest shortfall (liability - payout) if every available contract were bought.

        <= 0 means a zero-loss hedge exists. No MILP needed (payouts are non-negative).
        """
        insts = [i for i in instruments if i.depth > 0 and i.unit_cost > 0]
        services = [s for s, w in profile.service_mix.items() if w > 0]
        sc = build_scenarios(self.s, self.tables, insts, services)
        liab = compute_exposure(profile, self.tables, sc.prices).liability
        A = payout_matrix(insts, sc.prices)
        L = np.array([i.depth for i in insts], dtype=float)
        max_pay = A @ L if A.size else np.zeros_like(liab)
        target = np.where(liab > 0, liab + self.s.solver_safety_margin, 0.0)
        return float(np.max(target - max_pay))

    def is_feasible(self, profile: ShipperProfile, instruments: list[Instrument]) -> bool:
        return self.max_coverage_gap(profile, instruments) <= 1e-9

    # ---------------------------------------------------------------- internals ---
    def _run_milp(
        self, c: np.ndarray, A: np.ndarray, lb: np.ndarray, L: np.ndarray
    ) -> tuple[np.ndarray, SolverStats]:
        t0 = time.perf_counter()
        res = milp(
            c,
            constraints=[LinearConstraint(A, lb=lb, ub=np.inf)],
            integrality=np.ones_like(c),
            bounds=Bounds(np.zeros_like(L), L),
            options={
                "disp": False,
                "time_limit": self.s.solver_time_limit,
                "mip_rel_gap": self.s.solver_mip_rel_gap,
            },
        )
        dt = time.perf_counter() - t0
        stats = SolverStats(
            status=res.status, message=str(res.message), runtime_sec=round(dt, 4),
            mip_node_count=getattr(res, "mip_node_count", None),
            mip_gap=getattr(res, "mip_gap", None),
            n_variables=len(c), n_constraints=A.shape[0],
        )
        log.info("HiGHS status=%s (%s) in %.3fs", res.status, res.message, dt)
        if res.x is None or res.status not in (0, 1):
            raise HedgeSolverError(f"HiGHS failed: status={res.status} {res.message}")
        x = np.round(res.x).astype(int)
        x = np.clip(x, 0, L.astype(int))
        # Integer rounding must not break coverage - verify against exact constraints.
        short = lb - A @ x
        if np.any(short > 1e-6):
            raise HedgeSolverError(
                f"rounded MILP solution violates coverage by up to ${short.max():.4f}"
            )
        return x, stats

    def _check_feasible(
        self, profile: ShipperProfile, insts: list[Instrument], sc: Scenarios, exp: Exposure,
        A: np.ndarray, L: np.ndarray, target: np.ndarray,
    ) -> None:
        max_payout = A @ L if A.size else np.zeros_like(target)
        gap = target - max_payout
        bad = (exp.liability > 0) & (gap > 1e-9)
        if not bad.any():
            return

        shortfalls = [
            ScenarioShortfall(diesel=float(p), liability=float(l), max_hedge_payout=float(m),
                              shortfall=float(g))
            for p, l, m, g in zip(sc.prices[bad], exp.liability[bad], max_payout[bad], gap[bad])
        ]
        worst_idx = int(np.flatnonzero(bad)[np.argmax(gap[bad])])
        worst_price = float(sc.prices[worst_idx])
        worst_gap = float(gap[worst_idx])
        uncovered = [float(p) for p in sc.prices[bad & (max_payout <= 0)]]

        exhausted: list[ExhaustedInstrument] = []
        if A.size:
            by_market: dict[str, list[int]] = defaultdict(list)
            for j in np.flatnonzero(A[worst_idx] > 0):
                by_market[insts[j].market_id].append(int(j))
            for mid, cols in by_market.items():
                per = float(A[worst_idx, cols[0]])
                exhausted.append(ExhaustedInstrument(
                    market_id=mid, venue=insts[cols[0]].venue,
                    contract=insts[cols[0]].describe(), levels=len(cols),
                    depth=int(sum(insts[j].depth for j in cols)), payout_at_worst=per,
                    additional_needed=int(math.ceil(worst_gap / per)),
                ))
            exhausted.sort(key=lambda e: (e.venue.value, e.market_id))
        binary_bps = [
            bp for i in insts if i.type is not InstrumentType.CALL for bp in i.carrier_breakpoints()
        ]
        has_calls = any(i.type is InstrumentType.CALL for i in insts)
        top_bp = max(binary_bps) if binary_bps and not has_calls else None

        first, last = shortfalls[0].diesel, shortfalls[-1].diesel
        lines = [
            "INFEASIBLE: insufficient market depth to guarantee zero loss.",
            f"  {len(shortfalls)} scenario(s) between ${first:.3f} and ${last:.3f} cannot be covered "
            f"even if every listed contract is bought (x_i = L_i).",
            f"  Worst gap: ${worst_gap:,.2f} at diesel ${worst_price:.3f} "
            f"(liability ${exp.liability[worst_idx]:,.2f} vs max hedge payout ${max_payout[worst_idx]:,.2f}).",
        ]
        if uncovered:
            lines.append(
                f"  No instrument pays anything at {len(uncovered)} scenario(s) starting ${uncovered[0]:.3f}: "
                "strikes are listed too high (or basis buffer too wide) to reach the cap trigger."
            )
        if exhausted:
            lines.append(
                f"  {len(exhausted)} market(s) paying at ${worst_price:.3f} have their entire visible book exhausted."
            )
        if top_bp is not None and top_bp < last:
            lines.append(
                f"  Only binaries are available and the highest strike pays from ${top_bp:.3f}: above that, "
                "payout is flat while liability keeps rising. Add call options or higher strikes."
            )
        raise InfeasibleHedgeError(InfeasibilityReport(
            profile=profile, shortfalls=shortfalls, exhausted=exhausted,
            uncovered_prices=uncovered, highest_paying_breakpoint=top_bp, summary="\n".join(lines),
        ))

    def _assemble(
        self, profile: ShipperProfile, insts: list[Instrument], sc: Scenarios, exp: Exposure,
        A: np.ndarray, x: np.ndarray, stats: SolverStats | None, warnings: list[str],
    ) -> HedgeResult:
        # ---- order ticket (aggregate book levels per market) -------------------------
        groups: dict[str, list[tuple[Instrument, int]]] = defaultdict(list)
        for inst, q in zip(insts, x):
            if q > 0:
                groups[inst.market_id].append((inst, int(q)))
        depth_by_market: dict[str, int] = defaultdict(int)
        for inst in insts:
            depth_by_market[inst.market_id] += inst.depth

        orders: list[OrderLine] = []
        for mid, fills in groups.items():
            first = fills[0][0]
            qty = sum(q for _, q in fills)
            premium = round(sum(i.premium_per_contract * q for i, q in fills), 2)
            fees = round(sum(order_fee(i, q) for i, q in fills), 2)
            orders.append(OrderLine(
                market_id=mid, venue=first.venue, type=first.type, contract=first.describe(),
                strike=first.strike, quantity=qty,
                limit_price=max(i.quote for i, _ in fills),
                vwap=sum(i.quote * q for i, q in fills) / qty,
                premium=premium, fees=fees, total_cost=round(premium + fees, 2),
                depth_available=depth_by_market[mid], levels_used=len(fills),
            ))
        orders.sort(key=lambda o: (o.venue.value, o.strike))

        hedge_premium = round(sum(o.premium for o in orders), 2)
        hedge_fees = round(sum(o.fees for o in orders), 2)
        hedge_cost = round(hedge_premium + hedge_fees, 2)
        lp_obj = float(sum(i.unit_cost * q for i, q in zip(insts, x)))
        fee_dollars = math.ceil((hedge_cost + profile.min_profit_dollars) * 100 - 1e-9) / 100
        fee_pct = fee_dollars / profile.monthly_spend * 100

        # ---- audit ---------------------------------------------------------------
        payout = A @ x if A.size else np.zeros_like(sc.prices)
        net = fee_dollars - hedge_cost + payout - exp.liability
        passed = net >= profile.min_profit_dollars - 1e-9
        audit = [
            AuditRow(diesel=float(p), on_grid=bool(g), carrier_surcharge=float(n),
                     effective_surcharge=float(e), charged_surcharge=float(ch),
                     liability=float(l), hedge_payout=round(float(po), 2),
                     net_pnl=round(float(nt), 2), passed=bool(ok))
            for p, g, n, e, ch, l, po, nt, ok in zip(
                sc.prices, sc.on_grid, exp.nominal, exp.effective, exp.charged,
                exp.liability, payout, net, passed)
        ]
        worst = int(np.argmin(net))

        trigger, ceiling = self._tail_metrics(profile, insts, x)
        if ceiling is not None and ceiling <= self.s.diesel_max:
            warnings.append(f"Hedge breaks at ${ceiling:.3f}, inside the scenario range - audit will fail.")
        elif ceiling is not None:
            warnings.append(
                f"Tail risk: at diesel >= ${ceiling:.2f} the hedge no longer covers the liability "
                f"(guaranteed range ends at ${self.s.diesel_max:.2f})."
            )

        return HedgeResult(
            profile=profile, orders=orders,
            quantities={i.instrument_id: int(q) for i, q in zip(insts, x) if q > 0},
            hedge_premium=hedge_premium, hedge_fees=hedge_fees, hedge_cost=hedge_cost,
            lp_objective=round(lp_obj, 4), fee_dollars=fee_dollars, fee_pct=fee_pct,
            audit=audit, all_passed=bool(passed.all()), min_net_pnl=float(net[worst]),
            worst_scenario=float(sc.prices[worst]), trigger_price=trigger,
            coverage_ceiling=ceiling, tail_scan_max=self.s.tail_scan_max,
            warnings=warnings, solver=stats,
        )

    def _tail_metrics(
        self, profile: ShipperProfile, insts: list[Instrument], x: np.ndarray
    ) -> tuple[float | None, float | None]:
        """(cap trigger price, lowest diesel price at which payout < liability, if any)."""
        lo, top = self.s.diesel_min, self.s.tail_scan_max
        pts = [round_price(np.arange(lo, top + 1e-9, 0.01))]
        for svc, w in profile.service_mix.items():
            if w > 0:
                pts.append(self.tables[svc].breakpoints(lo, top))
        for inst, q in zip(insts, x):
            if q > 0:
                pts.append(np.array([p for p in inst.carrier_breakpoints() if lo <= p <= top]))
        prices = np.unique(np.concatenate(pts))
        liab = compute_exposure(profile, self.tables, prices).liability
        held = [(i, q) for i, q in zip(insts, x) if q > 0]
        pay = sum((i.payout(prices) * q for i, q in held), np.zeros_like(prices))
        trig = prices[liab > 0]
        trigger = float(trig[0]) if trig.size else None
        fail = np.flatnonzero((liab > 0) & (pay < liab))
        if not fail.size:
            return trigger, None
        return trigger, float(prices[int(fail[0])])
