"""Price access via Alpaca market data.

We deliberately get prices from Alpaca (already authenticated, generous data
limits) rather than spending the tiny FMP free-tier call budget on quotes.

Two needs:
  - historical daily close on/around a date (backtest fills + marking),
  - latest price (position sizing / marking to market).

The :class:`PriceProvider` protocol lets ranking/portfolio code stay testable
with an in-memory fake; :class:`AlpacaPriceProvider` is the live implementation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol

import pandas as pd


class PriceProvider(Protocol):
    def close_on_or_before(self, ticker: str, day: date) -> float | None:
        """Most recent daily close at or before ``day`` (None if unknown)."""

    def latest_price(self, ticker: str) -> float | None:
        """Latest available trade/close price (None if unknown)."""


class AlpacaPriceProvider:
    """Live provider backed by alpaca-py StockHistoricalDataClient.

    Caches per-ticker daily-bar DataFrames so a backtest over many trades for one
    ticker only fetches once.
    """

    def __init__(self, api_key: str, secret_key: str):
        # Imported lazily so unit tests don't require alpaca-py installed.
        from alpaca.data.historical import StockHistoricalDataClient

        self._client = StockHistoricalDataClient(api_key, secret_key)
        self._bars_cache: dict[str, pd.Series] = {}

    def _daily_closes(self, ticker: str) -> pd.Series:
        if ticker in self._bars_cache:
            return self._bars_cache[ticker]
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=800),
        )
        try:
            bars = self._client.get_stock_bars(req).df
        except Exception:
            self._bars_cache[ticker] = pd.Series(dtype=float)
            return self._bars_cache[ticker]
        if bars is None or bars.empty:
            closes = pd.Series(dtype=float)
        else:
            # Multi-index (symbol, timestamp) when one symbol requested.
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(ticker, level=0)
            closes = bars["close"].copy()
            closes.index = pd.to_datetime(closes.index).date
        self._bars_cache[ticker] = closes
        return closes

    def close_on_or_before(self, ticker: str, day: date) -> float | None:
        closes = self._daily_closes(ticker)
        if closes.empty:
            return None
        eligible = closes[[d <= day for d in closes.index]]
        if eligible.empty:
            return None
        return float(eligible.iloc[-1])

    def latest_price(self, ticker: str) -> float | None:
        closes = self._daily_closes(ticker)
        if closes.empty:
            return None
        return float(closes.iloc[-1])


class DictPriceProvider:
    """In-memory provider for tests/backfills.

    ``series`` maps ticker -> {date: close}. ``latest`` maps ticker -> price.
    """

    def __init__(
        self,
        series: dict[str, dict[date, float]],
        latest: dict[str, float] | None = None,
    ):
        self._series = series
        self._latest = latest or {}

    def close_on_or_before(self, ticker: str, day: date) -> float | None:
        s = self._series.get(ticker)
        if not s:
            return None
        eligible = [d for d in s if d <= day]
        if not eligible:
            return None
        return s[max(eligible)]

    def latest_price(self, ticker: str) -> float | None:
        if ticker in self._latest:
            return self._latest[ticker]
        s = self._series.get(ticker)
        if not s:
            return None
        return s[max(s)]
