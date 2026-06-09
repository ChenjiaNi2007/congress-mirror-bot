from datetime import date

from congress_bot.executor import OrderIntent
from congress_bot.notify import DISCLAIMER, build_summary
from congress_bot.ranking import MemberResult


def test_build_summary_includes_key_sections():
    text = build_summary(
        today=date(2025, 6, 2),
        leaderboard=[MemberResult("Jane Doe", "house", 0.234, 12, 6)],
        chosen_member="Jane Doe",
        new_disclosures=3,
        intents=[OrderIntent("AAPL", "buy", 5000.0, None, "add")],
        positions={"AAPL": 5000.0},
        dry_run=True,
        reranked=True,
    )
    assert "2025-06-02" in text
    assert "Jane Doe" in text
    assert "+23.4%" in text
    assert "New disclosures since last run:* 3" in text
    assert "BUY AAPL" in text
    assert "DRY RUN" in text
    assert DISCLAIMER in text


def test_build_summary_no_orders():
    text = build_summary(
        today=date(2025, 6, 2),
        leaderboard=[],
        chosen_member=None,
        new_disclosures=0,
        intents=[],
        positions={},
        dry_run=False,
        reranked=False,
    )
    assert "none (portfolio already on target)" in text
    assert DISCLAIMER in text
