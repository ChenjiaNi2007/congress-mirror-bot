from datetime import date

from congress_bot.disclosures import Trade
from congress_bot.pricing import DictPriceProvider
from congress_bot.ranking import backtest_member, rank_members


def _t(member, ticker, side, disc, low, high):
    return Trade(member, "house", ticker, disc, disc, side, low, high)


def test_backtest_simple_buy_and_mark():
    # Buy $10,000 of AAPL at $100 (100 sh), mark at $120 -> +20%.
    trades = [_t("A", "AAPL", "buy", date(2025, 1, 10), 5000, 15000)]
    prices = DictPriceProvider(
        {"AAPL": {date(2025, 1, 10): 100.0}}, latest={"AAPL": 120.0}
    )
    ret = backtest_member(trades, date(2025, 6, 1), prices)
    assert round(ret, 6) == 0.20


def test_backtest_buy_then_partial_sell():
    # Buy $10k @ $100 (100sh). Later sell $6k worth @ $150 (40sh) -> cash 6000,
    # 60 sh left. Mark latest $150 -> holdings 9000. final 15000 / initial 10000.
    trades = [
        _t("A", "AAPL", "buy", date(2025, 1, 10), 5000, 15000),
        _t("A", "AAPL", "sell", date(2025, 3, 10), 4000, 8000),
    ]
    prices = DictPriceProvider(
        {"AAPL": {date(2025, 1, 10): 100.0, date(2025, 3, 10): 150.0}},
        latest={"AAPL": 150.0},
    )
    ret = backtest_member(trades, date(2025, 6, 1), prices)
    assert round(ret, 6) == 0.5


def test_backtest_none_when_unpriceable():
    trades = [_t("A", "ZZZZ", "buy", date(2025, 1, 10), 5000, 15000)]
    prices = DictPriceProvider({})  # no prices
    assert backtest_member(trades, date(2025, 6, 1), prices) is None


def test_backtest_none_when_no_buys():
    trades = [_t("A", "AAPL", "sell", date(2025, 1, 10), 5000, 15000)]
    prices = DictPriceProvider({"AAPL": {date(2025, 1, 10): 100.0}})
    assert backtest_member(trades, date(2025, 6, 1), prices) is None


def test_rank_members_orders_desc_and_filters_small_samples():
    asof = date(2025, 6, 1)
    prices = DictPriceProvider(
        {
            "AAA": {date(2025, 1, 1): 100.0},
            "BBB": {date(2025, 1, 1): 100.0},
            "CCC": {date(2025, 1, 1): 100.0},
        },
        latest={"AAA": 200.0, "BBB": 110.0, "CCC": 100.0},
    )
    trades = [
        # Winner: AAA doubled.
        _t("Winner", "AAA", "buy", date(2025, 1, 1), 9000, 11000),
        _t("Winner", "BBB", "buy", date(2025, 1, 1), 9000, 11000),
        # Loser: only BBB +10%.
        _t("Loser", "BBB", "buy", date(2025, 1, 1), 9000, 11000),
        _t("Loser", "CCC", "buy", date(2025, 1, 1), 9000, 11000),
        # TooSmall: single ticker -> filtered when min_tickers=2.
        _t("TooSmall", "AAA", "buy", date(2025, 1, 1), 9000, 11000),
    ]
    board = rank_members(trades, asof, prices, min_trades=2, min_tickers=2)
    names = [r.member for r in board]
    assert names == ["Winner", "Loser"]
    assert board[0].trailing_return > board[1].trailing_return


def test_rank_members_excludes_out_of_window():
    asof = date(2025, 6, 1)
    prices = DictPriceProvider({"AAA": {date(2023, 1, 1): 100.0}},
                               latest={"AAA": 200.0})
    # Disclosure 2 years before asof -> outside trailing 12mo window.
    trades = [
        _t("Old", "AAA", "buy", date(2023, 1, 1), 9000, 11000),
        _t("Old", "AAA", "buy", date(2023, 1, 2), 9000, 11000),
    ]
    board = rank_members(trades, asof, prices, min_trades=1, min_tickers=1)
    assert board == []
