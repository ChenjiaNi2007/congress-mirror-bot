"""Copy-at-disclosure backtest -> member leaderboard.

We rank members by the trailing-12-month return of a portfolio that *copied each
member's disclosed trades at disclosure time*. This is lookahead-free: fills use
the disclosure-date close (the earliest a copier could have acted), and the
result is marked to market at ``asof``.

Documented approximations (this is for-interest, NOT alpha):
  - range-midpoint sizing — STOCK Act discloses dollar *ranges*, we use the mid;
  - disclosed-trades-only — we never see a member's full portfolio, only filings;
  - close-price fills — we assume execution at the disclosure-date daily close;
  - idle-cash bias is minimized by funding the portfolio with the member's own
    total buy volume over the window (so it is ~fully deployed).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from .disclosures import Trade
from .pricing import PriceProvider


@dataclass(frozen=True)
class MemberResult:
    member: str
    chamber: str
    trailing_return: float  # e.g. 0.12 == +12%
    n_trades: int
    n_tickers: int


def backtest_member(
    trades: list[Trade],
    asof: date,
    prices: PriceProvider,
) -> float | None:
    """Trailing return of a copy-at-disclosure portfolio for one member.

    ``trades`` should already be filtered to the window and to one member.
    Returns ``None`` when no trade can be priced.
    """
    # Fund the portfolio with the member's total buy volume so it is ~fully
    # deployed (minimizes idle-cash drag across members of different sizes).
    initial = sum(t.amount_mid for t in trades if t.side == "buy")
    if initial <= 0:
        return None

    cash = initial
    shares: dict[str, float] = defaultdict(float)
    priced_any = False

    for t in sorted(trades, key=lambda x: x.disclosure_date):
        price = prices.close_on_or_before(t.ticker, t.disclosure_date)
        if price is None or price <= 0:
            continue
        priced_any = True
        if t.side == "buy":
            spend = min(t.amount_mid, cash)
            if spend <= 0:
                continue
            shares[t.ticker] += spend / price
            cash -= spend
        else:  # sell
            held_value = shares[t.ticker] * price
            proceeds = min(t.amount_mid, held_value)
            if proceeds <= 0:
                continue
            shares[t.ticker] -= proceeds / price
            cash += proceeds

    if not priced_any:
        return None

    holdings_value = 0.0
    for ticker, qty in shares.items():
        if qty <= 0:
            continue
        mark = prices.latest_price(ticker)
        if mark is None or mark <= 0:
            mark = prices.close_on_or_before(ticker, asof) or 0.0
        holdings_value += qty * mark

    final_value = cash + holdings_value
    return final_value / initial - 1.0


def rank_members(
    trades: list[Trade],
    asof: date,
    prices: PriceProvider,
    *,
    min_trades: int,
    min_tickers: int,
    window_days: int = 365,
) -> list[MemberResult]:
    """Rank active members by trailing-window copy-at-disclosure return (desc)."""
    cutoff = asof - timedelta(days=window_days)
    by_member: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if cutoff <= t.disclosure_date <= asof:
            by_member[t.member].append(t)

    results: list[MemberResult] = []
    for member, mtrades in by_member.items():
        n_trades = len(mtrades)
        n_tickers = len({t.ticker for t in mtrades})
        if n_trades < min_trades or n_tickers < min_tickers:
            continue  # too small a sample — skip noisy members
        ret = backtest_member(mtrades, asof, prices)
        if ret is None:
            continue
        results.append(
            MemberResult(
                member=member,
                chamber=mtrades[0].chamber,
                trailing_return=ret,
                n_trades=n_trades,
                n_tickers=n_tickers,
            )
        )

    results.sort(key=lambda r: r.trailing_return, reverse=True)
    return results
