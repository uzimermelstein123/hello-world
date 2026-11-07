"""JSON service layer for the web UI.

Turns a flat :class:`QuoteRequest` (what the form sends) into a self-contained JSON
payload with everything the UI shows. That covers the quote, the step-by-step
arithmetic, a $0.01 diesel curve broken down by position, the order ticket, the
market snapshot and the audit. The UI does no pricing math of its own; it only
displays these numbers.
"""

from __future__ import annotations

import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from . import config
from .analytics import LognormalDieselModel, cap_grid, lowest_feasible_cap, quote_cap
from .carrier_grid import CarrierFuelTable, default_tables
from .config import EngineSettings
from .market_feed import MarketSnapshot, load_market
from .optimizer import HedgeOptimizer, InfeasibleHedgeError, compute_exposure
from .schemas import HedgeResult, Instrument, InstrumentType, ShipperProfile, Venue, round_price


class QuoteRequest(BaseModel):
    """Flat form payload. Percent-style inputs are fractions (0.13 = 13%)."""

    name: str = config.DEFAULT_SHIPPER_NAME
    monthly_spend: float = Field(config.DEFAULT_MONTHLY_SPEND, gt=0)
    carrier_fuel_discount: float = Field(config.DEFAULT_FUEL_DISCOUNT, ge=0, lt=1)
    promised_cap: float = Field(config.DEFAULT_PROMISED_CAP, ge=0, le=1)
    min_profit_dollars: float = Field(config.DEFAULT_MIN_PROFIT, ge=0)
    air_share: float = Field(0.0, ge=0, le=1)
    air_cap: float | None = Field(None, ge=0, le=1)

    eia_diesel_spot: float = Field(3.75, gt=0)
    ho_futures_price: float = Field(2.45, gt=0)
    diesel_annual_vol: float = Field(0.35, gt=0)
    horizon_days: int = Field(30, gt=0, le=365)
    ho_basis_buffer: float = Field(0.05, ge=0)
    kalshi_basis_buffer: float = Field(0.01, ge=0)
    include_kalshi: bool = True
    include_cme: bool = True
    live_kalshi: bool = False

    def profile(self) -> ShipperProfile:
        mix = {"ground": 1.0 - self.air_share}
        caps: dict[str, float] = {}
        if self.air_share > 0:
            mix["air"] = self.air_share
            if self.air_cap is not None:
                caps["air"] = self.air_cap
        mix = {k: v for k, v in mix.items() if v > 0}
        return ShipperProfile(
            name=self.name, monthly_spend=self.monthly_spend,
            carrier_fuel_discount=self.carrier_fuel_discount, promised_cap=self.promised_cap,
            min_profit_dollars=self.min_profit_dollars, service_mix=mix, service_caps=caps,
        )

    def settings(self) -> EngineSettings:
        return EngineSettings.from_env(
            eia_diesel_spot=self.eia_diesel_spot, ho_futures_price=self.ho_futures_price,
            diesel_annual_vol=self.diesel_annual_vol, horizon_days=self.horizon_days,
            ho_basis_buffer=self.ho_basis_buffer, kalshi_basis_buffer=self.kalshi_basis_buffer,
        )


class _Context:
    """Everything one request needs, built once."""

    def __init__(self, req: QuoteRequest) -> None:
        self.req = req
        self.settings = req.settings()
        self.profile = req.profile()
        self.tables: dict[str, CarrierFuelTable] = default_tables(self.settings.air_jet_index_offset)
        self.snapshot: MarketSnapshot = load_market(
            self.settings, live_kalshi=req.live_kalshi,
            include_kalshi=req.include_kalshi, include_cme=req.include_cme,
        )
        self.optimizer = HedgeOptimizer(self.tables, self.settings)
        self.fair_value = LognormalDieselModel(
            forward=self.settings.eia_diesel_spot, annual_vol=self.settings.retail_annual_vol,
            horizon_days=self.settings.horizon_days,
        )


# ------------------------------------------------------------------------- helpers
def _r(x: float, nd: int = 2) -> float:
    return float(round(float(x), nd))


def _diesel_trigger(inst: Instrument) -> float:
    """Diesel price at which the instrument starts paying (after basis + buffer)."""
    return inst.carrier_breakpoints()[0]


def _market_rows(insts: list[Instrument]) -> list[dict[str, Any]]:
    groups: dict[str, list[Instrument]] = defaultdict(list)
    for i in insts:
        groups[i.market_id].append(i)
    rows = []
    for mid, levels in groups.items():
        levels.sort(key=lambda i: i.book_level)
        best = levels[0]
        rows.append({
            "market_id": mid, "venue": best.venue.value, "type": best.type.value,
            "contract": best.describe(), "strike": best.strike,
            "diesel_trigger": _diesel_trigger(best),
            "best_ask": best.quote, "best_cost": _r(best.unit_cost),
            "depth": sum(i.depth for i in levels), "levels": len(levels),
            "multiplier": best.multiplier, "settles_on": best.settlement_index,
            "basis": best.index_basis, "buffer": best.basis_buffer,
        })
    rows.sort(key=lambda r: (r["venue"], r["strike"]))
    return rows


