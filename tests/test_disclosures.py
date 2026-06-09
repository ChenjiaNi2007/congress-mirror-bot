from datetime import date

from congress_bot.disclosures import (
    Trade,
    normalize_records,
    normalize_side,
    normalize_ticker,
    parse_amount_range,
)


class TestNormalizeTicker:
    def test_plain(self):
        assert normalize_ticker("aapl") == "AAPL"

    def test_strips_suffix(self):
        assert normalize_ticker("AAPL:US") == "AAPL"
        assert normalize_ticker("AAPL.O") == "AAPL"
        assert normalize_ticker("AAPL US") == "AAPL"

    def test_drops_unresolvable(self):
        for bad in [None, "", "--", "N/A", "123", "TOO LONG NAME", "BRK/B"]:
            assert normalize_ticker(bad) is None

    def test_drops_long_tokens(self):
        # 6+ letters is not a US-equity ticker in our model.
        assert normalize_ticker("GOOGLE") is None


class TestParseAmountRange:
    def test_range(self):
        assert parse_amount_range("$1,001 - $15,000") == (1001.0, 15000.0)

    def test_dash_variants(self):
        assert parse_amount_range("1001–15000") == (1001.0, 15000.0)
        assert parse_amount_range("1001 to 15000") == (1001.0, 15000.0)

    def test_single_value(self):
        assert parse_amount_range("$15,000") == (15000.0, 15000.0)

    def test_orders_low_high(self):
        assert parse_amount_range("15000 - 1001") == (1001.0, 15000.0)

    def test_none(self):
        assert parse_amount_range(None) is None
        assert parse_amount_range("n/a") is None


class TestNormalizeSide:
    def test_buy(self):
        assert normalize_side("Purchase") == "buy"
        assert normalize_side("P") == "buy"

    def test_sell(self):
        assert normalize_side("Sale (Full)") == "sell"
        assert normalize_side("Sale (Partial)") == "sell"
        assert normalize_side("S") == "sell"

    def test_other(self):
        assert normalize_side("Exchange") is None
        assert normalize_side(None) is None


class TestNormalizeRecords:
    def test_fmp_record(self):
        recs = [
            {
                "representative": "Jane Doe",
                "ticker": "MSFT",
                "type": "Purchase",
                "transactionDate": "2025-01-15",
                "disclosureDate": "2025-02-20",
                "amount": "$1,001 - $15,000",
            }
        ]
        trades = normalize_records(recs, source="fmp", chamber="house")
        assert len(trades) == 1
        t = trades[0]
        assert t.member == "Jane Doe"
        assert t.chamber == "house"
        assert t.ticker == "MSFT"
        assert t.side == "buy"
        assert t.txn_date == date(2025, 1, 15)
        assert t.disclosure_date == date(2025, 2, 20)
        assert t.amount_mid == 8000.5

    def test_drops_option_and_bond_rows(self):
        recs = [
            {"representative": "X", "ticker": "--", "type": "Purchase",
             "transactionDate": "2025-01-15", "amount": "$1,001 - $15,000"},
            {"representative": "Y", "ticker": "AAPL 250117C00150000",
             "type": "Purchase", "transactionDate": "2025-01-15",
             "amount": "$1,001 - $15,000"},
            {"representative": "Z", "ticker": "AAPL", "type": "Purchase",
             "transactionDate": "2025-01-15", "amount": "$1,001 - $15,000"},
        ]
        trades = normalize_records(recs, source="fmp", chamber="house")
        # Only the clean AAPL row survives.
        assert [t.ticker for t in trades] == ["AAPL"]

    def test_ssw_record(self):
        recs = [
            {
                "senator": "John Roe",
                "ticker": "NVDA",
                "type": "Sale (Full)",
                "transaction_date": "01/15/2025",
                "disclosure_date": "02/25/2025",
                "amount": "$15,001 - $50,000",
            }
        ]
        trades = normalize_records(recs, source="ssw")
        assert len(trades) == 1
        t = trades[0]
        assert t.chamber == "senate"
        assert t.side == "sell"
        assert t.txn_date == date(2025, 1, 15)

    def test_missing_disclosure_falls_back_to_txn_date(self):
        recs = [
            {"representative": "A", "ticker": "TSLA", "type": "Purchase",
             "transactionDate": "2025-03-01", "amount": "$1,001 - $15,000"},
        ]
        t = normalize_records(recs, source="fmp", chamber="house")[0]
        assert t.disclosure_date == t.txn_date == date(2025, 3, 1)


def test_dedup_key_stable_and_distinct():
    t1 = Trade("A", "house", "AAPL", date(2025, 1, 1), date(2025, 2, 1),
               "buy", 1001, 15000)
    t2 = Trade("A", "house", "AAPL", date(2025, 1, 1), date(2025, 2, 1),
               "buy", 1001, 15000)
    t3 = Trade("A", "house", "AAPL", date(2025, 1, 1), date(2025, 2, 1),
               "sell", 1001, 15000)
    assert t1.dedup_key() == t2.dedup_key()
    assert t1.dedup_key() != t3.dedup_key()
