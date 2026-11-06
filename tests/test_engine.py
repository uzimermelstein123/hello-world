import json

import numpy as np
import pytest

from fuel_cap_engine.analytics import cap_grid, cap_sweep, lowest_cap_for_budget, lowest_feasible_cap
from fuel_cap_engine.carrier_grid import CarrierFuelTable, FuelBand, generate_table, ups_ground_baseline
from fuel_cap_engine.config import EngineSettings
from fuel_cap_engine.main import run
from fuel_cap_engine.market_feed import (
    CMEOptionChainSimulator,
    KalshiClient,
    MockKalshiTransport,
    load_market,
)
from fuel_cap_engine.optimizer import HedgeOptimizer, InfeasibleHedgeError, compute_exposure
from fuel_cap_engine.schemas import Instrument, InstrumentType, ShipperProfile, Venue


# ----------------------------------------------------------------------------- helpers
def binary(k, quote, depth, strict=False, basis=0.0, buffer=0.0, fee=0.0, mid=None):
    return Instrument(
        instrument_id=f"B{k}@{quote}", market_id=mid or f"B{k}", venue=Venue.KALSHI,
        type=InstrumentType.BINARY_ABOVE, strike=k, strict=strict, quote=quote, multiplier=1.0,
        fee_per_contract=fee, depth=depth, index_basis=basis, basis_buffer=buffer,
    )


def call(k, quote, depth, mult=1000.0, basis=0.0, buffer=0.0):
    return Instrument(
        instrument_id=f"C{k}@{quote}", market_id=f"C{k}", venue=Venue.CME, type=InstrumentType.CALL,
        strike=k, quote=quote, multiplier=mult, depth=depth, index_basis=basis, basis_buffer=buffer,
    )


def step_table():
    """10% below $4.00, 20% from $4.00 up (flat above)."""
    return CarrierFuelTable(
        service="ground", index_name="TEST",
        bands=[FuelBand(lower=0.0, upper=4.0, surcharge=0.10), FuelBand(lower=4.0, upper=5.0, surcharge=0.20)],
        above_table_step_width=1.0, above_table_step_surcharge=0.0,
    )


def profile(**kw):
    base = dict(monthly_spend=100_000, carrier_fuel_discount=0.0, promised_cap=0.15, min_profit_dollars=500)
    base.update(kw)
    return ShipperProfile(**base)


@pytest.fixture(scope="module")
def settings():
    return EngineSettings()


@pytest.fixture(scope="module")
def market(settings):
    return load_market(settings)


# ----------------------------------------------------------------------- carrier grid
def test_ground_table_lookup_and_edges():
    t = ups_ground_baseline()
    assert t.surcharge_at(3.75)[0] == pytest.approx(0.175)
    # band [3.72, 3.81) -> 17.50%; the edge belongs to the upper band
    assert t.surcharge_at(3.81)[0] == pytest.approx(0.1775)
    assert t.surcharge_at(3.8099)[0] == pytest.approx(0.175)
    # above table: top = 6.06 @ 23.75%; +0.25% per started $0.09
    assert t.surcharge_at(6.06)[0] == pytest.approx(0.2400)
    assert t.surcharge_at(6.15)[0] == pytest.approx(0.2425)
    assert t.surcharge_at(2.50)[0] == pytest.approx(0.155)  # below table -> first band


def test_breakpoints_include_above_table_steps():
    t = ups_ground_baseline()
    bps = t.breakpoints(6.0, 6.5)
    assert {6.06, 6.15, 6.24, 6.33, 6.42}.issubset(set(np.round(bps, 2)))


def test_shift_and_adjust():
    t = ups_ground_baseline()
    s = t.shifted(0.10)
    assert s.surcharge_at(3.85)[0] == pytest.approx(t.surcharge_at(3.75)[0])
    a = t.adjusted(add=0.01, overrides={0: 0.05})
    assert a.surcharge_at(3.75)[0] == pytest.approx(0.185)
    assert a.surcharge_at(3.00)[0] == pytest.approx(0.05)


def test_csv_round_trip(tmp_path):
    t = ups_ground_baseline()
    f = tmp_path / "g.csv"
    t.to_csv(f)
    t2 = CarrierFuelTable.from_csv(f, "ground")
    prices = np.arange(3.0, 7.0, 0.013)
    np.testing.assert_allclose(t.surcharge_at(prices), t2.surcharge_at(prices))


def test_air_table_uses_jet_offset(settings):
    from fuel_cap_engine.carrier_grid import ups_air_baseline
    t = ups_air_baseline(-1.45)
    assert t.surcharge_at(3.75)[0] == pytest.approx(0.19 + 4 * 0.005)  # jet 2.30


