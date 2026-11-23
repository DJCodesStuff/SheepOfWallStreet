# Decisions

Each entry: what was decided, what was rejected, and what it costs.

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
