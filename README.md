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

The dashboard deploys to Streamlit Community Cloud as-is: main file `streamlit_app.py`, and
`DATABASE_URL = "postgresql://..."` in the app's Secrets.

Profiles live in `config/profiles.yaml`, rule versions in `config/strategy.yaml`. Rule versions are
immutable once synced; add a new version to change params.