def test_non_contiguous_table_rejected():
    with pytest.raises(ValueError):
        CarrierFuelTable(service="x", index_name="x", above_table_step_width=1, above_table_step_surcharge=0,
                         bands=[FuelBand(lower=0, upper=1, surcharge=0.1), FuelBand(lower=1.5, upper=2, surcharge=0.2)])


# ------------------------------------------------------------------------ instruments
def test_instrument_payouts():
    b = binary(4.0, 0.3, 10, strict=True)
    assert b.payout(np.array([3.99, 4.0, 4.01])).tolist() == [0, 0, 1]
    b2 = binary(4.0, 0.3, 10)
    assert b2.payout(4.0)[0] == 1
    c = call(2.70, 0.02, 5, mult=42_000, basis=-1.30, buffer=0.05)
    # D=4.20 -> HO-equivalent 4.20 - 1.30 - 0.05 = 2.85 -> 0.15 * 42000
    assert c.payout(4.20)[0] == pytest.approx(6300)
    assert c.carrier_breakpoints() == [4.05]
    r = Instrument(instrument_id="r", market_id="r", venue=Venue.KALSHI, type=InstrumentType.BINARY_RANGE,
                   strike=4.0, upper_strike=4.2, quote=0.1, multiplier=1, depth=1)
    assert r.payout(np.array([3.99, 4.0, 4.19, 4.2])).tolist() == [0, 1, 1, 0]


# ------------------------------------------------------------------------ market feed
def test_kalshi_orderbook_parsing_both_formats():
    cents = {"orderbook": {"yes": [[40, 10]], "no": [[55, 100], [58, 50]]}}
    dollars = {"orderbook_fp": {"no_dollars": [["0.5500", "100.00"], ["0.5800", "50.00"]]}}
    for payload in (cents, dollars):
        asks = KalshiClient.parse_yes_asks(payload)
        assert asks == [(0.42, 50), (0.45, 100)]


def test_kalshi_contract_spec():
    assert KalshiClient.contract_spec({"strike_type": "greater", "floor_strike": 4.1}) == (
        InstrumentType.BINARY_ABOVE, 4.1, None, True)
    assert KalshiClient.contract_spec({"strike_type": "between", "floor_strike": 4.0, "cap_strike": 4.2})[0] \
        is InstrumentType.BINARY_RANGE
    assert KalshiClient.contract_spec({"strike_type": "less", "cap_strike": 3.0}) is None


def test_mock_kalshi_books_are_sane(settings):
    insts = KalshiClient(MockKalshiTransport(settings), settings).fetch_instruments()
    assert insts
    by_market: dict[str, list[Instrument]] = {}
    for i in insts:
        assert 0 < i.quote < 1 and i.depth > 0
        by_market.setdefault(i.market_id, []).append(i)
    for levels in by_market.values():
        quotes = [i.quote for i in sorted(levels, key=lambda x: x.book_level)]
        assert quotes == sorted(quotes)  # deeper levels are not cheaper


def test_cme_chain_strike_intervals_and_spreads(settings):
    sim = CMEOptionChainSimulator(settings)
    ks = sim.strikes()
    diffs = np.round(np.diff(ks), 2)
    assert set(diffs) <= {0.05, 0.10}
    chain = sim.chain()
    assert all(q.ask > q.bid >= 0 for q in chain)
    mids = [q.mid for q in chain]
    assert mids == sorted(mids, reverse=True)  # call value decreases with strike


# -------------------------------------------------------------------------- optimizer
def test_milp_integer_optimum_hand_solved(settings):
    """min 0.3b + 50c  s.t. b + 100c >= 5000.01, b <= 3000  ->  c=21, b=2901, cost 1920.30."""
    opt = HedgeOptimizer({"ground": step_table()}, settings)
    r = opt.solve(profile(), [binary(4.0, 0.30, 3000), call(3.90, 0.05, 100)])
    assert r.quantities == {"B4.0@0.3": 2901, "C3.9@0.05": 21}
    assert r.hedge_cost == pytest.approx(1920.30)
    assert r.fee_dollars == pytest.approx(1920.30 + 500)
    assert r.fee_pct == pytest.approx((1920.30 + 500) / 100_000 * 100)
    assert r.all_passed and r.min_net_pnl >= 500 - 1e-9


def test_no_liability_means_no_hedge(settings):
    opt = HedgeOptimizer({"ground": step_table()}, settings)
    r = opt.solve(profile(promised_cap=0.25), [binary(4.0, 0.30, 3000)])
    assert r.orders == [] and r.hedge_cost == 0 and r.fee_dollars == 500
    assert r.all_passed


