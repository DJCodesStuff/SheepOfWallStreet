# Decisions

Each entry: what was decided, what was rejected, and what it costs.

## Entries are sized and placed at the next open, not the signal close (2026-11-23)

- **Decided:** The Trader runs twice each weekday (GitHub Actions, `.github/workflows/trader.yml`).
  The **evening run** (22:15 UTC, after the 16:30 NY bar cutoff) fills orders, closes trades,
  trails stops, queues trend-break exits and logs signals. It places no entries. The **open
  run** (14:45 UTC = 09:45 NY in winter, 10:45 in summer; anything 09:30-16:00 NY counts)
  re-scans the previous close and reads today's opening price (yfinance). It sizes each entry
  at that price, so the stop risks exactly `risk_per_trade_pct`, then runs the risk gate. The
  paper broker now lets an order placed before the 16:00 close fill on that day's bar, so the
  entry fills at the open it was sized from. This is what the backtester already did, so the
  Trader and backtest now agree (tested, including a gap-up case).
- **Why:** On 2026-11-05 main_paper's XLV and XLF entries gapped up 1.0-1.3% at the open. They
  were sized at the signal close against a stop fixed at that close, so they risked
  $1.28 and $1.44 instead of $1.00.
- **Rejected:** A separate gap filter (skip when the open is >0.5 ATR above the close). The
  existing reward:risk minimum already voids big gaps, because the target stays fixed while
  the risk grows. Keeping the old evening sizing plus a gap buffer was also rejected: it would
  still drift from the backtester.
- **Cost:** A paper entry fills at the open even when the run happens later (GitHub can start
  cron jobs an hour late). The run only ever reads the opening price, so nothing later leaks
  in, but a live adapter will fill at market instead and must model that. The open run
  makes one yfinance request per candidate. A symbol whose opening price can't be fetched is
  skipped for the day. The project's lifecycle note "Monday: skip the open" now applies only
  to manual trading; the bot enters on Mondays too.

## NYSE holiday calendar from pandas' holiday rules (2026-11-23)

- **Decided:** `NYSEHolidays` in `sheep/trader/data.py` defines NYSE full-day closures with
  pandas' built-in holiday rules. These cover New Year's Day (Sunday moves to Monday, and
  there's no Friday Dec 31 closure), MLK, Presidents, Good Friday, Memorial, Juneteenth (from
  2022), Independence Day, Labor, Thanksgiving and Christmas (Saturday moves to Friday,
  Sunday to Monday). The Trader treats a holiday like a weekend: no open run, and the evening
  run can happen at any hour. The stale-signal check expects the previous *trading* day, so
  the morning after a holiday acts on the pre-holiday close instead of calling it stale.
  `last_final_close` steps back to the last session, so a weekend or holiday no longer causes
  a needless data refetch or a holiday-dated equity snapshot. Test bars now use NYSE sessions.
- **Rejected:** `pandas_market_calendars` / `exchange_calendars`. They're more complete but add
  a dependency for 10 stable rules.
- **Cost:** One-off closures (national days of mourning, weather) aren't known and must be
  added by hand when announced. If one is missed, the open run finds no opening price and
  skips each candidate, which is harmless. Early 13:00 closes count as full sessions.

## Open risk across positions is capped by the weekly loss limit (2026-11-23)

- **Decided:** The pre-trade gate (`check_entry`, shared by the Trader and backtester) takes
  `room`: equity minus the weekly loss floor, minus what every open position (and unfilled
  entry) loses if its current stop fills. An entry whose own stop loss exceeds `room` is
  blocked. Each new entry in the same run uses up room. The backtester's weekly-limit check
  moved from the close to the open, matching the Trader, which also fixes it using the old
  week's start on Mondays.
- **Why:** main_paper opened two positions on the same day with about 2.7% at risk against a
  3% weekly limit. Both stopped out (one through a gap) for -3.05%. The old limit only blocked
  entries after the loss had happened.
- **Rejected:** Shrinking the entry to fit the remaining room. The gate blocks and never
  resizes, as it did before.
- **Cost:** Fewer entries in a losing week, and a second same-day entry is often blocked when
  the limit is tight. Gaps through stops can still exceed the limit; this caps planned risk only.
