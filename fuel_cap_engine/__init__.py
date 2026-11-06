"""Fuel Surcharge Cap engine: price a guaranteed fuel-surcharge cap for parcel shippers
and solve the minimum-cost zero-loss hedge with MILP (HiGHS)."""

from .carrier_grid import CarrierFuelTable, FuelBand, default_tables, ups_air_baseline, ups_ground_baseline
from .config import EngineSettings
from .optimizer import HedgeOptimizer, HedgeSolverError, InfeasibleHedgeError
from .schemas import HedgeResult, Instrument, InstrumentType, ShipperProfile, Venue

__all__ = [
    "CarrierFuelTable", "FuelBand", "default_tables", "ups_air_baseline", "ups_ground_baseline",
    "EngineSettings", "HedgeOptimizer", "HedgeSolverError", "InfeasibleHedgeError",
    "HedgeResult", "Instrument", "InstrumentType", "ShipperProfile", "Venue",
]
__version__ = "1.0.0"
