from datetime import date

from congress_bot.executor import (
    OrderIntent,
    compute_order_intents,
    reconcile,
)
from congress_bot.store import Store


def _intents_by_ticker(intents):
    return {i.ticker: i for i in intents}


class TestComputeOrderIntents:
    def test_add_new_position(self):
        intents = compute_order_intents(
            {"AAPL": 1.0}, {}, equity=10000,
            invested_fraction=1.0, rebalance_band=0.2,
        )
        assert len(intents) == 1
        i = intents[0]
        assert (i.ticker, i.side, i.reason) == ("AAPL", "buy", "add")
        assert i.notional == 10000.0

    def test_remove_untargeted_position(self):
        intents = compute_order_intents(
            {}, {"OLD": 5000.0}, equity=10000,
            invested_fraction=1.0, rebalance_band=0.2,
        )
        assert len(intents) == 1
        i = intents[0]
        assert (i.ticker, i.side, i.reason) == ("OLD", "sell", "remove")
        assert i.notional is None  # full liquidation

    def test_within_band_no_order(self):
        # target 5000, current 5500 -> 10% drift < 20% band.
        intents = compute_order_intents(
            {"AAPL": 1.0}, {"AAPL": 5500.0}, equity=5000,
            invested_fraction=1.0, rebalance_band=0.2,
        )
        assert intents == []

    def test_drift_buy_when_underweight(self):
        # target 10000, current 5000 -> 50% drift, buy the 5000 delta.
        intents = compute_order_intents(
            {"AAPL": 1.0}, {"AAPL": 5000.0}, equity=10000,
            invested_fraction=1.0, rebalance_band=0.2,
        )
        i = _intents_by_ticker(intents)["AAPL"]
        assert (i.side, i.reason, i.notional) == ("buy", "drift", 5000.0)

    def test_trim_sell_when_overweight(self):
        # target 5000, current 9000 -> 80% drift, sell 4000.
        intents = compute_order_intents(
            {"AAPL": 1.0}, {"AAPL": 9000.0}, equity=5000,
            invested_fraction=1.0, rebalance_band=0.2,
        )
        i = _intents_by_ticker(intents)["AAPL"]
        assert (i.side, i.reason, i.notional) == ("sell", "trim", 4000.0)

    def test_invested_fraction_scales_targets(self):
        intents = compute_order_intents(
            {"AAPL": 1.0}, {}, equity=10000,
            invested_fraction=0.5, rebalance_band=0.2,
        )
        assert intents[0].notional == 5000.0

    def test_mixed_add_remove_hold(self):
        intents = compute_order_intents(
            {"AAPL": 0.5, "MSFT": 0.5},
            {"MSFT": 5000.0, "OLD": 3000.0},
            equity=10000, invested_fraction=1.0, rebalance_band=0.2,
        )
        by = _intents_by_ticker(intents)
        assert by["AAPL"].reason == "add"
        assert by["OLD"].reason == "remove"
        assert "MSFT" not in by  # 5000 == target, no order


class _FakeBroker:
    def __init__(self, *, equity, positions, market_open=True, tradable=None):
        self._equity = equity
        self._positions = dict(positions)
        self._market_open = market_open
        self._tradable = tradable or set()
        self.submitted = []
        self.closed = []

    def get_account_equity(self):
        return self._equity

    def get_positions(self):
        return dict(self._positions)

    def is_market_open(self):
        return self._market_open

    def submit_notional_order(self, ticker, side, notional, client_order_id):
        self.submitted.append((ticker, side, round(notional, 2), client_order_id))

    def close_position(self, ticker):
        self.closed.append(ticker)

    def tradable_assets(self):
        return self._tradable


class TestReconcile:
    def test_dry_run_submits_nothing_but_logs(self, tmp_path):
        broker = _FakeBroker(equity=10000, positions={})
        with Store(str(tmp_path / "s.db")) as store:
            intents = reconcile(
                {"AAPL": 1.0}, broker, store,
                invested_fraction=1.0, rebalance_band=0.2,
                dry_run=True, today=date(2025, 6, 2),
            )
        assert len(intents) == 1
        assert broker.submitted == []

    def test_live_submits_orders(self, tmp_path):
        broker = _FakeBroker(equity=10000, positions={})
        with Store(str(tmp_path / "s.db")) as store:
            reconcile(
                {"AAPL": 1.0}, broker, store,
                invested_fraction=1.0, rebalance_band=0.2,
                dry_run=False, today=date(2025, 6, 2),
            )
        assert len(broker.submitted) == 1
        ticker, side, notional, coid = broker.submitted[0]
        assert (ticker, side, notional) == ("AAPL", "buy", 10000.0)
        assert coid == "cmb-2025-06-02-AAPL-add"

    def test_market_closed_forces_dry_run(self, tmp_path):
        broker = _FakeBroker(equity=10000, positions={}, market_open=False)
        with Store(str(tmp_path / "s.db")) as store:
            reconcile(
                {"AAPL": 1.0}, broker, store,
                invested_fraction=1.0, rebalance_band=0.2,
                dry_run=False, today=date(2025, 6, 2),
            )
        assert broker.submitted == []

    def test_idempotent_no_double_submit(self, tmp_path):
        broker = _FakeBroker(equity=10000, positions={})
        db = str(tmp_path / "s.db")
        for _ in range(2):
            with Store(db) as store:
                reconcile(
                    {"AAPL": 1.0}, broker, store,
                    invested_fraction=1.0, rebalance_band=0.2,
                    dry_run=False, today=date(2025, 6, 2),
                )
        # Second run sees the logged client_order_id and skips.
        assert len(broker.submitted) == 1

    def test_remove_calls_close_position(self, tmp_path):
        broker = _FakeBroker(equity=10000, positions={"OLD": 5000.0})
        with Store(str(tmp_path / "s.db")) as store:
            reconcile(
                {}, broker, store,
                invested_fraction=1.0, rebalance_band=0.2,
                dry_run=False, today=date(2025, 6, 2),
            )
        assert broker.closed == ["OLD"]
