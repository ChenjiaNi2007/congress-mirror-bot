# Congressional Trade Mirror Bot

Mirrors disclosed congressional stock trades into an **Alpaca paper account**,
re-ranks the member it follows monthly, and posts a daily Slack summary.

> ⚠️ **For-interest, paper-only.** STOCK Act Periodic Transaction Reports disclose
> *transactions* (ticker, buy/sell, a dollar **range**, transaction date) with a
> reporting lag of up to ~45 days. They do **not** disclose returns, P&L, or
> current positions. So "rank by returns" and "mirror positions" are
> *reconstructed* from delayed, range-based data. The ~45-day lag means we always
> act on stale information — **this is not a source of alpha.** The Alpaca paper
> endpoint is hard-asserted in `config.py`; the bot refuses to run against live.

## How it works

1. **Disclosures** (`disclosures.py`) — fetch + normalize Senate **and** House
   trades from Financial Modeling Prep (FMP); fall back to Senate Stock Watcher.
   Options / bonds / non-US / unresolvable tickers are dropped.
2. **Store** (`store.py`) — SQLite dedup of filings, chosen-member state, and an
   orders audit log (idempotency).
3. **Ranking** (`ranking.py`) — a lookahead-free *copy-at-disclosure backtest*:
   for each active member, simulate a portfolio that copied their disclosed trades
   at the disclosure-date close, mark to market, and take the trailing-12-month
   return. Rank desc. Re-ranked monthly.
4. **Portfolio** (`portfolio.py`) — implied current holdings (net-bought, not yet
   sold) → filter to Alpaca-tradable → cap at `MAX_POSITIONS` → equal weight.
5. **Executor** (`executor.py`) — diff target vs live positions → **notional
   market orders** for adds / removals / drift beyond `REBALANCE_BAND`. Idempotent,
   skips a closed market, honors `DRY_RUN`.
6. **Notify** (`notify.py`) — Slack webhook summary with leaderboard, mirrored
   member, new disclosures, orders, positions, and a standing disclaimer.
7. **Scheduler** (`scheduler.py`) — in-process APScheduler, one US/Eastern weekday
   job after the open; monthly rerank is data-driven on the first run of the month.

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in your keys; keep ALPACA_BASE_URL on paper
```

## CLI

```bash
congress-bot backfill    # fetch disclosures into the store (no trading)
congress-bot rerank      # run the leaderboard, pick + persist the mirrored member
congress-bot dry-run     # full pipeline, DRY_RUN forced on (never submits)
congress-bot run-daily   # full pipeline (submits if market open & DRY_RUN=false)
congress-bot serve       # long-lived scheduler service
```

## Verify before going live

1. `pytest` — unit tests (normalization, backtest math, target math, reconcile).
2. `congress-bot dry-run` against live FMP + Alpaca paper — inspect the leaderboard,
   chosen member, and the orders it *would* place.
3. `congress-bot rerank` — sanity-check that known active members appear.
4. During market hours, set `DRY_RUN=false` and run `congress-bot run-daily` once;
   confirm orders in the Alpaca paper dashboard and a summary in Slack.
5. `serve` with a temporary near-term trigger to confirm unattended runs, then
   restore the 09:35 ET trigger.

## Config (`.env`)

| Var | Meaning |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper credentials |
| `ALPACA_BASE_URL` | must contain `paper-api.alpaca.markets` (asserted) |
| `FMP_API_KEY` | Financial Modeling Prep free key |
| `SLACK_WEBHOOK_URL` | incoming webhook for the summary channel |
| `MAX_POSITIONS` | max distinct positions (default 15) |
| `INVESTED_FRACTION` | fraction of equity deployed (default 0.95) |
| `REBALANCE_BAND` | relative drift before rebalancing (default 0.20) |
| `MIN_TRADES` / `MIN_TICKERS` | min sample to rank a member (8 / 4) |
| `LOOKBACK_DAYS` | disclosure fetch window (default 120) |
| `DRY_RUN` | preview only; **default on** until you flip it |

## Deploy

- **Fly.io** (recommended): `fly.toml` + a persistent volume for `state.db`,
  secrets via `fly secrets set`. One always-on small machine.
- **VPS + systemd**: `deploy/congress-mirror-bot.service`.
- Local-Mac launchd is **not** recommended — it misses runs while the Mac sleeps.

## Caveats (also in code comments + the Slack disclaimer)

- ~45-day disclosure lag → always trading on stale info; not alpha.
- Range-midpoint sizing and disclosed-trades-only reconstruction are approximations.
- FMP free tier ~250 calls/day → prices come from Alpaca, not FMP; fetches cached.
- Equity-only mirror: options / bonds / non-US holdings are skipped.
- Paper account hard-asserted; `DRY_RUN` default-on until explicitly flipped.
