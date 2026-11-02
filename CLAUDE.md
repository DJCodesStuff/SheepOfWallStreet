# SheepOfWallStreet

An open-source Python application for swing trading US stocks and ETFs on daily bars, built around a small starting account ($100). It has two cooperating parts: a **Trader** that makes the calls and a **Reviewer** that marks mistakes and drives a feedback loop. It supports **multiple profiles** (accounts), each running either **paper (fake currency)** or **live** mode.

> This project is for learning and building. It is not financial advice. No strategy is guaranteed to earn money, and most simple strategies fail after fees and slippage. Paper trade first.

---

## Goals

- Medium to short term (swing) trading: positions held days to a few weeks
- Small capital: fractional shares, commission-free broker, ~1% risk per trade
- Honest testing: walk-forward validation, no look-ahead, costs included
- A feedback loop that improves rules slowly, with human approval
- Multiple profiles for tracking different strategies or accounts side by side

## Non-goals

- Day trading or high-frequency trading
- Leveraged products, options, forex (at this capital level)
- Auto-editing live rules without human approval
- Deep learning price predictors (rarely hold up out of sample)

---

## Market and Style

- **Market:** US stocks and liquid ETFs (e.g. SPY, QQQ, IWM, sector ETFs)
- **Style:** swing trading on **daily** bars
- **Why:** free clean data, fractional shares, free paper trading, fewer trades means fees matter less, avoids pattern day trader rules, fits a weekly cycle

---

## Tech Stack (all open source, Python)

| Purpose | Tool |
|---|---|
| Market data | `yfinance` (free, unofficial) or `OpenBB` |
| Indicators | `pandas-ta` |
| Backtesting | `vectorbt` or `backtesting.py` |
| Storage | Postgres (Neon), `psycopg`; `DATABASE_URL` in gitignored `.env` |
| Dashboard | Streamlit |
| Scheduling | cron or `APScheduler` |
| Broker SDK | `alpaca-py` (Alpaca offers free paper trading and fractional shares) |
| ML (optional, phase 2) | `scikit-learn`, `LightGBM` |

Alternatives if building from scratch is too slow: Freqtrade (crypto), Lumibot, QuantConnect Lean, NautilusTrader.

---

## Architecture

Two parts connected through a shared Postgres trade log.

```
SheepOfWallStreet/
  CLAUDE.md
  README.md
  pyproject.toml
  config/
    profiles.yaml        # profile definitions (see below)
    strategy.yaml        # versioned rule parameters
  sheep/
    common/              # db models, config loading, utils
    profiles/            # profile + account management
    broker/              # PaperBroker, AlpacaBroker (same interface)
    trader/              # data loader, signals, sizing, orders
    reviewer/            # tagging, weekly report, proposals
    backtest/            # walk-forward backtests, cost models
    dashboard/           # Streamlit app
  data/                  # cached market data (gitignored)
  tests/
  scripts/               # weekly cycle entry points
```

### Part 1: Trader (makes the calls)

- Scans the watchlist after the close and applies the rules
- Sizes positions, sets stop-loss and target at entry
- Sends orders through the broker interface (paper or live)
- Logs every decision with its reasoning: signal values, entry, stop, target, rule version, profile

### Part 2: Reviewer (marks errors, closes the loop)

- Reads closed trades from the log and tags each one
  - Followed rules vs. broke rules (late entry, ignored stop, oversized)
  - Good trade / bad luck vs. bad trade / lucky win
  - Error types: stop too tight, entered against trend, ignored earnings/news date, oversized position
- Produces a weekly report per profile: win rate, average win vs. loss, expectancy, errors by category, performance vs. SPY buy-and-hold
- Writes rule-change **proposals**, never live changes

### Feedback loop

1. Trader logs the trade and the rule version
2. Reviewer tags it and looks for patterns
3. Reviewer writes a proposal (e.g. wider ATR stop, skip trades before earnings)
4. Proposal is backtested (walk-forward) and paper traded
5. Human approves, and Trader gets a new rule version

**Guardrails**
- No auto-editing of live rules
- Minimum sample before a pattern counts (about 20+ trades per pattern)
- Judge process, not just outcome: "did we follow the rules" matters more than win or loss at this size

---

## Profiles and Paper Trading

The app supports **multiple profiles**, like accounts. Each profile is fully isolated: its own cash, positions, trade log, rule version, and reports.

### Profile fields

- `name`: unique profile id
- `mode`: `paper` (fake currency) or `live`
- `starting_capital`: e.g. 100 (fake or real)
- `currency`: display currency (default USD)
- `broker`: `paper` (built-in simulator), `alpaca_paper`, or `alpaca_live`
- `strategy_version`: which rule set this profile uses
- `watchlist`: list of tickers
- `risk`: risk per trade %, max open positions, weekly loss limit %

### Example `config/profiles.yaml`

