"""Implied holdings -> tradable, equal-weight target portfolio.

A member's *implied current holdings* = tickers they have net-bought over the
trailing window and not (per disclosures) fully sold back out. We can't see real
positions, only filings, so this is a reconstruction (documented caveat).

The target is equal-weight across the survivors, after:
  - filtering to Alpaca-tradable assets,
  - capping at ``max_positions`` (keep the largest net-bought names).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Callable

from .disclosures import Trade


def implied_holdings(
    trades: list[Trade],
    asof: date,
    *,
    window_days: int = 365,
) -> dict[str, float]:
    """Net-bought dollar exposure per ticker over the window (positives only).

    net = sum(buy mids) - sum(sell mids). Tickers net <= 0 are considered exited.
    """
    cutoff = asof - timedelta(days=window_days)
    net: dict[str, float] = defaultdict(float)
    for t in trades:
        if not (cutoff <= t.disclosure_date <= asof):
            continue
        if t.side == "buy":
            net[t.ticker] += t.amount_mid
        else:
            net[t.ticker] -= t.amount_mid
    return {tkr: v for tkr, v in net.items() if v > 0}


def target_weights(
    trades: list[Trade],
    asof: date,
    *,
    tradable: Callable[[str], bool],
    max_positions: int,
    window_days: int = 365,
) -> dict[str, float]:
    """Equal-weight target over tradable implied holdings, capped at max_positions.

    Returns ``{ticker: weight}`` summing to 1.0 (empty if nothing qualifies).
    """
    holdings = implied_holdings(trades, asof, window_days=window_days)
    eligible = {tkr: v for tkr, v in holdings.items() if tradable(tkr)}
    if not eligible:
        return {}
    # Keep the largest net-bought names, capped.
    ranked = sorted(eligible.items(), key=lambda kv: kv[1], reverse=True)
    kept = [tkr for tkr, _ in ranked[:max_positions]]
    weight = 1.0 / len(kept)
    return {tkr: weight for tkr in kept}
