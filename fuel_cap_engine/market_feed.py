"""Market data clients for the hedge instruments.

* :class:`KalshiClient` - Kalshi diesel event contracts (series ``KXDIESEL``). It talks
  to Kalshi's public Trade API v2 (``GET /markets``, ``GET /markets/{ticker}/orderbook``)
  through a pluggable transport. :class:`HttpKalshiTransport` hits the real API (no
  auth needed for market data); :class:`MockKalshiTransport` returns synthetic JSON in
  the same shape so the parsing path is identical in tests and demos.
* :class:`CMEOptionChainSimulator` - NY Harbor ULSD (HO) call option chain with
  Black-76 pricing, call skew, $0.05/$0.10 strike intervals, tick-rounded bid/ask
  spreads and order-book depth. Swap in a real feed (CME DataMine, Databento, broker
  API) by producing the same :class:`OptionQuote` list.

Every order-book *level* becomes its own :class:`~fuel_cap_engine.schemas.Instrument`
so the optimizer prices depth exactly (deeper levels cost more).
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import numpy as np
from pydantic import BaseModel, Field
from scipy.special import ndtr

from .config import EngineSettings
from .schemas import Instrument, InstrumentType, Venue

log = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


# ======================================================================================
# Kalshi
# ======================================================================================
class KalshiTransport(Protocol):
    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


class HttpKalshiTransport:
    """Minimal stdlib HTTP transport for Kalshi's public market-data endpoints."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "fuel-cap-engine/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise MarketDataError(f"Kalshi GET {path} failed: {exc}") from exc


def _black76_call(F: np.ndarray, K: np.ndarray, vol: np.ndarray, T: float, r: float) -> np.ndarray:
    sd = vol * math.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd**2) / sd
    d2 = d1 - sd
    return math.exp(-r * T) * (F * ndtr(d1) - K * ndtr(d2))


def _prob_above(F: float, K: np.ndarray, vol: float, T: float) -> np.ndarray:
    sd = vol * math.sqrt(T)
    d2 = (np.log(F / K) - 0.5 * sd**2) / sd
    return ndtr(d2)


class MockKalshiTransport:
    """Synthetic Kalshi API: "Diesel above $K" markets with a multi-level order book.

    Fair YES probability comes from a lognormal around the current EIA price; asks sit
    1-2c over fair, deeper levels 1c apart, depth peaks near the money.
    """

    def __init__(
        self,
        settings: EngineSettings,
        strikes: list[float] | None = None,
        seed: int | None = None,
        n_levels: int = 3,
    ) -> None:
        self.s = settings
        self.rng = np.random.default_rng(settings.mock_seed if seed is None else seed)
        self.strikes = strikes or [round(3.40 + 0.10 * k, 2) for k in range(17)]  # 3.40..5.00
        close = date.today() + timedelta(days=settings.horizon_days)
        self.event = f"{settings.kalshi_series_ticker}-{close.strftime('%y%b%d').upper()}"
        self.close_time = datetime(close.year, close.month, close.day, 21, tzinfo=timezone.utc)
        T = settings.horizon_days / 365.0
        F = settings.eia_diesel_spot
        fair = _prob_above(F, np.array(self.strikes), settings.retail_annual_vol, T)
        self.books: dict[str, list[tuple[int, int]]] = {}
        self.markets: list[dict[str, Any]] = []
        for K, p in zip(self.strikes, fair):
            ticker = f"{self.event}-T{K:.2f}"
            ask0 = int(min(99, max(1, math.ceil((p + 0.01 + 0.01 * self.rng.random()) * 100))))
            base = 1_500 + 18_000 * math.exp(-(((K - F) / 0.35) ** 2))
            levels: list[tuple[int, int]] = []
            for j in range(n_levels):
                ask = ask0 + j
                if ask > 99:
                    break
                qty = int(base * (1 + 0.6 * j) * self.rng.uniform(0.7, 1.3))
                levels.append((100 - ask, qty))  # stored as NO bids (cents, qty)
            self.books[ticker] = levels
            self.markets.append({
                "ticker": ticker,
                "event_ticker": self.event,
                "title": f"Diesel above ${K:.2f}?",
                "status": "active",
                "strike_type": "greater",
                "floor_strike": K,
                "close_time": self.close_time.isoformat().replace("+00:00", "Z"),
                "yes_ask": ask0,
                "rules_primary": "Resolves YES if the settlement diesel price is above the strike.",
            })

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/markets":
            return {"markets": self.markets, "cursor": ""}
        if path.startswith("/markets/") and path.endswith("/orderbook"):
            ticker = path.split("/")[2]
            if ticker not in self.books:
                raise MarketDataError(f"unknown ticker {ticker}")
            no = sorted(self.books[ticker])  # Kalshi returns ascending price
            return {"orderbook": {
                "yes": [],
                "no": [[p, q] for p, q in no],
                "no_dollars": [[f"{p / 100:.4f}", q] for p, q in no],
            }}
        raise MarketDataError(f"mock transport: unsupported path {path}")