def test_infeasible_reports_exhausted_liquidity(settings):
    opt = HedgeOptimizer({"ground": step_table()}, settings)
    with pytest.raises(InfeasibleHedgeError) as ei:
        opt.solve(profile(), [binary(4.0, 0.30, 1000), binary(4.5, 0.10, 500)])
    rep = ei.value.report
    assert rep.shortfalls and rep.shortfalls[0].diesel == pytest.approx(4.0)
    assert rep.shortfalls[0].shortfall == pytest.approx(5000.01 - 1000)
    # worst gap is at $4.00 (4,000.01) where only the $4.00 binary pays
    assert {e.market_id for e in rep.exhausted} == {"B4.0"}
    assert rep.exhausted[0].additional_needed == 4001
    assert "INFEASIBLE" in rep.summary


def test_strikes_too_high_are_flagged(settings):
    opt = HedgeOptimizer({"ground": step_table()}, settings)
    with pytest.raises(InfeasibleHedgeError) as ei:
        opt.solve(profile(), [binary(4.3, 0.10, 100_000)])
    assert ei.value.report.uncovered_prices[0] == pytest.approx(4.0)


def test_continuous_guarantee_brute_force(settings, market):
    """The guarantee must hold at every price, not only on the $0.05 grid."""
    tables = {"ground": ups_ground_baseline()}
    opt = HedgeOptimizer(tables, settings)
    p = ShipperProfile(monthly_spend=2_500_000, carrier_fuel_discount=0.30, promised_cap=0.13,
                       min_profit_dollars=1000)
    r = opt.solve(p, market.instruments)
    assert r.all_passed
    by_id = {i.instrument_id: i for i in market.instruments}
    fine = np.round(np.arange(settings.diesel_min, settings.diesel_max + 1e-9, 0.001), 6)
    pay = sum(by_id[k].payout(fine) * q for k, q in r.quantities.items())
    liab = compute_exposure(p, tables, fine).liability
    assert np.all(pay >= liab)


def test_basis_buffer_costs_more(settings, market):
    tables = {"ground": ups_ground_baseline()}
    p = ShipperProfile(monthly_spend=2_500_000, carrier_fuel_discount=0.30, promised_cap=0.125,
                       min_profit_dollars=1000)
    loose = HedgeOptimizer(tables, settings.model_copy(update={"ho_basis_buffer": 0.0})).solve(
        p, load_market(settings.model_copy(update={"ho_basis_buffer": 0.0, "kalshi_basis_buffer": 0.0})).instruments)
    tight = HedgeOptimizer(tables, settings).solve(p, market.instruments)
    assert tight.hedge_cost >= loose.hedge_cost


# -------------------------------------------------------------------------- analytics
def test_fee_monotone_in_cap_and_budget_search(settings, market):
    opt = HedgeOptimizer({"ground": ups_ground_baseline()}, settings)
    p = ShipperProfile(monthly_spend=2_500_000, carrier_fuel_discount=0.30, promised_cap=0.13,
                       min_profit_dollars=1000)
    caps = cap_grid(0.115, 0.145, 0.005)
    quotes = cap_sweep(opt, p, market.instruments, caps)
    fees = [q.fee_dollars for q in quotes if q.feasible]
    assert fees == sorted(fees, reverse=True)
    budget = 0.25
    q = lowest_cap_for_budget(opt, p, market.instruments, budget, caps)
    linear = next(x for x in quotes if x.feasible and x.fee_pct <= budget)
    assert q is not None and q.cap == linear.cap


def test_lowest_feasible_cap(settings):
    opt = HedgeOptimizer({"ground": step_table()}, settings)
    insts = [binary(4.0, 0.30, 3000)]
    q = lowest_feasible_cap(opt, profile(), insts, cap_grid(0.10, 0.20, 0.01))
    # need V*(0.20-cap) + $0.01 safety margin <= 3000: cap 0.17 misses by a cent -> 0.18
    assert q is not None and q.cap == pytest.approx(0.18)


# -------------------------------------------------------------------------------- CLI
def test_cli_end_to_end(tmp_path, capsys):
    out = tmp_path / "r.json"
    csv_path = tmp_path / "audit.csv"
    code = run(["--log-level", "ERROR", "--json", str(out), "--audit-csv", str(csv_path)])
    assert code == 0
    data = json.loads(out.read_text())
    assert data["all_passed"] is True
    assert csv_path.read_text().count("\n") == len(data["audit"]) + 1
    text = capsys.readouterr().out
    assert "TRADE ORDER TICKET" in text and "Audit PASSED" in text


def test_cli_infeasible_exit_code(capsys):
    code = run(["--log-level", "ERROR", "--no-cme", "--spend", "20000000", "--cap", "0.125"])
    assert code == 2
    assert "HEDGE INFEASIBLE" in capsys.readouterr().out