- **Live action (2026-11-23):** At the user's request, main_paper's pending XLF bracket (orders
  41-43, placed 2026-11-20 under the old close-based sizing) was cancelled before it could
  fill. At the 55.57 open it would have risked about $1.45 (1.5%), and the new gate would
  have blocked it (reward:risk 0.99 < 1.0).

## Open positions are marked at the latest final daily close (2026-11-23)

- **Decided:** The dashboard Overview values each open position at its symbol's latest final
  daily close (`load_bars`, cached 5 minutes). It shows open P&L, % vs entry, open R (against
  the *original* stop), P&L if the stop in force fills, distance to stop and target, and a
  stop→target progress bar, plus a per-symbol price chart with entry, target and stop levels.
  "If stopped" uses the trailing `current_stop` when set, else the entry stop.
- **Rejected:** Intraday/live quotes (no free reliable feed, and the system trades on daily
  bars); netting exit slippage and fees into open P&L (kept gross so it matches the price
  move; fees show up when the trade closes).
- **Cost:** Marks lag intraday moves by up to a session. Open P&L is slightly optimistic
  (before exit costs), and "if stopped" assumes an exact fill: a gap down fills worse. A
  symbol whose bars fail to load shows blank marks and a warning rather than breaking the page.

## Base rules v1_1: no breakout entries above RSI 70 (2026-11-24)

- **Decided:** New rule version `v1_1` = v1 plus `breakout_rsi_max: 70`: a breakout only counts
  when RSI(14) is at most 70. Pullback entries, stops and targets are unchanged. main_paper
  moves from v1 to v1_1. live_small stays on v1 until v1_1 has a paper record (feedback loop
  step 4). The new setting defaults to 100 (off), so v1 and v2_candidate trade exactly as before.
- **Why (live):** main_paper's first five days lost 3.05%. Both trades were v1 breakouts
  bought overextended: XLV at RSI 77, 20% above its 200-day MA, and XLF at RSI 71. Both
  stopped out within 1-2 days. Five trades are far below the 20-trade minimum, so the rule was
  adopted on backtest evidence, not on these trades.
- **Why (backtest):** Walk-forward on main_paper's watchlist, test windows 1997-01 to 2024-01,
  5 bps slippage. v1: 674 trades, Sharpe 0.40, +91%, max DD -21%. v1_1: 554 trades, Sharpe 0.53,
  +121%, max DD -21%, avg R 0.11 -> 0.15. Caps of 65-80 all beat v1, so 70 is not a lone spike
  (60 is too tight: Sharpe 0.39). On SPY/QQQ only, Sharpe goes 0.44 -> 0.52 and DD -18% -> -14%.
- **Holdout check (run once, 2024-11-23 onward):** no difference. v1 and v1_1 both returned
  +0.1% (Sharpe 0.04) while SPY buy-and-hold made +23.6% (Sharpe 0.70). Neither version beats
  SPY over the walk-forward period either (SPY Sharpe 0.53, +912%), so per the testing rules
  this is still a paper-only strategy.
- **Rejected (tested, not adopted):** A momentum floor (skip a top-ranked symbol whose own
  lookback return is negative). It would have blocked experiment_momentum_fast's three losing
  SPY pullbacks (momentum -2% to -5%), but it cut v2_candidate's Sharpe from 0.44 to 0.35:
  negative-momentum dips in SPY/QQQ have historically paid off, so those losses read as bad
  luck. A trend buffer (close at least 0.5-1 ATR above the 200-day MA, since two SPY entries
  sat 0.4-0.8% above it and one exited on a trend break 2 days later). It cut drawdown at 1 ATR
  (-21% -> -13%) but lowered return and Sharpe.
- **Cost:** About 18% fewer trades. Strong trends that keep running with RSI above 70 are missed.
  The dashboard shows versions saved without the setting as 100 (off).
- **Live action (2026-11-24):** `python -m sheep.common.db` was run against Neon, inserting
  rule version v1_1 and switching main_paper to it (no open trades or pending orders at the time).
  The next scheduled Trader run is the first on v1_1.
