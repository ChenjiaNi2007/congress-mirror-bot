from datetime import date

from congress_bot.disclosures import Trade
from congress_bot.portfolio import implied_holdings, target_weights


def _t(ticker, side, disc, low, high):
    return Trade("A", "house", ticker, disc, disc, side, low, high)


def test_implied_holdings_nets_buys_minus_sells():
    trades = [
        _t("AAPL", "buy", date(2025, 1, 1), 10000, 10000),
        _t("AAPL", "sell", date(2025, 2, 1), 3000, 3000),
        _t("MSFT", "buy", date(2025, 1, 1), 5000, 5000),
        # Fully sold out -> excluded.
        _t("TSLA", "buy", date(2025, 1, 1), 4000, 4000),
        _t("TSLA", "sell", date(2025, 2, 1), 4000, 4000),
    ]
    holdings = implied_holdings(trades, date(2025, 6, 1))
    assert holdings == {"AAPL": 7000.0, "MSFT": 5000.0}


def test_implied_holdings_window():
    trades = [
        _t("AAPL", "buy", date(2023, 1, 1), 10000, 10000),  # too old
        _t("MSFT", "buy", date(2025, 1, 1), 5000, 5000),
    ]
    holdings = implied_holdings(trades, date(2025, 6, 1))
    assert holdings == {"MSFT": 5000.0}


def test_target_weights_equal_weight_and_tradable_filter():
    trades = [
        _t("AAPL", "buy", date(2025, 1, 1), 10000, 10000),
        _t("MSFT", "buy", date(2025, 1, 1), 5000, 5000),
        _t("PRIV", "buy", date(2025, 1, 1), 8000, 8000),  # not tradable
    ]
    tradable = {"AAPL", "MSFT"}
    weights = target_weights(
        trades, date(2025, 6, 1), tradable=lambda t: t in tradable, max_positions=10
    )
    assert weights == {"AAPL": 0.5, "MSFT": 0.5}


def test_target_weights_caps_at_max_positions_keeping_largest():
    trades = [
        _t("AAA", "buy", date(2025, 1, 1), 1000, 1000),
        _t("BBB", "buy", date(2025, 1, 1), 9000, 9000),
        _t("CCC", "buy", date(2025, 1, 1), 5000, 5000),
    ]
    weights = target_weights(
        trades, date(2025, 6, 1), tradable=lambda t: True, max_positions=2
    )
    # Keep the two largest net-bought: BBB and CCC, equal weight.
    assert set(weights) == {"BBB", "CCC"}
    assert all(abs(w - 0.5) < 1e-9 for w in weights.values())


def test_target_weights_empty_when_nothing_tradable():
    trades = [_t("AAA", "buy", date(2025, 1, 1), 1000, 1000)]
    weights = target_weights(
        trades, date(2025, 6, 1), tradable=lambda t: False, max_positions=10
    )
    assert weights == {}
