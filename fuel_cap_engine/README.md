# Fuel Surcharge Cap Engine

Price a **guaranteed cap on a parcel shipper's fuel surcharge** and solve for the
**minimum-cost hedge** that makes the guarantee risk-free, using Mixed-Integer Linear
Programming (HiGHS via `scipy.optimize.milp`).

The shipper pays a protection fee up front. If the carrier's discounted fuel surcharge
ends up above the promised cap, you cover the excess. The engine buys derivatives
(Kalshi diesel binaries and CME NY Harbor ULSD calls) whose payouts cover the excess in
**every** diesel scenario. So the fee you charge is the cost of those hedges plus a margin
you're guaranteed to keep.

## Quick start

```bash
pip install -r requirements-dev.txt
python -m fuel_cap_engine.main                                   # default demo shipper
python -m fuel_cap_engine.main --spend 4000000 --discount 0.25 --cap 0.14 --min-profit 2500
python -m fuel_cap_engine.main --sweep 0.115:0.16:0.005          # pricing menu by cap level
python -m fuel_cap_engine.main --max-fee-pct 0.25                # tightest cap for a fee budget
python -m fuel_cap_engine.main --profile examples/shipper_profile.json --json out.json --audit-csv audit.csv
python -m pytest
```

Exit codes: `0` hedged and audited, `2` infeasible (not enough market depth), `3` audit
failure, `1` other error.

## Web UI (Fuel Cap Desk)

```bash
python -m fuel_cap_engine.webapp              # then open http://127.0.0.1:8000
python -m fuel_cap_engine.webapp --export snapshot.html   # static page with the default quote baked in
```

Enter the shipper and market inputs on the left and click **Run quote**. The page shows:

- **Quote**: protection fee (% of spend and $), guaranteed cap, the shipper's worst case
  (cap + fee), the diesel price where the cap starts paying, hedge cost, your margin, and
  the chance the cap pays out.
- **How this quote is built**: five numbered steps with the real arithmetic. They cover
  the carrier table lookup, what you owe, how each contract maps to a pump-diesel price,
  the optimizer's problem, and the fee calculation.
- **Scenario explorer**: drag the slider or hover the chart to pick a diesel price. A
  panel breaks down the surcharge, what you owe, what each contract pays, and your net
  profit at that price.
- **Profit chart, trade ticket, pricing menu** (click **Use** to re-quote at another
  cap), **market snapshot** and **verification audit**.

The server uses only the Python standard library (`webapp.py`). All pricing runs in
`service.py`, which returns the numbers as JSON; the page in `web/index.html` only
displays them.

## Modules

| Module | Role |
|---|---|
| `config.py` | `EngineSettings`: scenario grid, market levels, basis model, fees, solver knobs. Any field can be overridden with an `FSC_<FIELD>` environment variable. Also holds the default shipper profile. |
| `schemas.py` | Pydantic models: `Instrument` (with exact payout functions), `ShipperProfile`, `HedgeResult`, `AuditRow`, `InfeasibilityReport`. |
| `carrier_grid.py` | `CarrierFuelTable`: band lookup, above-table extrapolation, basis shifts (`shifted`), matrix adjustments (`adjusted`: add/multiply/per-band overrides/arbitrary function), and CSV import/export. Includes illustrative UPS Ground (EIA diesel) and Air (jet-fuel proxy) baseline tables. |
| `market_feed.py` | `KalshiClient` for Kalshi Trade API v2 (`/markets`, `/markets/{ticker}/orderbook`) with an HTTP transport and a mock transport that returns the same JSON shape. `CMEOptionChainSimulator` builds an HO call chain using Black-76, call skew, $0.05/$0.10 strikes, tick-rounded spreads and book depth. Each book level becomes its own instrument, so the solver walks the book at the correct prices. |
| `optimizer.py` | MILP formulation, exact pre-solve feasibility check and infeasibility diagnosis, post-solve audit, and tail-coverage scan. |
| `analytics.py` | Cap sweep (pricing menu), budget search (tightest cap for a given fee %), lowest feasible cap, and a lognormal fair-value model. |
| `reporting.py` / `main.py` | Console formatting and the CLI. |

## Math

Decision variables: x_i ∈ ℤ≥0, the number of contracts bought at each book level.

```
min   Σ c_i x_i                          c_i = ask × multiplier + fee
s.t.  Σ Payout_i(s) x_i ≥ Liability(s) + $0.01     ∀ s with Liability(s) > 0
      0 ≤ x_i ≤ L_i

Liability(s) = Σ_services V·w_k · max(0, Table_k(s)·(1 − discount) − cap_k)
P$ = K_hedge + min_profit        P% = P$ / V × 100
Net P&L(s) = P$ − K_hedge + Payout(s) − Liability(s) ≥ min_profit   ∀ s
```

Implementation details beyond the base spec:

- **Continuous guarantee.** Liability is a step function and binary payouts are steps,
  while call payouts are continuous and non-decreasing. So on every interval between
  breakpoints, the payout-minus-liability gap is smallest at the interval's left edge. The
  engine adds every carrier band edge and instrument strike (mapped into diesel terms) to
  the $0.05 grid, so the guarantee holds at every price in [$3.50, $6.50], not just on the
  grid. A test brute-forces this on a $0.001 grid.
- **Exact infeasibility diagnosis.** Payouts are never negative, so buying the entire
  visible book maximizes the payout in every scenario at once. The problem is feasible if
  and only if `Payout @ L ≥ Liability`. When it isn't, the report lists the uncoverable
  scenarios, the markets whose whole book is used up, how many more contracts would close
  the gap, and whether the strikes are listed too high to reach the cap trigger. The CLI
  then suggests the lowest cap that current liquidity can guarantee.
