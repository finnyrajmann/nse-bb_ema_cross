# NSE BB_EMA_CROSS

Automated NSE swing-trading paper system, deployed as a DigitalOcean Function.
Built as a variant of `200emabb` — same exit/risk framework, different entry
mechanism (BB-lower touch, staged via an intermittent pending list, rather
than a direct EMA-cross check).

## Design summary

- **Watchlist**: pre-vetted for STRUCTURAL uptrend shape only (EMA200
  3-checkpoint slope check), maintained by the separate periodic screener.
  Whether price is currently above EMA200 is re-checked fresh every daily
  run here, not trusted from the (monthly) screener.
- **Entry — staged, two steps**:
  1. A watchlist stock touches BB-lower (price <= BB-lower, 20/2std) AND
     price is currently above EMA200 -> added to the **pending list**
     (`pending_bb_ema_cross.csv`).
  2. Every day, each pending stock is re-checked:
     - Price falls below EMA200 -> **dropped** (no entry taken).
     - 9 EMA crosses above 30 EMA -> **promoted** to a real paper position,
       at that day's price.
     - Otherwise -> stays pending, indefinitely (no fixed timeout).
  This guarantees a real entry always coincides with 9EMA > 30EMA, so the
  EMA9/30 cross-down stop can never fire immediately after entry (unlike a
  direct BB-lower entry would risk).
- **Target exit**: price touches/exceeds Bollinger Band upper (20, 2 std).
- **Stop exit**: 9 EMA currently below 30 EMA, OR price <= 10% below the
  highest daily high since entry (trailing stop). Same-day-both logged as
  `STOP_BOTH`.
- **Below-EMA200 warning**: an open position whose price falls below EMA200
  while 9EMA is still above 30EMA is NOT force-exited — only flagged in the
  daily email. (Pending entries, by contrast, DO get hard-dropped on this
  condition — positions don't have that rule yet, pending more chart review.)
- **Hit/miss split**: PnL% > 3.0 -> hit, PnL% <= 3.0 -> miss.
- **Entry snapshot**: indicator values captured at the moment of
  *promotion* (not the initial BB-lower touch) — independent of
  positions.csv, never trimmed on exit.
- **Position sizing**: Rs.10,000 per trade, same as `200emabb`/BB Trader.

## Files

```
data/
  positions_bb_ema_cross.csv         # open paper positions
  pending_bb_ema_cross.csv           # staged BB-lower touches awaiting
                                      # EMA cross promotion (or EMA200 drop)
  trade_log_hit_bb_ema_cross.csv     # closed trades, PnL% > 3.0
  trade_log_miss_bb_ema_cross.csv    # closed trades, PnL% <= 3.0
  watchlist_bb_ema_cross.csv         # maintained by the separate screener
  entry_snapshot_bb_ema_cross.csv    # indicator values at moment of
                                      # promotion — permanent record
functions/
  project.yml                  # LOCAL ONLY — not checked in, add before DO deploy
  packages/nse_bb_ema_cross/daily_run/__main__.py
```

`pending_bb_ema_cross.csv` uses the same schema as the watchlist file
(Symbol, Industry, IsBanking) — no extra fields, since removal/promotion
logic is condition-based (checked fresh every run), not time-based, so no
stored touch-date is needed.

## Environment variables (set in `functions/project.yml`, not committed)

- `GITHUB_PAT`, `GITHUB_REPO`
- `GMAIL_SENDER`, `GMAIL_APP_PASSWORD`, `GMAIL_RECIPIENT`

## Deployment

Planned for the DO account that previously hosted FnO Trend (freed up
after its retirement) — not the `trading` context account `200emabb`
lives in. Account/cron details TBD.

## Known Gaps / Improvements Backlog

- [ ] Below-EMA200 open-position warning doesn't force an exit — same
      open question as `200emabb`: should it eventually trigger a real
      exit, or stay a flag-only signal? Pending more chart review.
- [ ] No fixed timeout on the pending list — a stock could stay pending
      indefinitely if price hovers above EMA200 without ever crossing.
      Intentional per current design, but worth watching in practice.

## Status

Design locked, code built (Sep 2026). Not yet deployed — pending DO
account details and watchlist population from the screener.
