"""Environment loading + validation.

The single most important invariant lives here: ``ALPACA_BASE_URL`` MUST point at
a paper endpoint. We hard-assert it so a misconfigured live key can never place a
real-money order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Substring that must appear in the Alpaca base URL for us to consider it "paper".
_PAPER_MARKER = "paper-api.alpaca.markets"


class ConfigError(RuntimeError):
    """Raised when required config is missing or unsafe."""


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"Missing required env var: {name}")
    return val


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # Alpaca
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    # Data
    fmp_api_key: str
    # Slack
    slack_webhook_url: str
    # State
    state_db_path: str
    # Tunables
    max_positions: int
    invested_fraction: float
    rebalance_band: float
    min_trades: int
    min_tickers: int
    lookback_days: int
    dry_run: bool

    def assert_paper(self) -> None:
        """Fail loud unless the Alpaca endpoint is the paper endpoint."""
        if _PAPER_MARKER not in self.alpaca_base_url:
            raise ConfigError(
                f"Refusing to run: ALPACA_BASE_URL={self.alpaca_base_url!r} is not a "
                f"paper endpoint (must contain {_PAPER_MARKER!r}). This bot is "
                "paper-only by design."
            )


def load_config(*, dotenv_path: str | None = None) -> Config:
    """Load config from environment (and a .env file if present), then validate.

    Always calls :meth:`Config.assert_paper` before returning.
    """
    load_dotenv(dotenv_path=dotenv_path, override=False)

    cfg = Config(
        alpaca_api_key=_require("ALPACA_API_KEY"),
        alpaca_secret_key=_require("ALPACA_SECRET_KEY"),
        alpaca_base_url=_require("ALPACA_BASE_URL"),
        fmp_api_key=os.environ.get("FMP_API_KEY", "").strip(),
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL", "").strip(),
        state_db_path=os.environ.get("STATE_DB_PATH", "state.db").strip() or "state.db",
        max_positions=_int("MAX_POSITIONS", 15),
        invested_fraction=_float("INVESTED_FRACTION", 0.95),
        rebalance_band=_float("REBALANCE_BAND", 0.20),
        min_trades=_int("MIN_TRADES", 8),
        min_tickers=_int("MIN_TICKERS", 4),
        lookback_days=_int("LOOKBACK_DAYS", 120),
        dry_run=_flag("DRY_RUN", True),
    )
    cfg.assert_paper()
    return cfg
