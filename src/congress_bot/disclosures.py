"""Fetch + normalize congressional trade disclosures.

Data source: Quiver Quantitative (quiverquant.com) congressional trading API.
Free API keys are available at https://www.quiverquant.com/account/signup
Set QUIVERQUANT_API_KEY in .env — the endpoint requires authentication.

Field mapping from Quiver Quant records:
  Representative  -> member
  House           -> chamber ("Representatives" -> "house", "Senate" -> "senate")
  Ticker          -> ticker
  Transaction     -> side ("Purchase" -> "buy", "Sale*" -> "sell")
  Range           -> amount_low / amount_high (STOCK Act dollar range)
  TransactionDate -> txn_date
  ReportDate      -> disclosure_date
  TickerType      -> "ST" for US equities; others are dropped

Everything is normalized to the :class:`Trade` dataclass. Non-equity rows
(TickerType != "ST"), untradable tickers, exchange-listed foreign names, and rows
missing required fields are dropped here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import httpx

QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"

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
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _normalize_chamber(raw: Any) -> str:
    """Map Quiver Quant 'House' field to 'senate' or 'house'."""
    s = str(raw or "").strip().lower()
    if s == "senate":
        return "senate"
    return "house"  # "Representatives" and anything else -> house


# ──────────────────────────────────────────────────────────────────────────
# Record normalization
# ──────────────────────────────────────────────────────────────────────────
def _qv_record_to_trade(rec: dict) -> Trade | None:
    """Normalize one Quiver Quant record to a Trade, or None if unusable."""
    # Drop non-equity rows (options, bonds, foreign shares)
    if rec.get("TickerType") and rec.get("TickerType") != "ST":
        return None

    member = (rec.get("Representative") or "").strip()
    ticker = normalize_ticker(rec.get("Ticker"))
    side = normalize_side(rec.get("Transaction"))
    txn_date = _parse_date(rec.get("TransactionDate"))
    disc_date = _parse_date(rec.get("ReportDate"))
    amounts = parse_amount_range(rec.get("Range"))
    chamber = _normalize_chamber(rec.get("House"))

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


def normalize_records(records: Iterable[dict], **_kwargs) -> list[Trade]:
    """Normalize an iterable of Quiver Quant dicts to Trades, dropping unusable rows."""
    out: list[Trade] = []
    for rec in records:
        trade = _qv_record_to_trade(rec)
        if trade is not None:
            out.append(trade)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Network fetch
# ──────────────────────────────────────────────────────────────────────────
def fetch_recent_trades(
    lookback_days: int,
    *,
    quiverquant_api_key: str,
    fmp_api_key: str = "",      # kept for backward compat, no longer used
    today: date | None = None,
    client: httpx.Client | None = None,
) -> list[Trade]:
    """Fetch + normalize recent congressional trades from Quiver Quant.

    Requires a free API key from https://www.quiverquant.com/account/signup
    Returns trades whose ``disclosure_date`` is within the lookback window.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=lookback_days)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        headers = {"User-Agent": "congress-mirror-bot/0.1"}
        if quiverquant_api_key:
            headers["Authorization"] = f"Token {quiverquant_api_key}"

        resp = client.get(QUIVER_URL, headers=headers, timeout=30.0)
        if resp.status_code == 401:
            if quiverquant_api_key:
                raise ValueError(
                    "Quiver Quant rejected your API key (401). "
                    "Check QUIVERQUANT_API_KEY in .env."
                )
            raise ValueError(
                "Quiver Quant rate-limited the unauthenticated request (401). "
                "The daily bot won't hit this — it only calls once per day. "
                "For repeated testing, add a free API key from "
                "https://www.quiverquant.com/account/signup to QUIVERQUANT_API_KEY in .env."
            )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raise ValueError(f"Unexpected Quiver Quant response shape: {type(raw)}")

        trades = normalize_records(raw)
        return [t for t in trades if t.disclosure_date >= cutoff]
    finally:
        if owns_client:
            client.close()
