"""Fetch + normalize congressional trade disclosures.

Primary source: Financial Modeling Prep (FMP) — covers both Senate and House.
Fallback: Senate Stock Watcher (SSW) JSON (no key; Senate only). House Stock
Watcher is dead / 403 as of early 2026, so there is no House fallback.

Everything is normalized to the :class:`Trade` dataclass. Rows we cannot map to a
plausible US-equity ticker (options, bonds, "--", non-US instruments) are dropped
here so downstream modules only ever see tradable-ish equities.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import httpx

FMP_SENATE_URL = "https://financialmodelingprep.com/api/v4/senate-trading-rss-feed"
FMP_HOUSE_URL = "https://financialmodelingprep.com/api/v4/senate-disclosure-rss-feed"
# Senate Stock Watcher aggregated JSON (free, no key).
SSW_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/"
    "aggregate/all_transactions.json"
)

# Range strings -> (low, high) USD. STOCK Act discloses dollar *ranges*.
_AMOUNT_RANGE_RE = re.compile(r"\$?\s*([\d,]+)\s*(?:-|–|to)\s*\$?\s*([\d,]+)")
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

Side = str  # "buy" | "sell"


@dataclass(frozen=True)
class Trade:
    member: str
    chamber: str  # "senate" | "house"
    ticker: str
    txn_date: date
    disclosure_date: date
    side: Side  # "buy" | "sell"
    amount_low: float
    amount_high: float

    @property
    def amount_mid(self) -> float:
        return (self.amount_low + self.amount_high) / 2.0

    def dedup_key(self) -> str:
        """Stable key for SQLite dedup (filing/txn identity)."""
        return "|".join(
            [
                self.member.lower(),
                self.chamber,
                self.ticker,
                self.txn_date.isoformat(),
                self.disclosure_date.isoformat(),
                self.side,
                f"{self.amount_low:.0f}",
                f"{self.amount_high:.0f}",
            ]
        )


# ──────────────────────────────────────────────────────────────────────────
# Parsing helpers (pure functions — unit-tested)
# ──────────────────────────────────────────────────────────────────────────
def normalize_ticker(raw: Any) -> str | None:
    """Return an uppercase US-equity ticker, or ``None`` if not resolvable.

    Drops options/bonds/empties/non-US: anything that isn't 1-5 plain letters.
    Strips common exchange suffixes like ``AAPL:US`` / ``AAPL.O``.
    """
    if raw is None:
        return None
    t = str(raw).strip().upper()
    if not t or t in {"--", "N/A", "NONE", "-"}:
        return None
    # Strip "AAPL:US" / "AAPL.O" exchange/class suffixes.
    t = re.split(r"[:.]", t)[0]
    # Strip a trailing exchange/country code ("AAPL US"), but reject multi-word
    # junk ("TOO LONG NAME") and option strings ("AAPL 250117C00150000").
    parts = t.split()
    if len(parts) == 2 and re.fullmatch(r"[A-Z]{1,2}", parts[1]):
        t = parts[0]
    elif len(parts) > 1:
        return None
    if not _TICKER_RE.match(t):
        return None
    return t


def parse_amount_range(raw: Any) -> tuple[float, float] | None:
    """Parse a STOCK Act dollar-range string into (low, high)."""
    if raw is None:
        return None
    s = str(raw)
    m = _AMOUNT_RANGE_RE.search(s)
    if not m:
        # Single value like "$15,000"
        single = re.search(r"\$?\s*([\d,]+)", s)
        if single:
            v = float(single.group(1).replace(",", ""))
            return (v, v)
        return None
    low = float(m.group(1).replace(",", ""))
    high = float(m.group(2).replace(",", ""))
    if high < low:
        low, high = high, low
    return (low, high)


def normalize_side(raw: Any) -> str | None:
    """Map a disclosure 'type' to 'buy' or 'sell' (None for exchanges/other)."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s.startswith("purchase") or s in {"buy", "p"}:
        return "buy"
    if "sale" in s or s in {"sell", "s"} or s.startswith("sold"):
        return "sell"
    return None