def _table_rows(table: CarrierFuelTable, lo: float, hi: float, discount: float, cap: float) -> list[dict]:
    """Carrier bands in diesel terms within [lo, hi] (plus above-table steps)."""
    edges = np.concatenate([[lo], table.breakpoints(lo, hi)])
    edges = np.unique(round_price(edges[(edges >= lo) & (edges < hi)]))
    rows = []
    for k, start in enumerate(edges):
        end = edges[k + 1] if k + 1 < len(edges) else hi
        nominal = float(table.surcharge_at(start)[0])
        eff = round(nominal * (1 - discount), 10)
        rows.append({
            "from": _r(start, 3), "to": _r(end, 3), "nominal": nominal, "effective": eff,
            "over_cap": max(0.0, round(eff - cap, 10)),
        })
    return rows


def _curve(ctx: _Context, r: HedgeResult) -> dict[str, Any]:
    s = ctx.settings
    prices = round_price(np.arange(s.diesel_min, s.diesel_max + 1e-9, 0.01))
    exp = compute_exposure(r.profile, ctx.tables, prices)
    by_id = {i.instrument_id: i for i in ctx.snapshot.instruments}
    by_market: dict[str, np.ndarray] = {}
    for iid, q in r.quantities.items():
        inst = by_id[iid]
        by_market.setdefault(inst.market_id, np.zeros_like(prices))
        by_market[inst.market_id] = by_market[inst.market_id] + inst.payout(prices) * q
    payout = sum(by_market.values(), np.zeros_like(prices))
    net = r.fee_dollars - r.hedge_cost + payout - exp.liability
    return {
        "prices": [_r(p, 3) for p in prices],
        "nominal": [_r(v, 6) for v in exp.nominal],
        "effective": [_r(v, 6) for v in exp.effective],
        "charged": [_r(v, 6) for v in exp.charged],
        "liability": [_r(v) for v in exp.liability],
        "payout": [_r(v) for v in payout],
        "net": [_r(v) for v in net],
        "by_position": {mid: [_r(v) for v in arr] for mid, arr in by_market.items()},
    }


def _base_payload(ctx: _Context) -> dict[str, Any]:
    s, p, snap = ctx.settings, ctx.profile, ctx.snapshot
    spot = compute_exposure(p, ctx.tables, np.array([s.eia_diesel_spot]))
    services = []
    for svc, w in p.service_mix.items():
        t = ctx.tables[svc]
        nominal = float(t.surcharge_at(s.eia_diesel_spot)[0])
        services.append({
            "service": svc, "weight": w, "cap": p.cap_for(svc), "index_name": t.index_name,
            "index_offset": t.index_offset, "note": t.note,
            "spot_nominal": nominal, "spot_effective": round(nominal * (1 - p.carrier_fuel_discount), 10),
            "bands": _table_rows(t, s.diesel_min, s.diesel_max, p.carrier_fuel_discount, p.cap_for(svc)),
        })
    return {
        "inputs": ctx.req.model_dump(),
        "profile": p.model_dump(),
        "market": {
            "eia_diesel_spot": s.eia_diesel_spot, "ho_futures_price": s.ho_futures_price,
            "ho_contract": f"{s.ho_contract_code} {s.ho_contract_month}",
            "ho_multiplier": s.ho_contract_multiplier, "ho_fee": s.ho_fee_per_contract,
            "ho_basis": s.ho_index_basis, "ho_buffer": s.ho_basis_buffer,
            "kalshi_series": s.kalshi_series_ticker, "kalshi_basis": s.kalshi_index_basis,
            "kalshi_buffer": s.kalshi_basis_buffer, "kalshi_fee_rate": s.kalshi_taker_fee_rate,
            "kalshi_source": snap.kalshi_source, "cme_source": snap.cme_source,
            "retail_vol": s.retail_annual_vol, "ho_vol": s.diesel_annual_vol,
            "horizon_days": s.horizon_days, "as_of": snap.as_of, "notes": snap.notes,
            "n_kalshi_levels": sum(i.venue is Venue.KALSHI for i in snap.instruments),
            "n_cme_levels": sum(i.venue is Venue.CME for i in snap.instruments),
            "markets": _market_rows(snap.instruments),
        },
        "scenario_range": {"min": s.diesel_min, "max": s.diesel_max, "step": s.diesel_step},
        "spot": {
            "diesel": s.eia_diesel_spot, "nominal": float(spot.nominal[0]),
            "effective": float(spot.effective[0]), "charged": float(spot.charged[0]),
            "liability": float(spot.liability[0]),
        },
        "services": services,
    }


