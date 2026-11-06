"""Pydantic data schemas shared across the engine."""

from __future__ import annotations

import math
from enum import Enum

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PRICE_DECIMALS = 6  # all index prices are rounded to this many decimals before comparison


def round_price(x: np.ndarray | float) -> np.ndarray:
    return np.round(np.asarray(x, dtype=float), PRICE_DECIMALS)


class Venue(str, Enum):
    KALSHI = "KALSHI"
    CME = "CME"


class InstrumentType(str, Enum):
    BINARY_ABOVE = "BINARY_ABOVE"  # pays `multiplier` if hedge index >= K (> K if strict)
    BINARY_RANGE = "BINARY_RANGE"  # pays `multiplier` if K <= hedge index < upper_strike
    CALL = "CALL"  # pays max(0, hedge index - K) * multiplier


class Instrument(BaseModel):
    """One purchasable *price level* of a derivative.

    An order book with several ask levels is represented as several Instruments that
    share a ``market_id``; the optimizer then walks the book exactly (cheapest level
    first is implied by the objective).
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    market_id: str
    venue: Venue
    type: InstrumentType
    strike: float  # K, in hedge-index units ($/gal)
    upper_strike: float | None = None  # only for BINARY_RANGE
    strict: bool = False  # BINARY_ABOVE: True => pays only if index > K
    quote: float = Field(ge=0)  # ask, in quote units ($/contract for binaries, $/gal for HO)
    multiplier: float = Field(gt=0)  # $1 for binaries, 42,000 gal for HO
    fee_per_contract: float = Field(0.0, ge=0)
    depth: int = Field(ge=0)  # L_i: contracts available at this ask
    book_level: int = 0
    index_basis: float = 0.0  # hedge index - carrier index (expected)
    basis_buffer: float = Field(0.0, ge=0)  # conservative haircut on the hedge index
    expiry: str | None = None
    settlement_index: str = ""

    @model_validator(mode="after")
    def _check_range(self) -> "Instrument":
        if self.type is InstrumentType.BINARY_RANGE:
            if self.upper_strike is None or self.upper_strike <= self.strike:
                raise ValueError("BINARY_RANGE requires upper_strike > strike")
        return self

    @property
    def premium_per_contract(self) -> float:
        return self.quote * self.multiplier

    @property
    def unit_cost(self) -> float:
        """c_i used in the MILP objective: premium + fees for one contract."""
        return self.premium_per_contract + self.fee_per_contract

    def hedge_index(self, carrier_index: np.ndarray | float) -> np.ndarray:
        return round_price(np.atleast_1d(np.asarray(carrier_index, dtype=float)) + self.index_basis - self.basis_buffer)

    def payout(self, carrier_index: np.ndarray | float) -> np.ndarray:
        """Payout per contract (in $) for carrier-index scenario price(s)."""
        x = self.hedge_index(carrier_index)
        k = round(self.strike, PRICE_DECIMALS)
        if self.type is InstrumentType.CALL:
            return np.maximum(0.0, x - k) * self.multiplier
        if self.type is InstrumentType.BINARY_ABOVE:
            hit = (x > k) if self.strict else (x >= k)
            return hit.astype(float) * self.multiplier
        upper = round(float(self.upper_strike), PRICE_DECIMALS)  # type: ignore[arg-type]
        return ((x >= k) & (x < upper)).astype(float) * self.multiplier

    def carrier_breakpoints(self) -> list[float]:
        """Carrier-index prices where this instrument's payout changes slope/level."""
        shift = -self.index_basis + self.basis_buffer
        pts = [self.strike + shift]
        if self.upper_strike is not None:
            pts.append(self.upper_strike + shift)
        return [float(round_price(p)) for p in pts]

    def describe(self) -> str:
        if self.type is InstrumentType.CALL:
            return f"{self.strike:.2f} Call"
        if self.type is InstrumentType.BINARY_RANGE:
            return f"{self.strike:.2f}-{self.upper_strike:.2f}"
        return f"{'>' if self.strict else '>='}{self.strike:.2f}"


