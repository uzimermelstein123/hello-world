"""Console formatting: currency, percentages, and fixed-width tables."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from .analytics import CapQuote
from .schemas import HedgeResult, InfeasibilityReport, OrderLine, Venue

RULE = "=" * 100
THIN = "-" * 100


def usd(x: float, cents: bool = True) -> str:
    s = f"${abs(x):,.2f}" if cents else f"${abs(x):,.0f}"
    return f"-{s}" if x < 0 else s


def pct(frac: float, digits: int = 2) -> str:
    """Format a fraction (0.1325) as a percentage."""
    return f"{frac * 100:.{digits}f}%"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], align: str | None = None) -> str:
    """Render a fixed-width table. ``align``: one char per column, 'l' or 'r'."""
    align = align or "l" + "r" * (len(headers) - 1)
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]

    def fmt(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) if a == "l" else c.rjust(w) for c, w, a in zip(cells, widths, align))

    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])


def section(title: str) -> str:
    return f"\n{RULE}\n {title}\n{RULE}"


def _limit(o: OrderLine) -> str:
    return f"${o.limit_price:.2f}" if o.venue is Venue.KALSHI else f"${o.limit_price:.4f}/gal"


def order_ticket(r: HedgeResult) -> str:
    if not r.orders:
        return "  No contracts required."
    rows = []
    for n, o in enumerate(r.orders, 1):
        rows.append([
            str(n), o.venue.value, o.market_id, o.contract, f"{o.quantity:,}",
            _limit(o), f"{o.vwap:.4f}", usd(o.premium), usd(o.fees), usd(o.total_cost),
            f"{o.quantity / o.depth_available:.0%}" if o.depth_available else "-",
        ])
    body = table(
        ["#", "Venue", "Market", "Payoff", "Qty", "Limit (BUY)", "VWAP", "Premium", "Fees", "Total", "Book used"],
        rows, "rllllrrrrrr",
    )
    return (
        f"{body}\n{THIN}\n"
        f"  Premium {usd(r.hedge_premium)}  +  Fees {usd(r.hedge_fees)}  =  "
        f"TOTAL HEDGE COST (K_hedge) {usd(r.hedge_cost)}"
    )


def audit_table(r: HedgeResult, full: bool = False) -> str:
    rows = []
    for a in r.audit:
        if not (full or a.on_grid):
            continue
        rows.append([
            f"${a.diesel:.3f}" + ("" if a.on_grid else "*"),
            pct(a.carrier_surcharge), pct(a.effective_surcharge), pct(a.charged_surcharge),
            usd(a.liability), usd(a.hedge_payout), usd(a.net_pnl), "PASS" if a.passed else "FAIL",
        ])
    return table(
        ["Diesel", "Carrier FSC", "Eff. FSC", "Shipper pays", "Liability", "Hedge payout", "Net P&L", "Check"],
        rows,
    )


def audit_summary(r: HedgeResult) -> str:
    n = len(r.audit)
    n_grid = sum(a.on_grid for a in r.audit)
    fails = [a for a in r.audit if not a.passed]
    mark = "PASSED" if r.all_passed else "FAILED"
    lines = [
        f"  Audit {mark}: {n - len(fails)}/{n} scenarios ({n_grid} grid points + {n - n_grid} "
        f"breakpoints*) have Net P&L >= {usd(r.profile.min_profit_dollars)}.",
        f"  Minimum Net P&L {usd(r.min_net_pnl)} at diesel ${r.worst_scenario:.3f}.",
    ]
    covered = [a for a in r.audit if a.liability > 0]
    if covered:
        t = min(covered, key=lambda a: a.hedge_payout - a.liability)
        lines.append(
            f"  Tightest covered scenario: ${t.diesel:.3f} - payout {usd(t.hedge_payout)} vs liability "
            f"{usd(t.liability)} (cushion {usd(t.hedge_payout - t.liability)})."
        )
    if r.coverage_ceiling is None:
        lines.append(f"  Hedge payout stays >= liability for every diesel price up to ${r.tail_scan_max:.2f}.")
    if fails:
        lines.append(f"  FAILURES at: {', '.join(f'${a.diesel:.3f}' for a in fails[:10])}")
    return "\n".join(lines)


def write_audit_csv(r: HedgeResult, path: str | Path) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["diesel", "on_grid", "carrier_surcharge", "effective_surcharge",
                    "charged_surcharge", "liability", "hedge_payout", "net_pnl", "passed"])
        for a in r.audit:
            w.writerow([f"{a.diesel:.6f}", a.on_grid, f"{a.carrier_surcharge:.6f}",
                        f"{a.effective_surcharge:.6f}", f"{a.charged_surcharge:.6f}",
                        f"{a.liability:.2f}", f"{a.hedge_payout:.2f}", f"{a.net_pnl:.2f}", a.passed])


def infeasibility(rep: InfeasibilityReport, max_rows: int = 12) -> str:
    out = [rep.summary, "", "  Uncoverable scenarios (all contracts bought):"]
    rows = [[f"${s.diesel:.3f}", usd(s.liability), usd(s.max_hedge_payout), usd(s.shortfall)]
            for s in rep.shortfalls[:max_rows]]
    out.append(table(["Diesel", "Liability", "Max payout", "Shortfall"], rows))
    if len(rep.shortfalls) > max_rows:
        out.append(f"  ... {len(rep.shortfalls) - max_rows} more")
    if rep.exhausted:
        out += ["", "  Liquidity exhausted at the worst scenario (100% of the visible book is consumed):"]
        rows = [[e.venue.value, e.market_id, e.contract, str(e.levels), f"{e.depth:,}",
                 usd(e.payout_at_worst), f"+{e.additional_needed:,}"] for e in rep.exhausted]
        out.append(table(["Venue", "Market", "Payoff", "Levels", "Depth", "Payout/contract",
                          "Extra needed (this market alone)"], rows, "lllrrrr"))
    return "\n".join(out)


def cap_menu(quotes: list[CapQuote], current_cap: float | None = None) -> str:
    rows = []
    for q in quotes:
        tag = " <" if current_cap is not None and abs(q.cap - current_cap) < 1e-9 else ""
        if q.feasible:
            load = (
                f"{q.fee_dollars / q.expected_claims:.1f}x"
                if q.expected_claims and q.expected_claims > 0.005 else "-"
            )
            rows.append([pct(q.cap) + tag, usd(q.hedge_cost), usd(q.fee_dollars), f"{q.fee_pct:.3f}%",
                         f"{q.all_in_max_pct:.2f}%",
                         f"{q.prob_binds:.1%}" if q.prob_binds is not None else "-",
                         usd(q.expected_claims) if q.expected_claims is not None else "-", load])
        else:
            rows.append([pct(q.cap) + tag, "INFEASIBLE", "-", "-", "-",
                         f"{q.prob_binds:.1%}" if q.prob_binds is not None else "-",
                         usd(q.expected_claims) if q.expected_claims is not None else "-", "-"])
    return table(
        ["Cap", "Hedge cost", "Fee $", "Fee % V", "All-in max", "P(cap binds)", "E[claims]", "Fee/E[claims]"],
        rows,
    )