def _parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    # ISO with timezone / extra precision.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────────────
# Record normalization (one raw dict -> Trade | None)
# ──────────────────────────────────────────────────────────────────────────
def _fmp_record_to_trade(rec: dict, chamber: str) -> Trade | None:
    member = (
        rec.get("representative")
        or rec.get("senator")
        or rec.get("office")
        or rec.get("name")
        or ""
    ).strip()
    ticker = normalize_ticker(rec.get("ticker") or rec.get("symbol"))
    side = normalize_side(rec.get("type") or rec.get("transactionType"))
    txn_date = _parse_date(rec.get("transactionDate") or rec.get("date"))
    disc_date = _parse_date(rec.get("disclosureDate") or rec.get("dateRecieved"))
    amounts = parse_amount_range(rec.get("amount"))
    if not (member and ticker and side and txn_date and amounts):
        return None
    disc_date = disc_date or txn_date
    return Trade(
        member=member,
        chamber=chamber,
        ticker=ticker,
        txn_date=txn_date,
        disclosure_date=disc_date,
        side=side,
        amount_low=amounts[0],
        amount_high=amounts[1],
    )


def _ssw_record_to_trade(rec: dict) -> Trade | None:
    member = (rec.get("senator") or "").strip()
    ticker = normalize_ticker(rec.get("ticker"))
    side = normalize_side(rec.get("type"))
    txn_date = _parse_date(rec.get("transaction_date"))
    disc_date = _parse_date(rec.get("disclosure_date"))
    amounts = parse_amount_range(rec.get("amount"))
    if not (member and ticker and side and txn_date and amounts):
        return None
    disc_date = disc_date or txn_date
    return Trade(
        member=member,
        chamber="senate",
        ticker=ticker,
        txn_date=txn_date,
        disclosure_date=disc_date,
        side=side,
        amount_low=amounts[0],
        amount_high=amounts[1],
    )


def normalize_records(
    records: Iterable[dict], *, source: str, chamber: str = "senate"
) -> list[Trade]:
    """Normalize an iterable of raw dicts to Trades, dropping unmappable rows."""
    to_trade = _ssw_record_to_trade if source == "ssw" else None
    out: list[Trade] = []
    for rec in records:
        trade = (
            _ssw_record_to_trade(rec)
            if source == "ssw"
            else _fmp_record_to_trade(rec, chamber)
        )
        if trade is not None:
            out.append(trade)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Network fetch
# ──────────────────────────────────────────────────────────────────────────
def _fmp_get(url: str, api_key: str, *, client: httpx.Client) -> list[dict]:
    resp = client.get(url, params={"apikey": api_key}, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_recent_trades(
    lookback_days: int,
    *,
    fmp_api_key: str,
    today: date | None = None,
    client: httpx.Client | None = None,
) -> list[Trade]:
    """Fetch + normalize recent congressional trades.

    Tries FMP (Senate + House) first. On any error / empty result, falls back to
    Senate Stock Watcher. Returns trades whose ``disclosure_date`` is within the
    lookback window (covers the ~45-day STOCK Act reporting lag).
    """
    today = today or date.today()
    cutoff = today - timedelta(days=lookback_days)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        trades: list[Trade] = []
        if fmp_api_key:
            try:
                senate_raw = _fmp_get(FMP_SENATE_URL, fmp_api_key, client=client)
                house_raw = _fmp_get(FMP_HOUSE_URL, fmp_api_key, client=client)
                trades = normalize_records(
                    senate_raw, source="fmp", chamber="senate"
                ) + normalize_records(house_raw, source="fmp", chamber="house")
            except (httpx.HTTPError, ValueError):
                trades = []
        if not trades:
            # Fallback: Senate Stock Watcher (no key, Senate only).
            resp = client.get(SSW_URL, timeout=60.0)
            resp.raise_for_status()
            trades = normalize_records(resp.json(), source="ssw")
        return [t for t in trades if t.disclosure_date >= cutoff]
    finally:
        if owns_client:
            client.close()