class ShipperProfile(BaseModel):
    """The shipper being quoted."""

    name: str = "Shipper"
    monthly_spend: float = Field(gt=0, description="V: billable base transportation spend, $/month")
    carrier_fuel_discount: float = Field(ge=0, lt=1)
    promised_cap: float = Field(ge=0, le=1, description="Max effective fuel surcharge (fraction)")
    min_profit_dollars: float = Field(ge=0)
    service_mix: dict[str, float] = Field(default_factory=lambda: {"ground": 1.0})
    service_caps: dict[str, float] = Field(
        default_factory=dict, description="Optional per-service cap overrides (fraction)"
    )

    @field_validator("service_mix")
    @classmethod
    def _mix_sums_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("service_mix cannot be empty")
        if any(w < 0 for w in v.values()):
            raise ValueError("service_mix weights must be >= 0")
        if not math.isclose(sum(v.values()), 1.0, abs_tol=1e-6):
            raise ValueError(f"service_mix weights must sum to 1.0 (got {sum(v.values()):.6f})")
        return v

    def cap_for(self, service: str) -> float:
        return self.service_caps.get(service, self.promised_cap)


class OrderLine(BaseModel):
    """One aggregated order on the trade ticket (all book levels of one market)."""

    market_id: str
    venue: Venue
    type: InstrumentType
    contract: str
    strike: float
    quantity: int
    limit_price: float  # worst ask level used (quote units)
    vwap: float  # quote units
    premium: float  # $
    fees: float  # $
    total_cost: float  # $
    depth_available: int
    levels_used: int


class AuditRow(BaseModel):
    diesel: float
    on_grid: bool
    carrier_surcharge: float  # blended nominal carrier surcharge (fraction)
    effective_surcharge: float  # after negotiated discount
    charged_surcharge: float  # what the shipper actually pays: min(effective, cap)
    liability: float
    hedge_payout: float
    net_pnl: float
    passed: bool


class SolverStats(BaseModel):
    status: int
    message: str
    runtime_sec: float
    mip_node_count: int | None = None
    mip_gap: float | None = None
    n_variables: int
    n_constraints: int


class HedgeResult(BaseModel):
    profile: ShipperProfile
    orders: list[OrderLine]
    quantities: dict[str, int]  # instrument_id -> x_i*
    hedge_premium: float
    hedge_fees: float
    hedge_cost: float  # K_hedge (actual, incl. order-level fee rounding)
    lp_objective: float  # sum c_i x_i as seen by the solver
    fee_dollars: float  # P_$
    fee_pct: float  # P_% (percent, e.g. 1.23 for 1.23%)
    audit: list[AuditRow]
    all_passed: bool
    min_net_pnl: float
    worst_scenario: float
    trigger_price: float | None  # lowest diesel price at which the cap binds
    coverage_ceiling: float | None  # lowest diesel price where payout < liability (None = none up to scan max)
    tail_scan_max: float
    warnings: list[str] = Field(default_factory=list)
    solver: SolverStats | None = None


class ScenarioShortfall(BaseModel):
    diesel: float
    liability: float
    max_hedge_payout: float  # payout if every available contract were bought
    shortfall: float


class ExhaustedInstrument(BaseModel):
    """A market whose whole visible book (all levels) is consumed and still falls short."""

    market_id: str
    venue: Venue
    contract: str
    levels: int
    depth: int  # total contracts across all levels
    payout_at_worst: float  # $ per contract at the worst failing scenario
    additional_needed: int  # extra contracts of this alone that would close the worst gap


class InfeasibilityReport(BaseModel):
    profile: ShipperProfile
    shortfalls: list[ScenarioShortfall]
    exhausted: list[ExhaustedInstrument]
    uncovered_prices: list[float]  # scenarios where *no* instrument pays anything
    highest_paying_breakpoint: float | None = None  # above this, binaries add no more payout
    summary: str