class KalshiClient:
    def __init__(self, transport: KalshiTransport, settings: EngineSettings) -> None:
        self.t = transport
        self.s = settings

    def list_markets(self, status: str = "open") -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):  # hard pagination cap
            data = self.t.get_json("/markets", {
                "series_ticker": self.s.kalshi_series_ticker, "status": status,
                "limit": 200, "cursor": cursor,
            })
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return markets

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self.t.get_json(f"/markets/{ticker}/orderbook",
                               {"depth": self.s.kalshi_max_book_levels})

    @staticmethod
    def parse_yes_asks(payload: dict[str, Any]) -> list[tuple[float, int]]:
        """YES asks (price $, qty) ascending, derived from the NO bid ladder.

        Kalshi books only show bids. Buying YES at price p fills against a NO bid at
        (1 - p). Handles both the cents format (``no``) and dollar-string format
        (``no_dollars``), including the ``orderbook_fp`` envelope.
        """
        book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
        levels: list[tuple[float, int]] = []
        if book.get("no_dollars"):
            for price, qty in book["no_dollars"]:
                levels.append((round(1.0 - float(price), 4), int(float(qty))))
        elif book.get("no"):
            for price, qty in book["no"]:
                levels.append((round(1.0 - float(price) / 100.0, 4), int(float(qty))))
        return sorted((p, q) for p, q in levels if 0.0 < p < 1.0 and q > 0)

    @staticmethod
    def contract_spec(market: dict[str, Any]) -> tuple[InstrumentType, float, float | None, bool] | None:
        st = (market.get("strike_type") or "").lower()
        floor, cap = market.get("floor_strike"), market.get("cap_strike")
        if st in ("greater", "greater_or_equal") and floor is not None:
            return InstrumentType.BINARY_ABOVE, float(floor), None, st == "greater"
        if st == "between" and floor is not None and cap is not None:
            return InstrumentType.BINARY_RANGE, float(floor), float(cap), False
        return None  # "less"-type and custom markets do not hedge upside risk

    def fetch_instruments(self) -> list[Instrument]:
        out: list[Instrument] = []
        for m in self.list_markets():
            spec = self.contract_spec(m)
            if spec is None or m.get("status") not in (None, "active", "open"):
                continue
            itype, k, upper, strict = spec
            for lvl, (price, qty) in enumerate(
                self.parse_yes_asks(self.get_orderbook(m["ticker"]))[: self.s.kalshi_max_book_levels]
            ):
                out.append(Instrument(
                    instrument_id=f"{m['ticker']}@{price:.2f}",
                    market_id=m["ticker"], venue=Venue.KALSHI, type=itype,
                    strike=k, upper_strike=upper, strict=strict,
                    quote=price, multiplier=1.0,
                    fee_per_contract=self.s.kalshi_taker_fee_rate * price * (1 - price),
                    depth=qty, book_level=lvl,
                    index_basis=self.s.kalshi_index_basis,
                    basis_buffer=self.s.kalshi_basis_buffer,
                    expiry=m.get("close_time"),
                    settlement_index=f"Kalshi {self.s.kalshi_series_ticker}",
                ))
        log.info("Kalshi: %d instruments (book levels) loaded", len(out))
        return out


# ======================================================================================
# CME NY Harbor ULSD (HO) options
# ======================================================================================
class OptionQuote(BaseModel):
    strike: float
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    mid: float
    implied_vol: float
    delta: float


