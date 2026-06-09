"""Command-line entrypoint + pipeline orchestration.

Subcommands:
  backfill   — fetch disclosures into the store (no trading).
  rerank     — run the backtest leaderboard, pick + persist the mirrored member.
  run-daily  — full daily pipeline (fetch -> maybe rerank -> reconcile -> Slack).
  dry-run    — run-daily with DRY_RUN forced on (never submits).
  serve      — long-lived APScheduler service (daily open + monthly rerank).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import Config, load_config
from .disclosures import fetch_recent_trades
from .executor import AlpacaBroker, TradingBroker, reconcile
from .notify import build_summary, send_summary
from .portfolio import target_weights
from .pricing import AlpacaPriceProvider, PriceProvider
from .ranking import MemberResult, rank_members
from .store import Store


def _rerank_due(store: Store, today: date) -> bool:
    last = store.last_rerank_date
    if last is None:
        return True
    return (today.year, today.month) != (last.year, last.month)


def run_rerank(
    cfg: Config,
    store: Store,
    prices: PriceProvider,
    *,
    asof: date,
) -> list[MemberResult]:
    """Compute the leaderboard from all stored trades; persist the #1 member."""
    leaderboard = rank_members(
        store.all_trades(),
        asof,
        prices,
        min_trades=cfg.min_trades,
        min_tickers=cfg.min_tickers,
    )
    if leaderboard:
        store.chosen_member = leaderboard[0].member
    store.last_rerank_date = asof
    return leaderboard


def run_daily(
    cfg: Config,
    *,
    broker: TradingBroker,
    prices: PriceProvider,
    dry_run_override: bool | None = None,
    force_rerank: bool = False,
    today: date | None = None,
) -> str:
    """Full daily pipeline. Returns the Slack summary text (also sent if configured)."""
    today = today or date.today()
    dry_run = cfg.dry_run if dry_run_override is None else dry_run_override

    with Store(cfg.state_db_path) as store:
        # 1. Fetch + dedup.
        trades = fetch_recent_trades(
            cfg.lookback_days, fmp_api_key=cfg.fmp_api_key, today=today
        )
        new_trades = store.upsert_disclosures(trades)

        # 2. Re-rank (monthly, or forced).
        reranked = force_rerank or _rerank_due(store, today)
        leaderboard: list[MemberResult] = []
        if reranked:
            leaderboard = run_rerank(cfg, store, prices, asof=today)

        chosen = store.chosen_member

        # 3. Target portfolio for the chosen member.
        weights: dict[str, float] = {}
        if chosen:
            tradable = broker.tradable_assets()
            weights = target_weights(
                store.trades_for_member(chosen),
                today,
                tradable=lambda t: t in tradable,
                max_positions=cfg.max_positions,
            )

        # 4. Reconcile.
        intents = reconcile(
            weights,
            broker,
            store,
            invested_fraction=cfg.invested_fraction,
            rebalance_band=cfg.rebalance_band,
            dry_run=dry_run,
            today=today,
        )

        positions = broker.get_positions()

        # 5. Notify.
        summary = build_summary(
            today=today,
            leaderboard=leaderboard,
            chosen_member=chosen,
            new_disclosures=len(new_trades),
            intents=intents,
            positions=positions,
            dry_run=dry_run or not broker.is_market_open(),
            reranked=reranked,
        )
        send_summary(cfg.slack_webhook_url, summary)
        return summary


# ──────────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ──────────────────────────────────────────────────────────────────────────
def _cmd_backfill(cfg: Config) -> int:
    with Store(cfg.state_db_path) as store:
        trades = fetch_recent_trades(cfg.lookback_days, fmp_api_key=cfg.fmp_api_key)
        new = store.upsert_disclosures(trades)
        print(f"Fetched {len(trades)} trades; {len(new)} new; "
              f"{len(store.all_trades())} total in store.")
    return 0


def _cmd_rerank(cfg: Config) -> int:
    prices = AlpacaPriceProvider(cfg.alpaca_api_key, cfg.alpaca_secret_key)
    with Store(cfg.state_db_path) as store:
        if not store.all_trades():
            store.upsert_disclosures(
                fetch_recent_trades(cfg.lookback_days, fmp_api_key=cfg.fmp_api_key)
            )
        leaderboard = run_rerank(cfg, store, prices, asof=date.today())
    print(f"Chosen member: {store_chosen(cfg)}")
    for i, r in enumerate(leaderboard[:10], start=1):
        print(f"{i:2}. {r.member} ({r.chamber}) {r.trailing_return:+.1%} "
              f"[{r.n_trades}t/{r.n_tickers}k]")
    return 0


def store_chosen(cfg: Config) -> str | None:
    with Store(cfg.state_db_path) as store:
        return store.chosen_member


def _cmd_run_daily(cfg: Config, *, dry: bool) -> int:
    broker = AlpacaBroker(cfg.alpaca_api_key, cfg.alpaca_secret_key)
    prices = AlpacaPriceProvider(cfg.alpaca_api_key, cfg.alpaca_secret_key)
    summary = run_daily(
        cfg, broker=broker, prices=prices, dry_run_override=True if dry else None
    )
    print(summary)
    return 0


def _cmd_serve(cfg: Config) -> int:
    from .scheduler import serve

    serve(cfg)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="congress-bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "rerank", "run-daily", "dry-run", "serve"):
        sub.add_parser(name)

    args = parser.parse_args(argv)
    cfg = load_config()  # validates paper endpoint

    if args.cmd == "backfill":
        return _cmd_backfill(cfg)
    if args.cmd == "rerank":
        return _cmd_rerank(cfg)
    if args.cmd == "run-daily":
        return _cmd_run_daily(cfg, dry=False)
    if args.cmd == "dry-run":
        return _cmd_run_daily(cfg, dry=True)
    if args.cmd == "serve":
        return _cmd_serve(cfg)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
