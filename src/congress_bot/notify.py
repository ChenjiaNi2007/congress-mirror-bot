"""Slack summary via incoming webhook."""
from __future__ import annotations

from datetime import date

import httpx

from .executor import OrderIntent
from .ranking import MemberResult

DISCLAIMER = (
    ":warning: For-interest paper strategy. Mirrors STOCK Act disclosures with a "
    "~45-day reporting lag (stale by design) using range-midpoint sizing and "
    "disclosed-trades-only reconstruction. Not investment advice; not alpha."
)


def build_summary(
    *,
    today: date,
    leaderboard: list[MemberResult],
    chosen_member: str | None,
    new_disclosures: int,
    intents: list[OrderIntent],
    positions: dict[str, float],
    dry_run: bool,
    reranked: bool,
) -> str:
    """Build a human-readable Slack message (mrkdwn)."""
    lines: list[str] = [f"*Congress Mirror Bot — {today.isoformat()}*"]
    if dry_run:
        lines.append("_(DRY RUN — no orders submitted)_")

    if chosen_member:
        tag = " _(re-ranked today)_" if reranked else ""
        lines.append(f"*Mirrored member:* {chosen_member}{tag}")

    if leaderboard:
        lines.append("*Leaderboard (trailing-12mo copy-at-disclosure return):*")
        for i, r in enumerate(leaderboard[:5], start=1):
            lines.append(
                f"  {i}. {r.member} ({r.chamber}) — {r.trailing_return:+.1%} "
                f"[{r.n_trades} trades / {r.n_tickers} tickers]"
            )

    lines.append(f"*New disclosures since last run:* {new_disclosures}")

    if intents:
        verb = "Would place" if dry_run else "Placed"
        lines.append(f"*{verb} {len(intents)} order(s):*")
        for it in intents:
            amt = (
                f"${it.notional:,.0f}"
                if it.notional is not None
                else ("liquidate" if it.reason == "remove" else "")
            )
            lines.append(f"  • {it.side.upper()} {it.ticker} {amt} ({it.reason})")
    else:
        lines.append("*Orders:* none (portfolio already on target)")

    if positions:
        lines.append("*Current positions (market value):*")
        for tkr, mv in sorted(positions.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  • {tkr}: ${mv:,.0f}")

    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def send_summary(
    webhook_url: str, text: str, *, client: httpx.Client | None = None
) -> bool:
    """POST the summary to Slack. Returns True on success; never raises."""
    if not webhook_url:
        return False
    owns = client is None
    client = client or httpx.Client()
    try:
        resp = client.post(webhook_url, json={"text": text}, timeout=15.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        if owns:
            client.close()
