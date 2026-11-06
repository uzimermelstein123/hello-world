"""CLI driver: shipper profile -> market snapshot -> MILP hedge -> trade ticket + quote.

Examples::

    python -m fuel_cap_engine.main
    python -m fuel_cap_engine.main --spend 4000000 --discount 0.25 --cap 0.14 --min-profit 2500
    python -m fuel_cap_engine.main --sweep 0.11:0.17:0.005          # pricing menu by cap
    python -m fuel_cap_engine.main --max-fee-pct 0.40               # tightest cap for a budget
    python -m fuel_cap_engine.main --profile examples/shipper_profile.json --json out.json

Exit codes: 0 = hedged & audited, 2 = infeasible (insufficient depth), 3 = audit failure,
1 = other error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

from . import config, reporting as rpt
from .analytics import (
    LognormalDieselModel,
    cap_grid,
    cap_sweep,
    lowest_cap_for_budget,
    lowest_feasible_cap,
)
from .carrier_grid import CarrierFuelTable, default_tables
from .market_feed import MarketDataError, MarketSnapshot, load_market
from .optimizer import HedgeOptimizer, HedgeSolverError, InfeasibleHedgeError, compute_exposure
from .schemas import HedgeResult, Instrument, ShipperProfile, Venue

log = logging.getLogger("fuel_cap_engine")


def _parse_mix(s: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in s.split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = float(v)
    return out


def _parse_range(s: str) -> tuple[float, float, float]:
    lo, hi, step = (float(x) for x in s.split(":"))
    return lo, hi, step


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fuel_cap_engine",
        description="Price a guaranteed fuel-surcharge cap and solve the optimal hedge (MILP/HiGHS).",
    )
    g = p.add_argument_group("shipper profile")
    g.add_argument("--profile", type=Path, help="JSON ShipperProfile (flags below override it)")
    g.add_argument("--name")
    g.add_argument("--spend", type=float, help="V: monthly billable base transportation spend ($)")
    g.add_argument("--discount", type=float, help="carrier fuel discount (0.30 = 30%% off)")
    g.add_argument("--cap", type=float, help="promised max effective fuel surcharge (0.13 = 13%%)")
    g.add_argument("--min-profit", type=float, help="risk-free $ margin per period")
    g.add_argument("--service-mix", type=_parse_mix, help="e.g. ground=0.8,air=0.2")

    g = p.add_argument_group("carrier tables")
    g.add_argument("--ground-table-csv", type=Path, help="CSV lower,upper,surcharge_pct for Ground")
    g.add_argument("--air-table-csv", type=Path, help="CSV lower,upper,surcharge_pct for Air (jet index)")
    g.add_argument("--index-shift", type=float, default=0.0, help="basis shift of all table thresholds ($/gal)")
    g.add_argument("--pct-add", type=float, default=0.0, help="add to every surcharge level (fraction)")
    g.add_argument("--pct-mult", type=float, default=1.0, help="multiply every surcharge level")

    g = p.add_argument_group("market / scenarios")
    g.add_argument("--diesel-spot", type=float, help="latest EIA retail diesel ($/gal)")
    g.add_argument("--ho-futures", type=float, help="NY Harbor ULSD futures ($/gal)")
    g.add_argument("--vol", type=float, help="annualized HO (wholesale) vol; retail vol is scaled by HO/retail")
    g.add_argument("--horizon-days", type=int)
    g.add_argument("--diesel-range", type=_parse_range, help="min:max:step, default 3.50:6.50:0.05")
    g.add_argument("--ho-basis-buffer", type=float)
    g.add_argument("--kalshi-basis-buffer", type=float)
    g.add_argument("--live-kalshi", action="store_true", help="pull the live Kalshi order book")
    g.add_argument("--no-kalshi", action="store_true")
    g.add_argument("--no-cme", action="store_true")
    g.add_argument("--seed", type=int, help="mock market seed")

    g = p.add_argument_group("pricing analytics")
    g.add_argument("--sweep", type=_parse_range, metavar="LO:HI:STEP", help="cap menu, e.g. 0.11:0.17:0.005")
    g.add_argument("--max-fee-pct", type=float, help="find the tightest cap with fee <= this %% of spend")

    g = p.add_argument_group("output")
    g.add_argument("--full-audit", action="store_true", help="include breakpoint rows in the audit table")
    g.add_argument("--audit-csv", type=Path)
    g.add_argument("--json", type=Path, help="write the full HedgeResult as JSON")
    g.add_argument("--log-level", default="INFO")
    return p


def build_profile(args: argparse.Namespace) -> ShipperProfile:
    data: dict = json.loads(args.profile.read_text()) if args.profile else {
        "name": config.DEFAULT_SHIPPER_NAME,
        "monthly_spend": config.DEFAULT_MONTHLY_SPEND,
        "carrier_fuel_discount": config.DEFAULT_FUEL_DISCOUNT,
        "promised_cap": config.DEFAULT_PROMISED_CAP,
        "min_profit_dollars": config.DEFAULT_MIN_PROFIT,
    }
    for flag, key in [("name", "name"), ("spend", "monthly_spend"), ("discount", "carrier_fuel_discount"),
                      ("cap", "promised_cap"), ("min_profit", "min_profit_dollars"),
                      ("service_mix", "service_mix")]:
        if getattr(args, flag) is not None:
            data[key] = getattr(args, flag)
    return ShipperProfile(**data)


def build_settings(args: argparse.Namespace) -> config.EngineSettings:
    ov = {
        "eia_diesel_spot": args.diesel_spot, "ho_futures_price": args.ho_futures,
        "diesel_annual_vol": args.vol, "horizon_days": args.horizon_days,
        "ho_basis_buffer": args.ho_basis_buffer, "kalshi_basis_buffer": args.kalshi_basis_buffer,
        "mock_seed": args.seed,
    }
    if args.diesel_range:
        ov["diesel_min"], ov["diesel_max"], ov["diesel_step"] = args.diesel_range
    return config.EngineSettings.from_env(**ov)


def build_tables(args: argparse.Namespace, s: config.EngineSettings) -> dict[str, CarrierFuelTable]:
    tables = default_tables(s.air_jet_index_offset)
    if args.ground_table_csv:
        tables["ground"] = CarrierFuelTable.from_csv(args.ground_table_csv, "ground")
    if args.air_table_csv:
        tables["air"] = CarrierFuelTable.from_csv(
            args.air_table_csv, "air", index_name="USGC_JET_FUEL", index_offset=s.air_jet_index_offset
        )
    if args.index_shift or args.pct_add or args.pct_mult != 1.0:
        tables = {k: t.shifted(args.index_shift).adjusted(add=args.pct_add, mult=args.pct_mult)
                  for k, t in tables.items()}
    return tables


def print_header(
    profile: ShipperProfile, s: config.EngineSettings, snap: MarketSnapshot,
    tables: dict[str, CarrierFuelTable],
) -> None:
    print(rpt.section("FUEL SURCHARGE CAP ENGINE - hedge & quote"))
    mix = ", ".join(f"{k} {w:.0%}" for k, w in profile.service_mix.items())
    print(f"  Shipper            {profile.name}")
    print(f"  Monthly base spend {rpt.usd(profile.monthly_spend)}   (mix: {mix})")
    caps = ", ".join(f"{k} {rpt.pct(profile.cap_for(k))}" for k in profile.service_mix)
    print(f"  Fuel discount      {rpt.pct(profile.carrier_fuel_discount)}   Promised cap: {caps}")
    print(f"  Min. margin        {rpt.usd(profile.min_profit_dollars)} per period (risk-free)")
    n_k = sum(i.venue is Venue.KALSHI for i in snap.instruments)
    n_c = sum(i.venue is Venue.CME for i in snap.instruments)
    print(f"  Market             EIA diesel ${s.eia_diesel_spot:.3f} | HO {s.ho_contract_month} "
          f"${s.ho_futures_price:.4f} | basis HO {s.ho_index_basis:+.2f} (buffer {s.ho_basis_buffer:.2f}), "
          f"Kalshi {s.kalshi_index_basis:+.2f} (buffer {s.kalshi_basis_buffer:.2f})")
    print(f"  Instruments        Kalshi {s.kalshi_series_ticker} [{snap.kalshi_source}]: {n_k} book levels | "
          f"CME {s.ho_contract_code} calls [{snap.cme_source}]: {n_c} book levels")
    print(f"  Scenario range     ${s.diesel_min:.2f} - ${s.diesel_max:.2f} step ${s.diesel_step:.2f}"
          f"{' + all breakpoints (continuous guarantee)' if s.continuous_guarantee else ''}")
    for note in snap.notes:
        print(f"  NOTE: {note}")
    for svc in profile.service_mix:
        if tables[svc].note:
            print(f"  Table [{svc}]: {tables[svc].index_name} - {tables[svc].note}")


def print_exposure(profile: ShipperProfile, s: config.EngineSettings, opt: HedgeOptimizer,
                   r: HedgeResult) -> None:
    print(rpt.section("EXPOSURE"))
    spot = compute_exposure(profile, opt.tables, np.array([s.eia_diesel_spot]))
    print(f"  At today's ${s.eia_diesel_spot:.3f}: carrier FSC {rpt.pct(spot.nominal[0])} -> "
          f"{rpt.pct(spot.effective[0])} after discount "
          f"({'ABOVE' if spot.liability[0] > 0 else 'below'} cap; liability {rpt.usd(spot.liability[0])})")
    if r.trigger_price is not None:
        print(f"  Cap starts paying out at diesel >= ${r.trigger_price:.3f} "
              f"({(r.trigger_price / s.eia_diesel_spot - 1):+.1%} vs today)")
    top = r.audit[-1]
    print(f"  Liability at ${top.diesel:.2f}: {rpt.usd(top.liability)} "
          f"(uncapped FSC {rpt.pct(top.effective_surcharge)} vs cap)")


def print_quote(
    r: HedgeResult, fv: LognormalDieselModel, opt: HedgeOptimizer, instruments: list[Instrument]
) -> None:
    p = r.profile
    print(rpt.section("CUSTOMER PRICING QUOTE"))
    caps = {k: p.cap_for(k) for k in p.service_mix}
    cap_txt = ", ".join(f"{k}: {rpt.pct(c)}" for k, c in caps.items())
    blended_cap = sum(w * caps[k] for k, w in p.service_mix.items())
    print(f"  Guaranteed max effective fuel surcharge  {cap_txt}")
    print(f"  Protection fee (P$)                      {rpt.usd(r.fee_dollars)} per month")
    print(f"  Protection fee (P%)                      {r.fee_pct:.3f}% of base transportation spend")
    print(f"  Shipper worst-case all-in fuel cost      {blended_cap * 100 + r.fee_pct:.3f}% of spend "
          f"(cap + fee), for any diesel price up to ${opt.s.diesel_max:.2f}")
    print(rpt.THIN)
    print(f"  Hedge cost K_hedge                       {rpt.usd(r.hedge_cost)}")
    print(f"  Reseller margin (guaranteed minimum)     {rpt.usd(p.min_profit_dollars)}")
    ev = fv.expected_liability(opt, p)
    pb = fv.prob_cap_binds(opt, p)
    print(f"  Fair-value reference (lognormal F=${fv.forward:.3f}, vol {fv.annual_vol:.0%}, "
          f"{fv.horizon_days}d): P(cap binds) {pb:.1%}, E[claims] {rpt.usd(ev)}")
    print(f"  Expected reseller P&L                    {rpt.usd(p.min_profit_dollars + _expected_hedge_surplus(r, fv, opt, instruments))}"
          f"  (min margin + expected hedge payout in excess of claims)")


def _expected_hedge_surplus(
    r: HedgeResult, fv: LognormalDieselModel, opt: HedgeOptimizer, instruments: list[Instrument]
) -> float:
    """E[hedge payout - liability] under the fair-value model (>= 0 inside the scenario range)."""
    prices = fv.sample()
    liab = compute_exposure(r.profile, opt.tables, prices).liability
    by_id = {i.instrument_id: i for i in instruments}
    pay = np.zeros_like(prices)
    for iid, q in r.quantities.items():
        pay += by_id[iid].payout(prices) * q
    return float((pay - liab).mean())


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        stream=sys.stderr)
    try:
        s = build_settings(args)
        profile = build_profile(args)
        tables = build_tables(args, s)
        snap = load_market(s, live_kalshi=args.live_kalshi, include_kalshi=not args.no_kalshi,
                           include_cme=not args.no_cme)
    except (ValueError, MarketDataError, KeyError, OSError) as exc:
        log.error("setup failed: %s", exc)
        return 1

    opt = HedgeOptimizer(tables, s)
    fv = LognormalDieselModel(forward=s.eia_diesel_spot, annual_vol=s.retail_annual_vol,
                              horizon_days=s.horizon_days)
    print_header(profile, s, snap, tables)

    if args.sweep:
        print(rpt.section("PRICING MENU - risk-free fee by cap level"))
        quotes = cap_sweep(opt, profile, snap.instruments, cap_grid(*args.sweep), fv)
        print(rpt.cap_menu(quotes, profile.promised_cap))
        print("  All-in max = cap + fee: the shipper's worst-case fuel cost as % of spend.\n"
              "  Fee/E[claims] = how many dollars of fee per expected dollar of claims (hedging friction + margin).")

    if args.max_fee_pct is not None:
        print(rpt.section(f"BUDGET SOLVE - tightest cap with fee <= {args.max_fee_pct:.3f}% of spend"))
        q = lowest_cap_for_budget(opt, profile, snap.instruments, args.max_fee_pct,
                                  cap_grid(0.0, 0.40, 0.0025))
        if q is None:
            print("  No cap up to 40.00% fits that budget with available liquidity.")
        else:
            print(f"  Tightest cap: {rpt.pct(q.cap)}  fee {rpt.usd(q.fee_dollars)} ({q.fee_pct:.3f}%)  "
                  f"all-in max {q.all_in_max_pct:.3f}%")
            profile = profile.model_copy(update={"promised_cap": q.cap, "service_caps": {}})
            print(f"  -> Solving full trade ticket at cap {rpt.pct(q.cap)}")

    try:
        result = opt.solve(profile, snap.instruments)
    except InfeasibleHedgeError as exc:
        print(rpt.section("HEDGE INFEASIBLE"))
        print(rpt.infeasibility(exc.report))
        alt = lowest_feasible_cap(opt, profile, snap.instruments,
                                  [c for c in cap_grid(0.0, 0.40, 0.0025) if c > profile.promised_cap])
        if alt:
            print(f"\n  Lowest cap that current liquidity CAN guarantee: {rpt.pct(alt.cap)} "
                  f"(fee {rpt.usd(alt.fee_dollars)} = {alt.fee_pct:.3f}% of spend)")
        print("  Options: raise the cap, lower the diesel_max of the guarantee, reduce the covered spend,\n"
              "  split the order over several sessions, or add venues/strikes (e.g. OTC calls from a dealer).")
        return 2
    except HedgeSolverError as exc:
        log.error("%s", exc)
        return 1

    print_exposure(profile, s, opt, result)
    print(rpt.section("TRADE ORDER TICKET"))
    print(rpt.order_ticket(result))
    if result.solver:
        st = result.solver
        print(f"  HiGHS: {st.message} | {st.n_variables} vars x {st.n_constraints} binding-scenario rows | "
              f"{st.runtime_sec:.3f}s | nodes {st.mip_node_count}")
    print_quote(result, fv, opt, snap.instruments)
    print(rpt.section("VERIFICATION AUDIT  (Net P&L = P$ - K_hedge + payout - liability)"))
    print(rpt.audit_table(result, full=args.full_audit))
    print(rpt.THIN)
    print(rpt.audit_summary(result))
    for w in result.warnings:
        print(f"  WARNING: {w}")

    if args.audit_csv:
        rpt.write_audit_csv(result, args.audit_csv)
        log.info("audit written to %s", args.audit_csv)
    if args.json:
        args.json.write_text(result.model_dump_json(indent=2))
        log.info("result written to %s", args.json)
    return 0 if result.all_passed else 3


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
