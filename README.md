# SheepOfWallStreet

Swing trading app for US stocks/ETFs on daily bars, built for a small account. A Trader makes the
calls; a Reviewer tags mistakes and proposes rule changes for human approval. See `CLAUDE.md` for
the full design.

> Not financial advice. Most simple strategies fail after fees and slippage. Paper trade first.

## Setup

```sh
uv sync
echo 'DATABASE_URL=postgresql://...' > .env     # gitignored; never commit it
uv run --env-file .env python -m sheep.common.db  # create tables, sync config/ into the DB
uv run --env-file .env pytest                     # DB tests run in a rolled-back temp schema
```

Tip: `export UV_ENV_FILE=.env` to drop the `--env-file` flag.

## Daily use

```sh
uv run --env-file .env python -m sheep.trader.run          # after 16:30 NY: fills, exits, new entries
uv run --env-file .env streamlit run streamlit_app.py      # read-only dashboard
uv run --env-file .env python -m sheep.backtest.engine main_paper   # walk-forward vs SPY
```

The Trader runs itself on weekdays at 22:15 UTC through GitHub Actions
(`.github/workflows/trader.yml`); add `DATABASE_URL` under the repo's Settings → Secrets and
variables → Actions. You can also start it by hand from the Actions tab ("Run workflow"). A run
where any profile fails is marked failed, and GitHub emails you.

The dashboard deploys to Streamlit Community Cloud as-is: main file `streamlit_app.py`, and
`DATABASE_URL = "postgresql://..."` in the app's Secrets.

Profiles live in `config/profiles.yaml`, rule versions in `config/strategy.yaml`. Rule versions are
immutable once synced; add a new version to change params.

## Research tools

Adapted for daily bars from [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT, Jake Schincariol): its scanner, news monitor, time machine and risk manager. Also on the
dashboard's Scanner and Time machine pages.

```sh
uv run python -m sheep.trader.scanner SPY QQQ XLK JPM   # setups on daily closes; --universe = whole US market
uv run --env-file .env python -m sheep.trader.news JPM XLF --hours 48   # headlines + SEC 8-Ks
uv run python -m sheep.backtest.timemachine SPY --version v1           # 10,000 replays + the rule's real record
```

SEC filings need `SEC_USER_AGENT="Your Name you@example.com"` in `.env` (and in the Streamlit
app's Secrets); without it the news shows headlines only.

**Risk gate.** Every new entry, in the Trader and in backtests, must pass `sheep/trader/risk.py`
or it is blocked (logged as `BLOCK ...`, never resized): stop below entry below target, loss at
the stop within `risk_per_trade_pct`, reward:risk of at least `min_reward_risk` (default 1),
position cost within `max_position_pct` of equity (default 100), symbol not in `blocked`, and
(Trader only) a signal from the latest session's bar, not a stale one. Set the optional keys under
a profile's `risk:` in `config/profiles.yaml`. Exits are never blocked.