class CMEOptionChainSimulator:
    """Realistic HO call chain around the front-month futures price."""

    TICK = 0.0001  # $/gal ($4.20 per contract)
    MIN_ASK = 0.0010  # $/gal floor for deep OTM offers

    def __init__(self, settings: EngineSettings, seed: int | None = None) -> None:
        self.s = settings
        self.rng = np.random.default_rng((settings.mock_seed if seed is None else seed) + 1)

    def strikes(self) -> list[float]:
        F, s = self.s.ho_futures_price, self.s
        fine, coarse = s.ho_strike_fine_interval, s.ho_strike_coarse_interval
        k = math.floor((F - s.ho_strike_max_itm) / fine) * fine
        out: list[float] = []
        while k <= F + s.ho_fine_band + 1e-9:
            out.append(round(k, 2))
            k += fine
        k = math.ceil((out[-1] + 1e-9) / coarse) * coarse
        while k <= F + s.ho_strike_max_otm + 1e-9:
            out.append(round(k, 2))
            k += coarse
        return sorted(set(out))

    def chain(self) -> list[OptionQuote]:
        s = self.s
        F, T, r = s.ho_futures_price, s.horizon_days / 365.0, s.risk_free_rate
        K = np.array(self.strikes())
        iv = s.diesel_annual_vol * (1 + s.ho_call_skew * np.log(K / F))
        iv = np.maximum(iv, 0.5 * s.diesel_annual_vol)
        mid = _black76_call(F, K, iv, T, r)
        sd = iv * math.sqrt(T)
        delta = math.exp(-r * T) * ndtr((np.log(F / K) + 0.5 * sd**2) / sd)
        quotes: list[OptionQuote] = []
        for k, m, v, d in zip(K, mid, iv, delta):
            half = max(0.0005, 0.03 * m)
            ask = max(self.MIN_ASK, math.ceil((m + half) / self.TICK) * self.TICK)
            bid = max(0.0, math.floor((m - half) / self.TICK) * self.TICK)
            size = int((10 + 140 * math.exp(-3 * abs(math.log(k / F)))) * self.rng.uniform(0.7, 1.3))
            quotes.append(OptionQuote(
                strike=float(k), bid=round(bid, 4), ask=round(ask, 4),
                bid_size=max(1, int(size * self.rng.uniform(0.6, 1.1))), ask_size=max(1, size),
                mid=round(float(m), 6), implied_vol=round(float(v), 4), delta=round(float(d), 4),
            ))
        return quotes

    def fetch_instruments(self, levels: int = 2) -> list[Instrument]:
        s = self.s
        code = f"{s.ho_contract_code} {s.ho_contract_month}"
        out: list[Instrument] = []
        for q in self.chain():
            market_id = f"{code} C{q.strike:.2f}"
            price, size = q.ask, q.ask_size
            for lvl in range(levels):
                out.append(Instrument(
                    instrument_id=f"{market_id}@{price:.4f}", market_id=market_id,
                    venue=Venue.CME, type=InstrumentType.CALL, strike=q.strike,
                    quote=round(price, 4), multiplier=s.ho_contract_multiplier,
                    fee_per_contract=s.ho_fee_per_contract, depth=size, book_level=lvl,
                    index_basis=s.ho_index_basis, basis_buffer=s.ho_basis_buffer,
                    expiry=s.ho_contract_month, settlement_index="NYMEX HO futures",
                ))
                price = math.ceil((price + max(0.0005, 0.02 * price)) / self.TICK) * self.TICK
                size *= 2
        log.info("CME: %d instruments (%d strikes x %d levels)", len(out), len(out) // levels, levels)
        return out


# ======================================================================================
# Aggregation
# ======================================================================================
class MarketSnapshot(BaseModel):
    instruments: list[Instrument]
    kalshi_source: str
    cme_source: str
    as_of: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    notes: list[str] = Field(default_factory=list)


def load_market(
    settings: EngineSettings,
    live_kalshi: bool = False,
    include_kalshi: bool = True,
    include_cme: bool = True,
    fallback_to_mock: bool = True,
) -> MarketSnapshot:
    instruments: list[Instrument] = []
    notes: list[str] = []
    kalshi_source = cme_source = "disabled"

    if include_kalshi:
        if live_kalshi:
            try:
                client = KalshiClient(
                    HttpKalshiTransport(settings.kalshi_base_url, settings.kalshi_http_timeout), settings
                )
                live = client.fetch_instruments()
                if not live:
                    raise MarketDataError(
                        f"no open upside markets for series {settings.kalshi_series_ticker}"
                    )
                instruments += live
                kalshi_source = "live"
            except MarketDataError as exc:
                if not fallback_to_mock:
                    raise
                log.warning("Live Kalshi feed unavailable (%s); falling back to mock book", exc)
                notes.append(f"Kalshi live feed failed: {exc}. Using mock order book.")
                live_kalshi = False
        if not live_kalshi:
            instruments += KalshiClient(MockKalshiTransport(settings), settings).fetch_instruments()
            kalshi_source = "mock"

    if include_cme:
        instruments += CMEOptionChainSimulator(settings).fetch_instruments()
        cme_source = "simulated"

    return MarketSnapshot(instruments=instruments, kalshi_source=kalshi_source,
                          cme_source=cme_source, notes=notes)
