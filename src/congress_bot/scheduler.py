"""Long-lived APScheduler service (cli.py serve).

One timezone-aware (US/Eastern) job: a daily run shortly after the US market open
on weekdays. The job checks the Alpaca clock and no-ops (for trading) if the
market is actually closed (holiday / early close), so we don't need a separate
holiday calendar.

The **monthly re-rank** is data-driven rather than a second trigger: ``run_daily``
re-ranks automatically on the first run of each calendar month (see
``cli._rerank_due``) and holds the chosen member in between. A second cron trigger
would only risk double-runs on the same day, so we intentionally keep one job.

We rely on the in-process scheduler (no host cron) so the single container is
fully self-contained.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config
from .executor import AlpacaBroker
from .pricing import AlpacaPriceProvider

log = logging.getLogger("congress_bot.scheduler")

EASTERN = "US/Eastern"


def serve(cfg: Config) -> None:
    """Block forever, running the daily pipeline after each weekday open."""
    logging.basicConfig(level=logging.INFO)
    broker = AlpacaBroker(cfg.alpaca_api_key, cfg.alpaca_secret_key)
    prices = AlpacaPriceProvider(cfg.alpaca_api_key, cfg.alpaca_secret_key)

    def _daily_job() -> None:
        from .cli import run_daily

        try:
            log.info("Running daily pipeline")
            summary = run_daily(cfg, broker=broker, prices=prices)
            log.info("Daily pipeline complete:\n%s", summary)
        except Exception:  # keep the scheduler alive across failures
            log.exception("Daily pipeline failed")

    sched = BlockingScheduler(timezone=EASTERN)
    # 09:35 ET on weekdays — a few minutes after the 09:30 open. The pipeline
    # re-ranks itself on the first run of each new month.
    sched.add_job(
        _daily_job,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=35, timezone=EASTERN),
        id="daily_open",
        name="daily-after-open",
        misfire_grace_time=3600,
    )
    log.info("Scheduler started (US/Eastern). Daily job at 09:35; monthly "
             "rerank is data-driven on the first run of each month.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
