# NSE BB_EMA_CROSS

Automated NSE swing-trading paper system, deployed as a DigitalOcean Function.
Built as a variant of `200emabb`. It uses the same risk framework and a different
entry mechanism: a Bollinger-band lower touch that is staged through a pending
list, rather than a direct EMA-cross check.

## Design summary

- **Watchlist**: pre-vetted for STRUCTURAL uptrend shape only (EMA200
  3-checkpoint slope check), maintained by the separate periodic screener.
  Whether price is above EMA200 is re-checked fresh on every daily run, not
  trusted from the screener.
- **Entry: staged, two steps**
  1. **Touch**: a watchlist stock whose newest bar closes at or below BB-lower
     (20 period, 2 std) AND is above EMA200 is added to the **pending list**
     (`pending_bb_ema_cross.csv`). The row records `AddedDate`, the date of the
     bar that touched BB-lower.
  2. **Review (every run)**: each pending stock is re-checked.
     - Price below EMA200 -> **dropped**, no entry taken.
     - 9 EMA crosses above 30 EMA on any bar after `AddedDate` -> **promoted**
       to a paper position. The entry price is the close of the cross bar, and
       EntryDate is the next trading session after that bar.
     - Otherwise -> stays pending, with no fixed timeout.
  Because the cross is required, a real entry always coincides with 9EMA > 30EMA,
  so the 9/30 cross-down stop cannot fire right after entry.
- **Late runs**: if a run is late (missed run or stale data), the entry is
  backdated to the cross bar and the exit rules are replayed bar by bar up to
  the latest bar. A position that should already have exited goes straight to
  the hit/miss log.
- **Exit: trailing stop only**
  - Exit when the daily CLOSE is at or below 3% under the highest daily candle
    BODY TOP since entry. Body top = max(open, close), so wicks are ignored.
    The peak counts sessions from EntryDate onward.
  - The BB-upper target and the 9EMA < 30EMA stop are both switched OFF
    (`USE_BB_TARGET = False`, `USE_EMA_STOP = False`). They can be re-enabled
    by changing those flags. If the EMA stop is re-enabled, same-day
    trailing-stop-plus-EMA-stop exits are logged as `STOP_BOTH`.
- **Below-EMA200 warning (positions)**: an open position whose close is below
  EMA200 while 9EMA is still above 30EMA is NOT force-exited. It is only flagged
  in the email. Pending stocks, by contrast, are dropped on this condition.
- **Hit/miss split**: PnL% > 3.0 -> hit, PnL% <= 3.0 -> miss.
- **Entry snapshot**: indicator values (EMA9, EMA30, EMA200, BB upper) captured
  at promotion, not at the initial BB-lower touch. Independent of positions,
  never trimmed on exit.
- **Position sizing**: Rs.10,000 per trade, same as `200emabb` and BB Trader.

## Trading calendar and data quality

- **Calendar**: `data/nse_holidays.csv` (Date, Type, Description) is read from
  the GitHub repo at each run. Type is `HOLIDAY` or `COVERS_THROUGH`. A day is a
  session if it is Mon-Fri and not a listed holiday.
- **Non-session days**: the run exits early. On a weekday holiday a short
  holiday email is sent. Weekends exit silently. No positions, pending list or
  logs are touched.
- **Last completed session**: today's bar counts as final only after 16:00 IST
  on a session day. NIFTY's newest bar is used as a cross-check against the
  calendar, and a mismatch is flagged in the email.
- **Stale data**: a stock whose newest bar is older than the last completed
  session is skipped for new BB-lower touches and listed in the email. Pending
  stocks with stale data are still evaluated on the data available.
- **Calendar warnings**: the email flags a calendar that is missing, not
  confirmed through today, or expiring within 14 days.
- **Legacy pending rows**: rows with no `AddedDate` are rebuilt once by
  simulating the rules from `SYSTEM_START_DATE` (2026-09-17), or from the
  stock's last logged exit if that is later. Rebuilt rows are listed in the
  email.

## Files

```
data/
  nse_holidays.csv                   # trading calendar, read from the repo
  positions_bb_ema_cross.csv         # open paper positions
                                      # (Symbol, EntryDate, EntryPrice, Quantity, TrackType)
  pending_bb_ema_cross.csv           # staged BB-lower touches
                                      # (Symbol, Industry, IsBanking, AddedDate)
  trade_log_hit_bb_ema_cross.csv     # closed trades, PnL% > 3.0
  trade_log_miss_bb_ema_cross.csv    # closed trades, PnL% <= 3.0
  watchlist_bb_ema_cross.csv         # maintained by the separate screener
  entry_snapshot_bb_ema_cross.csv    # indicator values at promotion (permanent record)
                                      # (Symbol, EntryDate, Price, EMA9, EMA30, EMA200, BBUpper)
functions/
  project.yml                        # LOCAL ONLY, not checked in
  packages/nse_bb_ema_cross/daily_run/__main__.py
```

## Run options

Invoke with a JSON body:

- `{"dry_run": true}`: runs everything except writes to GitHub. The email subject
  is prefixed `[DRY RUN]`.
- `{"force": true}`: runs on a non-session day. Use with care, since it writes to
  the repo and sends a real email.

## Environment variables (set in `functions/project.yml`, not committed)

- `GITHUB_PAT`, `GITHUB_REPO`
- `GMAIL_SENDER`, `GMAIL_APP_PASSWORD`, `GMAIL_RECIPIENT`

## Deployment

Deployment account and schedule: TBD.

## Known gaps and backlog

- [ ] Below-EMA200 open-position warning does not force an exit. Same open
      question as `200emabb`: should it eventually trigger a real exit, or stay
      a flag-only signal? Pending chart review.
- [ ] No fixed timeout on the pending list. A stock can stay pending
      indefinitely if price hovers above EMA200 without a 9/30 cross. This is
      intentional under the current design but should be watched in practice.
- [ ] Confirm the entry convention: entry price is the cross-bar close, with
      EntryDate on the next session. The earlier README said "that day's price".
- [ ] BB-upper target and 9/30 stop are disabled. Confirm this is still the
      intended design before re-enabling either.

## Change log

- 21 Oct 2026: exit switched to a 3% trailing stop under the highest daily HIGH
  since entry. BB-upper target and 9/30 stop switched off.
- Nov 2026: trailing stop measured from the highest daily candle BODY TOP
  instead of the highest HIGH. Trading-calendar gate, stale-data handling,
  dry-run and force options added.