# ----------------------------------------------------------------------------- API
def run_quote(req: QuoteRequest) -> dict[str, Any]:
    ctx = _Context(req)
    payload = _base_payload(ctx)
    try:
        r = ctx.optimizer.solve(ctx.profile, ctx.snapshot.instruments)
    except InfeasibleHedgeError as exc:
        alt = lowest_feasible_cap(
            ctx.optimizer, ctx.profile, ctx.snapshot.instruments,
            [c for c in cap_grid(0.0, 0.40, 0.0025) if c > ctx.profile.promised_cap],
        )
        payload.update({
            "status": "infeasible",
            "report": exc.report.model_dump(mode="json", exclude={"profile"}),
            "suggested_cap": alt.model_dump() if alt else None,
        })
        return payload

    p = ctx.profile
    blended_cap = sum(w * p.cap_for(k) for k, w in p.service_mix.items())
    ev = ctx.fair_value.expected_liability(ctx.optimizer, p)
    pb = ctx.fair_value.prob_cap_binds(ctx.optimizer, p)
    curve = _curve(ctx, r)
    # expected hedge payout over claims, from the same lognormal sample
    sample = ctx.fair_value.sample()
    by_id = {i.instrument_id: i for i in ctx.snapshot.instruments}
    pay = sum((by_id[k].payout(sample) * q for k, q in r.quantities.items()), np.zeros_like(sample))
    surplus = float((pay - compute_exposure(p, ctx.tables, sample).liability).mean())

    held = defaultdict(list)
    for iid, q in r.quantities.items():
        held[by_id[iid].market_id].append(by_id[iid])
    orders = []
    for o in r.orders:
        inst = held[o.market_id][0]
        orders.append({
            **o.model_dump(mode="json"),
            "diesel_trigger": _diesel_trigger(inst),
            "multiplier": inst.multiplier,
            "unit_payout_note": (
                f"${inst.multiplier:,.0f} per $1 of HO above {inst.strike:.2f}"
                if inst.type is InstrumentType.CALL else "$1.00 if it settles in the money"
            ),
        })

    covered = [a for a in r.audit if a.liability > 0]
    tight = min(covered, key=lambda a: a.hedge_payout - a.liability) if covered else None
    payload.update({
        "status": "ok",
        "kpis": {
            "fee_dollars": r.fee_dollars, "fee_pct": r.fee_pct, "hedge_cost": r.hedge_cost,
            "hedge_premium": r.hedge_premium, "hedge_fees": r.hedge_fees,
            "min_profit": p.min_profit_dollars, "blended_cap": blended_cap,
            "all_in_max_pct": blended_cap * 100 + r.fee_pct,
            "trigger_price": r.trigger_price, "coverage_ceiling": r.coverage_ceiling,
            "tail_scan_max": r.tail_scan_max, "expected_claims": ev, "prob_binds": pb,
            "expected_pnl": p.min_profit_dollars + surplus,
            "fee_rounding": _r(r.fee_dollars - r.hedge_cost - p.min_profit_dollars),
        },
        "orders": orders,
        "solver": r.solver.model_dump() if r.solver else None,
        "audit": {
            "rows": [a.model_dump() for a in r.audit],
            "all_passed": r.all_passed, "min_net_pnl": r.min_net_pnl,
            "worst_scenario": r.worst_scenario,
            "n_grid": sum(a.on_grid for a in r.audit), "n_total": len(r.audit),
            "tightest": tight.model_dump() if tight else None,
        },
        "curve": curve,
        "warnings": r.warnings,
    })
    return payload


def run_sweep(req: QuoteRequest, caps: list[float] | None = None) -> dict[str, Any]:
    ctx = _Context(req)
    if not caps:
        c0 = req.promised_cap
        caps = [round(c0 + k * 0.005, 4) for k in range(-3, 5) if c0 + k * 0.005 > 0]
    caps = sorted({round(c, 4) for c in caps})
    with ThreadPoolExecutor(max_workers=4) as pool:
        quotes = list(pool.map(
            lambda c: quote_cap(ctx.optimizer, ctx.profile, ctx.snapshot.instruments, c, ctx.fair_value),
            caps,
        ))
    return {"current_cap": req.promised_cap, "quotes": [q.model_dump() for q in quotes]}


def defaults() -> dict[str, Any]:
    return QuoteRequest().model_dump()


def json_safe(obj: Any) -> Any:
    """Replace NaN/inf (not valid JSON) with None, recursively."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
