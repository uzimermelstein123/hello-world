"""Engine configuration: market reference levels, scenario grid, fees, solver knobs.

Every field can be overridden with an environment variable named ``FSC_<FIELD_NAME>``
(upper-case), e.g. ``FSC_EIA_DIESEL_SPOT=3.92``. The CLI can override the most common
ones as flags as well.

Index convention used throughout the engine
-------------------------------------------
The *carrier index* is the U.S. EIA weekly National Average On-Highway (retail) Diesel
price in $/gal. All scenarios are expressed in carrier-index dollars.

Each hedge instrument settles on its own *hedge index*. The engine maps a carrier-index
scenario ``D`` to the hedge index as::

    hedge_index = D + index_basis - basis_buffer

* ``index_basis`` is the expected spread (hedge index minus EIA retail). NY Harbor ULSD
  futures trade roughly $1.00-$1.40 below the EIA retail average (taxes + distribution
  margin), so the default HO basis is negative.
* ``basis_buffer`` (>= 0) is a deliberate haircut: the engine assumes the hedge index
  under-performs the carrier index by this amount, so the zero-loss guarantee survives
  that much basis slippage.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------------------
# Default shipper profile (used by the CLI when flags are omitted)
# --------------------------------------------------------------------------------------
DEFAULT_SHIPPER_NAME = "Enterprise Shipper"
DEFAULT_MONTHLY_SPEND = 2_500_000.00  # $ billable base transportation spend / month
DEFAULT_FUEL_DISCOUNT = 0.30  # 30% negotiated discount on the fuel surcharge
DEFAULT_PROMISED_CAP = 0.13  # 13.00% guaranteed max *effective* fuel surcharge
DEFAULT_MIN_PROFIT = 1_000.00  # $ risk-free margin to the reseller


class EngineSettings(BaseModel):
    """All tunable engine parameters with production-sane defaults."""

    # ---- Scenario grid (carrier index, $/gal) ----------------------------------------
    diesel_min: float = Field(3.50, gt=0)
    diesel_max: float = Field(6.50, gt=0)
    diesel_step: float = Field(0.05, gt=0)
    # Add every carrier-table and instrument breakpoint inside the grid range so the
    # zero-loss guarantee holds for *every* price in [min, max], not just grid points.
    continuous_guarantee: bool = True
    # How far above diesel_max to scan when reporting the hedge "coverage ceiling".
    tail_scan_max: float = Field(10.00, gt=0)

    # ---- Market reference levels ----------------------------------------------------
    eia_diesel_spot: float = Field(3.75, gt=0)  # latest EIA weekly retail diesel
    ho_futures_price: float = Field(2.45, gt=0)  # NY Harbor ULSD front month, $/gal
    # Annualized vol of NY Harbor ULSD futures (wholesale). Retail diesel passes wholesale
    # moves through roughly dollar-for-dollar, so retail % vol = this * HO / retail.
    diesel_annual_vol: float = Field(0.35, gt=0)
    horizon_days: int = Field(30, gt=0)  # days to hedge settlement
    risk_free_rate: float = 0.045

    # ---- Basis model (see module docstring) -----------------------------------------
    ho_index_basis: float = -1.30  # NYH ULSD futures minus EIA retail diesel
    ho_basis_buffer: float = Field(0.05, ge=0)
    kalshi_index_basis: float = 0.0  # Kalshi diesel markets settle on a retail average
    kalshi_basis_buffer: float = Field(0.01, ge=0)
    # UPS Air surcharges are indexed to U.S. Gulf Coast jet fuel, not retail diesel.
    # jet = diesel + air_jet_index_offset (single-factor approximation).
    air_jet_index_offset: float = -1.45

    # ---- CME NY Harbor ULSD options (OH on HO futures) ------------------------------
    ho_contract_multiplier: float = 42_000.0  # gallons per contract
    ho_contract_code: str = "OH"
    ho_contract_month: str = "F7"  # Jan-2027 futures => Dec-2026 option expiry
    ho_fee_per_contract: float = Field(3.50, ge=0)  # exchange + clearing + broker, $
    ho_strike_fine_interval: float = 0.05
    ho_strike_coarse_interval: float = 0.10
    ho_fine_band: float = 0.50  # +/- $ around ATM using the fine interval
    ho_strike_max_otm: float = 2.50  # list strikes up to F + this
    ho_strike_max_itm: float = 0.30  # list strikes down to F - this
    ho_call_skew: float = 0.35  # vol multiplier slope vs ln(K/F)

    # ---- Kalshi ---------------------------------------------------------------------
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_series_ticker: str = "KXDIESEL"
    kalshi_taker_fee_rate: float = Field(0.07, ge=0)  # fee = rate * P * (1 - P)
    kalshi_max_book_levels: int = Field(5, gt=0)
    kalshi_http_timeout: float = Field(10.0, gt=0)

    # ---- Solver ---------------------------------------------------------------------
    solver_time_limit: float = Field(60.0, gt=0)
    solver_mip_rel_gap: float = Field(1e-6, ge=0)
    # Every covered scenario must clear liability by at least this many dollars. It
    # absorbs floating-point slack so the post-solve audit can be exact.
    solver_safety_margin: float = Field(0.01, ge=0)

    # ---- Mock data ------------------------------------------------------------------
    mock_seed: int = 7

    @property
    def retail_annual_vol(self) -> float:
        """% vol of EIA retail diesel implied by dollar-for-dollar pass-through of HO moves."""
        return self.diesel_annual_vol * self.ho_futures_price / self.eia_diesel_spot

    @classmethod
    def from_env(cls, **overrides: Any) -> "EngineSettings":
        """Build settings from defaults <- ``FSC_*`` env vars <- explicit overrides."""
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            raw = os.environ.get(f"FSC_{name.upper()}")
            if raw is not None:
                values[name] = raw  # pydantic coerces str -> float/int/bool
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)
