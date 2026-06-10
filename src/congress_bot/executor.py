"""Reconcile a target portfolio with the live Alpaca paper account.

Given target weights and account equity, compute target dollar value per ticker
(``weight * equity * invested_fraction``) and submit **notional market orders**
only when a ticker is added, removed (liquidate), or has drifted beyond the
rebalance band. Idempotent via the orders audit log; honors DRY_RUN; never acts
when the market is closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from .store import Store


@dataclass(frozen=True)
class OrderIntent:
    ticker: str
    side: str  # "buy" | "sell"
    notional: float | None  # used for buys / partial sells
    qty: float | None  # used for full liquidation (close position)
    reason: str  # "add" | "remove" | "drift" | "trim"


class TradingBroker(Protocol):
    def get_account_equity(self) -> float: ...
    def get_positions(self) -> dict[str, float]:  # ticker -> current market value
        ...
    def is_market_open(self) -> bool: ...
    def submit_notional_order(
        self, ticker: str, side: str, notional: float, client_order_id: str
    ) -> None: ...
    def close_position(self, ticker: str) -> None: ...
    def tradable_assets(self) -> set[str]: ...


def compute_order_intents(
    target_weights: dict[str, float],
    positions: dict[str, float],
    equity: float,
    *,
    invested_fraction: float,
    rebalance_band: float,
) -> list[OrderIntent]:
    """Pure planning step: diff target vs current positions -> order intents.

    - ticker in target but not held -> ``add`` (buy to target $).
    - ticker held but not in target -> ``remove`` (liquidate).
    - held & targeted, |current-target|/target > band -> ``drift`` buy / ``trim`` sell.
    """
    investable = equity * invested_fraction
    intents: list[OrderIntent] = []

    target_dollars = {t: w * investable for t, w in target_weights.items()}

    # Removals: held but no longer targeted.
    for ticker in positions:
        if ticker not in target_weights:
            intents.append(
                OrderIntent(ticker, "sell", None, None, reason="remove")
            )

    # Adds / rebalances.
    for ticker, tgt in target_dollars.items():
        cur = positions.get(ticker, 0.0)
        if cur <= 0:
            if tgt > 0:
                intents.append(OrderIntent(ticker, "buy", tgt, None, reason="add"))
            continue
        if tgt <= 0:
            continue
        drift = abs(cur - tgt) / tgt
        if drift <= rebalance_band:
            continue
        delta = tgt - cur
        if delta > 0:
            intents.append(OrderIntent(ticker, "buy", delta, None, reason="drift"))
        else:
            intents.append(
                OrderIntent(ticker, "sell", abs(delta), None, reason="trim")
            )

    return intents


def reconcile(
    target_weights: dict[str, float],
    broker: TradingBroker,
    store: Store,
    *,
    invested_fraction: float,
    rebalance_band: float,
    dry_run: bool,
    today: date | None = None,
) -> list[OrderIntent]:
    """Plan and (unless dry-run / market closed) submit orders. Returns intents."""
    today = today or date.today()

    import os
    force = os.environ.get("FORCE_ORDERS", "").strip().lower() in {"1", "true", "yes"}
    if not dry_run and not force and not broker.is_market_open():
        # Plan is still computed for the Slack summary, but nothing is submitted.
        dry_run = True

    equity = broker.get_account_equity()
    positions = broker.get_positions()
    intents = compute_order_intents(
        target_weights,
        positions,
        equity,
        invested_fraction=invested_fraction,
        rebalance_band=rebalance_band,
    )

    for intent in intents:
        client_order_id = _client_order_id(intent, today)
        if store.order_exists(client_order_id):
            continue  # idempotency: already submitted this logical order today
        if dry_run:
            store.log_order(
                ticker=intent.ticker,
                side=intent.side,
                notional=intent.notional,
                qty=intent.qty,
                dry_run=True,
                client_order_id=client_order_id,
                note=f"DRY_RUN {intent.reason}",
            )
            continue

        if intent.reason == "remove":
            broker.close_position(intent.ticker)
        else:
            broker.submit_notional_order(
                intent.ticker,
                intent.side,
                float(intent.notional or 0.0),
                client_order_id,
            )
        store.log_order(
            ticker=intent.ticker,
            side=intent.side,
            notional=intent.notional,
            qty=intent.qty,
            dry_run=False,
            client_order_id=client_order_id,
            note=intent.reason,
        )

    return intents


def _client_order_id(intent: OrderIntent, day: date) -> str:
    """Deterministic per-(ticker, reason, day) id so re-runs don't double-submit."""
    return f"cmb-{day.isoformat()}-{intent.ticker}-{intent.reason}"


# ──────────────────────────────────────────────────────────────────────────
# Live Alpaca implementation
# ──────────────────────────────────────────────────────────────────────────
class AlpacaBroker:
    """Concrete :class:`TradingBroker` backed by alpaca-py TradingClient (paper)."""

    def __init__(self, api_key: str, secret_key: str):
        from alpaca.trading.client import TradingClient

        # paper=True is enforced; config.assert_paper already validated the URL.
        self._client = TradingClient(api_key, secret_key, paper=True)
        self._tradable_cache: set[str] | None = None

    def get_account_equity(self) -> float:
        return float(self._client.get_account().equity)

    def get_positions(self) -> dict[str, float]:
        return {
            p.symbol: float(p.market_value) for p in self._client.get_all_positions()
        }

    def is_market_open(self) -> bool:
        return bool(self._client.get_clock().is_open)

    def submit_notional_order(
        self, ticker: str, side: str, notional: float, client_order_id: str
    ) -> None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(
            symbol=ticker,
            notional=round(notional, 2),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        self._client.submit_order(req)

    def close_position(self, ticker: str) -> None:
        self._client.close_position(ticker)

    def tradable_assets(self) -> set[str]:
        if self._tradable_cache is not None:
            return self._tradable_cache
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        req = GetAssetsRequest(
            status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY
        )
        assets = self._client.get_all_assets(req)
        self._tradable_cache = {a.symbol for a in assets if a.tradable}
        return self._tradable_cache