- **Basis model.** `hedge_index = D + index_basis − basis_buffer`. NY Harbor ULSD futures
  trade about $1.30 below EIA retail. The buffer is a deliberate haircut, so the guarantee
  survives that much basis slippage.
- **Fees.** Kalshi's taker fee (0.07·P·(1−P), rounded up to the cent per fill) and a
  per-contract all-in CME fee are included in both the objective and the final cost.
- **Tail scan.** After solving, the engine scans up to $10 diesel and reports where (if
  anywhere) the hedge stops covering the liability.

---

## Running this as a business

### What the shipper buys
"Your effective UPS fuel surcharge will never exceed **X%** this month. The cost is **P%**
of base transportation spend, paid up front." The shipper's worst-case all-in fuel cost
is **X + P%**. That's the number to sell, and the CLI prints it.

### How to price it
The engine's fee is a **floor**: what it costs you to make the guarantee risk-free, plus
your minimum margin. You can charge more. The `--sweep` menu shows, for each cap level:

- **Fee $ / Fee %**: the risk-free price.
- **All-in max**: cap + fee, i.e. the shipper's guaranteed worst case.
- **P(cap binds)** and **E[claims]**: the actuarial view from a lognormal model.
- **Fee / E[claims]**: how much friction you're charging for. Far out-of-the-money caps
  look cheap in dollars but are expensive relative to expected claims. Most of that fee is
  bid/ask spread, contract lumpiness, and margin.

Where to set the cap: offer two or three tiers, for example roughly +10%, +15%, and +20%
above today's diesel price (or simply the caps that hit fee budgets of 0.1%, 0.25%, and
0.5%). Use `--max-fee-pct` to solve for each tier. A cap near today's effective surcharge
costs about as much as the expected claims and rarely sells; caps far above it are cheap
catastrophe cover.

### Timing: match the hedge to how the surcharge is set
- UPS resets its fuel surcharge **weekly**, based on the EIA diesel price from **two weeks
  earlier**. At sale time, the first ~2 weeks of surcharges are already known, so only
  hedge the weeks still unknown.
- Run one optimization per **settlement period** and set `V` to the spend in that period.
  Weekly Kalshi contracts line up with weekly surcharges. A monthly HO option settles on
  one price, while your liability is the sum of 4–5 weekly prices. That mismatch is
  basis, so keep a buffer, or use CME's monthly **average-price** ULSD options, which
  track a monthly average much more closely.
- For a 12-month lock, price each month separately with that month's instruments. HO
  options are listed years out; Kalshi is short-dated. You buy the hedges up front, so
  budget the working capital.

### Risks the engine covers and the ones it doesn't
- **Covered:** diesel moves anywhere in the scenario range, order-book depth, contract
  lumpiness, and fees. The hedge is long-only, so there are no margin calls and the most
  you can lose on it is the premium paid.
- **Basis:** retail diesel vs. NY Harbor wholesale (retail lags wholesale by 1–2 weeks),
  Kalshi's settlement source vs. EIA, and jet fuel vs. diesel for Air. These are covered
  only up to `basis_buffer`. Backtest the EIA-vs-HO spread and size the buffer from it.
- **Tail above `diesel_max`:** reported as a warning. The 2022 peak EIA diesel was about
  $5.81.
- **Carrier table changes:** UPS revises its tables. If the carrier changes the table
  mid-term, your liability changes too. Write contract language that ties the cap to the
  table in force at signing, or re-price.
- **Shipper volume:** the liability scales with actual spend. If spend comes in above
  `V`, you're under-hedged. Cap the covered spend in the contract, or hedge to a
  higher `V`.

### Scale
Kalshi depth is thin, and one HO contract (42,000 gal) pays about $42,000 per $1 move,
which is lumpy for a shipper spending $1–3M a month. The economics improve when you
**pool many shippers into one book**: liabilities add up, the lumpiness averages out,
and one hedge serves everyone. The optimizer already sums liabilities across services,
so a multi-shipper book is the natural next step.

### Legal structure (talk to counsel)
Selling a cash-settled price cap to a third party for a premium can be regulated as a
**swap or commodity option** (CFTC) or as **insurance** (state regulators). The usual way
around this is to be the shipper's **reseller or 3PL**: you bill them the shipping charges
yourself, and the capped surcharge is just a pricing term in your shipping contract. The
engine's `min_profit_dollars` "reseller margin" assumes that structure. Hedging requires
a futures/options account with an FCM for HO, and a Kalshi account for the binaries.

### Going live: data you need
1. **Current carrier fuel tables.** Load them with `--ground-table-csv` / `--air-table-csv`
   (`lower,upper,surcharge_pct`). The built-in tables are **illustrative only**.
2. **EIA weekly diesel.** EIA open-data API, U.S. No. 2 Diesel retail, weekly series
   `EMD_EPD2D_PTE_NUS_DPG`. Set it with `--diesel-spot` or `FSC_EIA_DIESEL_SPOT`.
3. **Kalshi:** `--live-kalshi`. Market data is public. Confirm the series ticker
   (`FSC_KALSHI_SERIES_TICKER`), its settlement source, and its strike type in the
   contract rules.
4. **CME HO options:** replace `CMEOptionChainSimulator` with a broker or data-vendor feed
   that returns the same `OptionQuote` list.
