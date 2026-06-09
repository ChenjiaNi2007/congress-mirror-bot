from datetime import date

import pytest

from congress_bot.config import Config, ConfigError, load_config
from congress_bot.disclosures import Trade
from congress_bot.store import Store


def _cfg(base_url):
    return Config(
        alpaca_api_key="k", alpaca_secret_key="s", alpaca_base_url=base_url,
        fmp_api_key="f", slack_webhook_url="w", state_db_path="x.db",
        max_positions=15, invested_fraction=0.95, rebalance_band=0.2,
        min_trades=8, min_tickers=4, lookback_days=120, dry_run=True,
    )


class TestPaperAssertion:
    def test_paper_url_ok(self):
        _cfg("https://paper-api.alpaca.markets").assert_paper()  # no raise

    def test_live_url_rejected(self):
        with pytest.raises(ConfigError):
            _cfg("https://api.alpaca.markets").assert_paper()

    def test_load_config_rejects_live(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
        with pytest.raises(ConfigError):
            load_config(dotenv_path="/nonexistent/.env")

    def test_load_config_paper_ok(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        monkeypatch.setenv("DRY_RUN", "false")
        cfg = load_config(dotenv_path="/nonexistent/.env")
        assert cfg.dry_run is False
        assert cfg.max_positions == 15  # default applied


def _t(member, ticker, side="buy"):
    return Trade(member, "house", ticker, date(2025, 1, 1), date(2025, 2, 1),
                 side, 1001, 15000)


class TestStore:
    def test_upsert_returns_only_new(self, tmp_path):
        with Store(str(tmp_path / "s.db")) as store:
            first = store.upsert_disclosures([_t("A", "AAPL"), _t("A", "MSFT")])
            assert len(first) == 2
            # Re-inserting the same plus one new -> only the new one returned.
            second = store.upsert_disclosures([_t("A", "AAPL"), _t("A", "TSLA")])
            assert [t.ticker for t in second] == ["TSLA"]
            assert len(store.all_trades()) == 3

    def test_trades_for_member_case_insensitive(self, tmp_path):
        with Store(str(tmp_path / "s.db")) as store:
            store.upsert_disclosures([_t("Jane Doe", "AAPL"), _t("John Roe", "MSFT")])
            got = store.trades_for_member("jane doe")
            assert [t.ticker for t in got] == ["AAPL"]

    def test_state_chosen_member_and_rerank_date(self, tmp_path):
        with Store(str(tmp_path / "s.db")) as store:
            assert store.chosen_member is None
            store.chosen_member = "Jane Doe"
            store.last_rerank_date = date(2025, 6, 1)
            assert store.chosen_member == "Jane Doe"
            assert store.last_rerank_date == date(2025, 6, 1)

    def test_order_log_idempotency(self, tmp_path):
        with Store(str(tmp_path / "s.db")) as store:
            assert not store.order_exists("coid-1")
            store.log_order(ticker="AAPL", side="buy", notional=100.0, qty=None,
                            dry_run=False, client_order_id="coid-1")
            assert store.order_exists("coid-1")
