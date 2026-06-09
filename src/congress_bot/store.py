"""SQLite state.

Three tables on a persistent volume:
  - ``disclosures`` — every normalized trade, deduped by filing/txn key. Drives
    the "new since last run" set.
  - ``state``       — key/value: chosen member, last rerank date, last run time.
  - ``orders``      — audit log of submitted orders (idempotency / Slack summary).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Iterable

from .disclosures import Trade

_SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosures (
    dedup_key        TEXT PRIMARY KEY,
    member           TEXT NOT NULL,
    chamber          TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    txn_date         TEXT NOT NULL,
    disclosure_date  TEXT NOT NULL,
    side             TEXT NOT NULL,
    amount_low       REAL NOT NULL,
    amount_high      REAL NOT NULL,
    first_seen       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL,
    notional     REAL,
    qty          REAL,
    dry_run      INTEGER NOT NULL,
    client_order_id TEXT,
    note         TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── disclosures ────────────────────────────────────────────────────────
    def upsert_disclosures(self, trades: Iterable[Trade]) -> list[Trade]:
        """Insert trades, returning only those that were *new* (not seen before)."""
        new: list[Trade] = []
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            for t in trades:
                cur.execute(
                    "SELECT 1 FROM disclosures WHERE dedup_key = ?", (t.dedup_key(),)
                )
                if cur.fetchone() is not None:
                    continue
                cur.execute(
                    """INSERT INTO disclosures
                       (dedup_key, member, chamber, ticker, txn_date, disclosure_date,
                        side, amount_low, amount_high, first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        t.dedup_key(),
                        t.member,
                        t.chamber,
                        t.ticker,
                        t.txn_date.isoformat(),
                        t.disclosure_date.isoformat(),
                        t.side,
                        t.amount_low,
                        t.amount_high,
                        now,
                    ),
                )
                new.append(t)
        self._conn.commit()
        return new

    def all_trades(self) -> list[Trade]:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT * FROM disclosures")
            return [_row_to_trade(r) for r in cur.fetchall()]

    def trades_for_member(self, member: str) -> list[Trade]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM disclosures WHERE LOWER(member) = LOWER(?)", (member,)
            )
            return [_row_to_trade(r) for r in cur.fetchall()]

    # ── state k/v ──────────────────────────────────────────────────────────
    def get_state(self, key: str) -> str | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT value FROM state WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    @property
    def chosen_member(self) -> str | None:
        return self.get_state("chosen_member")

    @chosen_member.setter
    def chosen_member(self, member: str) -> None:
        self.set_state("chosen_member", member)

    @property
    def last_rerank_date(self) -> date | None:
        v = self.get_state("last_rerank_date")
        return date.fromisoformat(v) if v else None

    @last_rerank_date.setter
    def last_rerank_date(self, d: date) -> None:
        self.set_state("last_rerank_date", d.isoformat())

    # ── orders audit log ─────────────────────────────────────────────────────
    def log_order(
        self,
        *,
        ticker: str,
        side: str,
        notional: float | None,
        qty: float | None,
        dry_run: bool,
        client_order_id: str | None = None,
        note: str = "",
    ) -> None:
        self._conn.execute(
            """INSERT INTO orders
               (submitted_at, ticker, side, notional, qty, dry_run, client_order_id, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                ticker,
                side,
                notional,
                qty,
                1 if dry_run else 0,
                client_order_id,
                note,
            ),
        )
        self._conn.commit()

    def order_exists(self, client_order_id: str) -> bool:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT 1 FROM orders WHERE client_order_id = ?", (client_order_id,)
            )
            return cur.fetchone() is not None


def _row_to_trade(r: sqlite3.Row) -> Trade:
    return Trade(
        member=r["member"],
        chamber=r["chamber"],
        ticker=r["ticker"],
        txn_date=date.fromisoformat(r["txn_date"]),
        disclosure_date=date.fromisoformat(r["disclosure_date"]),
        side=r["side"],
        amount_low=r["amount_low"],
        amount_high=r["amount_high"],
    )