```yaml
profiles:
  - name: main_paper
    mode: paper
    broker: paper
    starting_capital: 100
    currency: USD
    strategy_version: v1
    watchlist: [SPY, QQQ, IWM, XLK, XLF, XLE, XLV]
    risk:
      risk_per_trade_pct: 1.0
      max_open_positions: 3
      weekly_loss_limit_pct: 3.0

  - name: experiment_momentum_fast
    mode: paper
    broker: paper
    starting_capital: 100
    strategy_version: v2_candidate
    watchlist: [SPY, QQQ]
    risk:
      risk_per_trade_pct: 1.0
      max_open_positions: 2
      weekly_loss_limit_pct: 3.0

  - name: live_small
    mode: live
    broker: alpaca_live
    starting_capital: 100
    strategy_version: v1
    watchlist: [SPY, QQQ]
    risk:
      risk_per_trade_pct: 1.0
      max_open_positions: 2
      weekly_loss_limit_pct: 3.0
```

### Paper broker (fake currency)

- A built-in `PaperBroker` simulates fills with the same interface as the live broker, so strategy code never knows the difference
- Fills at next open (or close) with configurable slippage and fees so results stay realistic
- Tracks fake cash, positions, equity curve, and realized/unrealized P&L per profile
- A profile can be reset or cloned to compare strategy versions on identical starting conditions

### Safety rules for live mode

- Live profiles require an explicit `mode: live` and an environment flag (e.g. `ALLOW_LIVE=1`)
- Live orders need human confirmation by default
- API keys come from environment variables, never committed
- Never auto-promote a paper profile to live

---

## Weekly Lifecycle (per profile)

**Weekend: review and plan**
- Reviewer runs the weekly report and tags trades
- Update the watchlist (10-20 liquid tickers)
- Set the weekly risk budget; review any pending proposals

**Monday: observe**
- Skip the open; check the plan after the first 30-60 minutes
- Enter only trades matching a written setup

**Tuesday to Thursday: execute and manage**
- One scan per day after the close, orders placed for the next day
- Every position has a stop-loss and a target
- No new setups once weekly limits are hit

**Friday: de-risk**
- Per position: close, hold over the weekend, or tighten the stop
- Avoid opening new trades late in the day

---

## Strategy: Phase 1 (rules, not prediction)

- **Trend filter:** go long only when price is above the 200-day moving average
- **Momentum rank:** rank the watchlist by 3-6 month return, pick the top 1-3
- **Entry timing:** pullback (RSI dip) or breakout above a recent high
- **Exit:** ATR-based stop-loss plus trailing stop or trend-break exit
- **Sizing:** risk about 1% of equity per trade (about $1 on $100), fractional shares

## Strategy: Phase 2 (optional ML filter)

- Use ML to rank or filter Phase 1 signals, not to predict exact prices
- Features: returns, volatility, RSI, distance from moving averages, volume
- Model: logistic regression or gradient boosting (LightGBM)
- Target: classification, e.g. "up more than X% in the next 5-10 days"

## Testing Rules

- Walk-forward validation: train on the past, test on the future, repeat. Never shuffle.
- No look-ahead: signals use only data available at that day's close
- Include fees and slippage in every backtest
- Hold out out-of-sample data that is never tuned on
- Benchmark against buy-and-hold SPY. If it can't beat it after costs, don't trade it.

---

## Build Plan

1. Project scaffold, config loading, Postgres schema (profiles, orders, trades, tags, proposals, rule versions)
2. Data loader with caching (yfinance)
3. Phase 1 strategy and indicators
4. Backtester with cost model and walk-forward runner
5. `PaperBroker` and profile management
6. Trader: daily scan, sizing, order logging
7. Trade journal and Streamlit dashboard (per-profile views)
8. Reviewer: tagging, weekly report, proposal generator
9. Scheduler for the weekly cycle
10. Alpaca paper broker adapter, then (optionally, much later) live adapter
11. Phase 2 ML filter

## Database Sketch (Postgres)

- `profiles`: id, name, mode, broker, starting_capital, strategy_version, config_json
- `orders`: id, profile_id, symbol, side, qty, type, status, created_at, filled_at, fill_price, fees
- `trades`: id, profile_id, symbol, entry_ts, exit_ts, entry_price, exit_price, qty, stop, target, pnl, rule_version, reasoning_json
- `trade_tags`: id, trade_id, tag, category, notes, tagged_at
- `equity_snapshots`: id, profile_id, date, cash, equity
- `proposals`: id, profile_id, created_at, description, params_diff_json, backtest_result_json, status (pending/approved/rejected)
- `rule_versions`: id, version, params_json, created_at, approved_by

## Conventions for Claude Code

- Python 3.11+, type hints, `ruff` for linting, `pytest` for tests
- Keep the broker interface identical for paper and live
- Every trade record must store the rule version and reasoning
- Write tests for sizing, stop logic, and the paper broker before wiring the scheduler
- Never commit secrets (`.env`), data, or `DATABASE_URL`
- Never enable live trading by default

## Open Questions

- Final watchlist (ETF-only or include individual stocks)?
- Fill model for paper trading: next open vs. close?
- Alert channel for signals and weekly reports (email, Telegram, none)?
- Dashboard only, or also a CLI?
