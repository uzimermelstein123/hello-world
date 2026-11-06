"""Carrier fuel surcharge tables indexed to an energy price.

UPS (and FedEx) publish weekly fuel surcharge tables: the surcharge percentage applied
to transportation charges is looked up from a band table keyed on an index price
(EIA retail on-highway diesel for Ground; U.S. Gulf Coast jet fuel for Air). Above the
top of the published table the surcharge keeps stepping by a fixed increment.

The baseline tables shipped here are ILLUSTRATIVE: they have the right structure and a
realistic slope, but band edges and percentages change every time the carrier revises
its table. Load the current published table with :meth:`CarrierFuelTable.from_csv`
before quoting a real customer.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Mapping
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import PRICE_DECIMALS, round_price


class FuelBand(BaseModel):
    """Surcharge applies when ``lower <= index < upper``."""

    model_config = ConfigDict(frozen=True)

    lower: float
    upper: float
    surcharge: float = Field(ge=0, description="fraction, e.g. 0.1750 for 17.50%")

    @model_validator(mode="after")
    def _width(self) -> "FuelBand":
        if self.upper <= self.lower:
            raise ValueError(f"band upper ({self.upper}) must exceed lower ({self.lower})")
        return self


class CarrierFuelTable(BaseModel):
    model_config = ConfigDict(frozen=True)

    carrier: str = "UPS"
    service: str
    index_name: str
    # table index = carrier-index (EIA diesel) scenario + index_offset
    index_offset: float = 0.0
    bands: list[FuelBand]
    below_table_surcharge: float | None = None  # default: first band's surcharge
    above_table_step_width: float = Field(gt=0)
    above_table_step_surcharge: float = Field(ge=0)
    effective_date: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _contiguous(self) -> "CarrierFuelTable":
        if not self.bands:
            raise ValueError("table needs at least one band")
        for a, b in zip(self.bands, self.bands[1:]):
            if abs(a.upper - b.lower) > 1e-9:
                raise ValueError(f"bands not contiguous/sorted at {a.upper} -> {b.lower}")
        return self

    # ---------------------------------------------------------------- lookup -------
    def surcharge_at(self, diesel: np.ndarray | float) -> np.ndarray:
        """Nominal (undiscounted) surcharge fraction for carrier-index price(s)."""
        idx = round_price(np.atleast_1d(np.asarray(diesel, dtype=float)) + self.index_offset)
        lowers = np.array([round(b.lower, PRICE_DECIMALS) for b in self.bands])
        rates = np.array([b.surcharge for b in self.bands])
        top = round(self.bands[-1].upper, PRICE_DECIMALS)

        pos = np.searchsorted(lowers, idx, side="right") - 1
        below = self.below_table_surcharge if self.below_table_surcharge is not None else rates[0]
        out = np.where(pos >= 0, rates[np.clip(pos, 0, None)], below)

        above = idx >= top
        if np.any(above):
            steps = np.floor(np.round((idx - top) / self.above_table_step_width, 9)) + 1
            out = np.where(above, rates[-1] + steps * self.above_table_step_surcharge, out)
        return np.round(out, 10)

    def breakpoints(self, lo: float, hi: float) -> np.ndarray:
        """Carrier-index prices in [lo, hi] at which the surcharge steps up."""
        edges = [b.lower for b in self.bands] + [self.bands[-1].upper]
        top = self.bands[-1].upper
        hi_idx = hi + self.index_offset
        k = 1
        while top + k * self.above_table_step_width <= hi_idx + 1e-9:
            edges.append(top + k * self.above_table_step_width)
            k += 1
        pts = round_price(np.array(edges) - self.index_offset)
        return np.unique(pts[(pts >= lo - 1e-9) & (pts <= hi + 1e-9)])

    # ---------------------------------------------------------- adjustments ------
    def shifted(self, index_shift: float) -> "CarrierFuelTable":
        """Basis shift: move every threshold by ``index_shift`` $/gal.

        A positive shift means the carrier needs a *higher* index price to reach each
        surcharge level (a more favorable table for the shipper).
        """
        bands = [
            FuelBand(lower=round(b.lower + index_shift, PRICE_DECIMALS),
                     upper=round(b.upper + index_shift, PRICE_DECIMALS),
                     surcharge=b.surcharge)
            for b in self.bands
        ]
        return self.model_copy(update={"bands": bands})

    def adjusted(
        self,
        add: float = 0.0,
        mult: float = 1.0,
        overrides: Mapping[int, float] | None = None,
        fn: Callable[[FuelBand], FuelBand] | None = None,
    ) -> "CarrierFuelTable":
        """Matrix adjustment of surcharge levels.

        new = surcharge * mult + add, then per-band ``overrides`` {band_index: surcharge},
        then an arbitrary ``fn`` applied band-by-band.
        """
        overrides = dict(overrides or {})
        bands: list[FuelBand] = []
        for i, b in enumerate(self.bands):
            s = overrides.get(i, b.surcharge * mult + add)
            nb = FuelBand(lower=b.lower, upper=b.upper, surcharge=round(max(0.0, s), 10))
            bands.append(fn(nb) if fn else nb)
        return self.model_copy(update={
            "bands": bands,
            "above_table_step_surcharge": self.above_table_step_surcharge * mult,
            "below_table_surcharge": (
                None if self.below_table_surcharge is None
                else max(0.0, self.below_table_surcharge * mult + add)
            ),
        })

    def with_index_offset(self, offset: float) -> "CarrierFuelTable":
        return self.model_copy(update={"index_offset": offset})

    # ------------------------------------------------------------------- I/O ------
    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        service: str,
        index_name: str = "EIA_US_DIESEL_RETAIL",
        carrier: str = "UPS",
        index_offset: float = 0.0,
        above_table_step_width: float | None = None,
        above_table_step_surcharge: float | None = None,
    ) -> "CarrierFuelTable":
        """Load a table from CSV with columns ``lower,upper,surcharge_pct``.

        ``surcharge_pct`` is in percent (17.5 = 17.50%). If the step-above-table rule is
        not given, it is inferred from the last two bands.
        """
        bands: list[FuelBand] = []
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                bands.append(FuelBand(
                    lower=float(row["lower"]),
                    upper=float(row["upper"]),
                    surcharge=float(row["surcharge_pct"]) / 100.0,
                ))
        bands.sort(key=lambda b: b.lower)
        if above_table_step_width is None:
            above_table_step_width = bands[-1].upper - bands[-1].lower
        if above_table_step_surcharge is None:
            above_table_step_surcharge = (
                bands[-1].surcharge - bands[-2].surcharge if len(bands) > 1 else 0.0
            )
        return cls(
            carrier=carrier, service=service, index_name=index_name,
            index_offset=index_offset, bands=bands,
            above_table_step_width=above_table_step_width,
            above_table_step_surcharge=above_table_step_surcharge,
            note=f"loaded from {path}",
        )

    def to_csv(self, path: str | Path) -> None:
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["lower", "upper", "surcharge_pct"])
            for b in self.bands:
                w.writerow([f"{b.lower:.2f}", f"{b.upper:.2f}", f"{b.surcharge * 100:.2f}"])


def generate_table(
    service: str,
    index_name: str,
    start: float,
    width: float,
    n_bands: int,
    base_surcharge: float,
    step_surcharge: float,
    index_offset: float = 0.0,
    carrier: str = "UPS",
    note: str = "",
) -> CarrierFuelTable:
    """Uniform band table: [start + k*w, start + (k+1)*w) -> base + k*step."""
    bands = [
        FuelBand(
            lower=round(start + k * width, PRICE_DECIMALS),
            upper=round(start + (k + 1) * width, PRICE_DECIMALS),
            surcharge=round(base_surcharge + k * step_surcharge, 10),
        )
        for k in range(n_bands)
    ]
    return CarrierFuelTable(
        carrier=carrier, service=service, index_name=index_name, index_offset=index_offset,
        bands=bands, above_table_step_width=width, above_table_step_surcharge=step_surcharge,
        note=note,
    )


def ups_ground_baseline() -> CarrierFuelTable:
    """Illustrative UPS Ground table keyed on EIA retail on-highway diesel.

    $0.09 bands starting at $3.00 / 15.50%, +0.25% per band, same step above the table.
    (At $3.75 diesel this yields 17.50%.)
    """
    return generate_table(
        service="ground", index_name="EIA_US_DIESEL_RETAIL",
        start=3.00, width=0.09, n_bands=34, base_surcharge=0.1550, step_surcharge=0.0025,
        note="ILLUSTRATIVE baseline - replace with the carrier's current published table",
    )


def ups_air_baseline(jet_index_offset: float = -1.45) -> CarrierFuelTable:
    """Illustrative UPS Domestic Air table.

    UPS Air is keyed on U.S. Gulf Coast kerosene-type jet fuel, not diesel. The engine
    is single-factor (diesel), so jet is approximated as ``diesel + jet_index_offset``.
    Hedging Air with diesel instruments therefore carries cross-commodity basis risk;
    widen the basis buffers if Air is a material share of spend.
    """
    return generate_table(
        service="air", index_name="USGC_JET_FUEL (diesel proxy)",
        start=1.90, width=0.10, n_bands=40, base_surcharge=0.1900, step_surcharge=0.0050,
        index_offset=jet_index_offset,
        note="ILLUSTRATIVE baseline - replace with the carrier's current published table",
    )


def default_tables(air_jet_index_offset: float = -1.45) -> dict[str, CarrierFuelTable]:
    return {"ground": ups_ground_baseline(), "air": ups_air_baseline(air_jet_index_offset)}
