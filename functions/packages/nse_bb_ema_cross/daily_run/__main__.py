"""
NSE BB_EMA_CROSS — DO Functions Entry Point
=============================================
Uses only requests + standard library (no pip installs needed).
- Yahoo Finance API for price data (OHLC)
- GitHub REST API for reading/writing CSV data
- Gmail SMTP for notifications

Design summary (locked, Sep 2026):
- Watchlist is pre-vetted for STRUCTURAL uptrend shape only (EMA200
  3-checkpoint slope check) by a SEPARATE periodic screener. Whether price
  is currently above EMA200 is re-checked fresh on every daily run here,
  not trusted from the (monthly) screener.
- Entry is staged via an intermittent "pending" list, distinct from
  positions.csv:
    1. A watchlist stock touches BB-lower (price <= BB-lower) AND price is
       currently above EMA200 -> added to the pending list.
    2. Every day, each pending stock is re-checked:
       - If price falls below EMA200 -> dropped from pending (no entry).
       - Elif 9 EMA has crossed above 30 EMA -> PROMOTED: a real paper
         position opens here, at today's price.
       - Otherwise -> stays pending, indefinitely (no fixed timeout).
  This guarantees a real entry always coincides with 9EMA > 30EMA, so the
  EMA9/30 cross-down stop can never fire immediately after entry.
- Target exit: price touches/exceeds BB-upper (20, 2 std).
- Stop exit: EITHER 9 EMA currently below 30 EMA, OR price <= 10% below
  the highest daily HIGH since entry (trailing stop). Same-day-both case
  logged as a distinct STOP_BOTH exit reason.
- Below-EMA200 warning: an open position whose price falls below EMA200
  while 9EMA is still above 30EMA is NOT force-exited — only flagged in
  the email. (Unlike pending entries, which DO get hard-dropped on this
  condition — positions have no such rule yet, by design, pending more
  chart review.)
- Hit/miss split: PnL% > 3.0 -> hit, PnL% <= 3.0 -> miss.
- Entry snapshot: indicator values captured at the moment of PROMOTION
  (not at initial BB-lower touch), independent of positions.csv, never
  trimmed on exit.
- File naming: system code as SUFFIX everywhere —
  watchlist_bb_ema_cross.csv, pending_bb_ema_cross.csv,
  positions_bb_ema_cross.csv, trade_log_hit_bb_ema_cross.csv,
  trade_log_miss_bb_ema_cross.csv, entry_snapshot_bb_ema_cross.csv
- pending_bb_ema_cross.csv uses the same schema as the watchlist file
  (Symbol, Industry, IsBanking) — no extra fields, since removal/promotion
  logic is condition-based, not time-based, so no stored touch-date is
  needed.
"""

import os
import csv
import smtplib
import time
import base64
import math
from io import StringIO
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SYSTEM_CODE      = "bb_ema_cross"

BB_PERIOD        = 20
BB_STD           = 2
EMA_FAST         = 9
EMA_SLOW         = 30
EMA_LONG         = 200
TRAIL_STOP_PCT   = 10.0
POSITION_SIZE    = 10000
SLEEP            = 0.5
HIT_THRESHOLD_PCT = 3.0   # PnL% strictly greater than this -> hit, else -> miss

DATA_PERIOD      = "1y"   # sufficient for 9/30 EMA, BB(20), and EMA200
                           # (EMA200 here is informational/gate use only —
                           # the deep 300-close structural check lives in
                           # the separate watchlist screener)


# ─────────────────────────────────────────────
# YAHOO FINANCE
# ─────────────────────────────────────────────
def fetch_price_bars(symbol, period=DATA_PERIOD):
    """
    Fetch daily OHLC bars for a symbol.
    Returns a list of dicts: {'date': date, 'open', 'high', 'low', 'close'}
    ordered oldest -> newest. Returns None on failure.
    """
    ticker = symbol.upper().strip()
    if not ticker.startswith("^"):
        ticker = ticker + ".NS"

    params = {
        'range':    period,
        'interval': '1d',
        'events':   'history',
    }
    headers = {'User-Agent': 'Mozilla/5.0'}

    for host in ['query1', 'query2']:
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
            r = requests.get(url, params=params, headers=headers, timeout=15)
            data = r.json()
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            opens  = quote['open']
            highs  = quote['high']
            lows   = quote['low']
            closes = quote['close']

            bars = []
            for i, ts in enumerate(timestamps):
                c = closes[i]
                if c is None:
                    continue
                bars.append({
                    'date':  datetime.utcfromtimestamp(ts).date(),
                    'open':  opens[i] if opens[i] is not None else c,
                    'high':  highs[i] if highs[i] is not None else c,
                    'low':   lows[i] if lows[i] is not None else c,
                    'close': c,
                })
            if bars:
                return bars
        except Exception:
            continue
    return None


def calc_ema(values, period):
    """Calculate EMA over a list of closes (oldest -> newest)."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 2)


def calc_bb(closes, period=BB_PERIOD, std_mult=BB_STD):
    """Calculate Bollinger Bands from the last N closes."""
    if len(closes) < period + 2:
        return None
    window   = closes[-period:]
    mean     = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std      = math.sqrt(variance)
    return {
        'bb_mid':   round(mean, 2),
        'bb_upper': round(mean + std_mult * std, 2),
        'bb_lower': round(mean - std_mult * std, 2),
    }


def get_indicators(symbol, period=DATA_PERIOD):
    """Get price + BB + 9/30/200 EMA indicators + raw bars for a symbol."""
    bars = fetch_price_bars(symbol, period)
    if not bars or len(bars) < BB_PERIOD + 2:
        return None

    closes = [b['close'] for b in bars]
    price  = round(closes[-1], 2)

    bb = calc_bb(closes)
    if bb is None:
        return None

    ema9   = calc_ema(closes, EMA_FAST)
    ema30  = calc_ema(closes, EMA_SLOW)
    ema200 = calc_ema(closes, EMA_LONG)

    return {
        'price':    price,
        'bb_upper': bb['bb_upper'],
        'bb_mid':   bb['bb_mid'],
        'bb_lower': bb['bb_lower'],
        'ema9':     ema9,
        'ema30':    ema30,
        'ema200':   ema200,
        'bars':     bars,
    }


def high_since(bars, entry_dt):
    """Highest daily HIGH from entry_dt (inclusive) to the most recent bar."""
    relevant = [b['high'] for b in bars if b['date'] >= entry_dt]
    if not relevant:
        return bars[-1]['high'] if bars else None
    return max(relevant)


# ─────────────────────────────────────────────
# GITHUB REST API
# ─────────────────────────────────────────────
def github_get(repo, path, pat):
    """Read a file from GitHub. Returns (content, sha)."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data    = r.json()
    content = base64.b64decode(data['content']).decode('utf-8')
    return content, data['sha']


def github_put(repo, path, pat, content, sha, message):
    """Write a file to GitHub."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        'Authorization': f'token {pat}',
        'Accept': 'application/vnd.github.v3+json',
    }
    payload = {
        'message': message,
        'content': base64.b64encode(content.encode('utf-8')).decode('utf-8'),
        'sha':     sha,
    }
    r = requests.put(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    return True


def parse_csv(content):
    reader = csv.DictReader(StringIO(content))
    return list(reader)


def to_csv(rows, fieldnames):
    out    = StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# ─────────────────────────────────────────────
# EXIT MONITOR (open positions)
# ─────────────────────────────────────────────
def run_exit(positions, hit_log, miss_log):
    """
    Check every open position for target/stop conditions.
    Returns (exits, holds, warnings, remaining_positions, hit_log, miss_log)
    """
    exits         = []
    holds         = []
    warnings      = []   # below-EMA200 but still EMA9>EMA30 — flag only
    new_positions = []
    hit_log       = list(hit_log)
    miss_log      = list(miss_log)

    for pos in positions:
        symbol      = pos['Symbol']
        entry_price = float(pos['EntryPrice'])
        quantity    = int(pos['Quantity'])
        entry_date  = datetime.strptime(pos['EntryDate'], '%Y-%m-%d')
        track_type  = pos['TrackType']
        capital     = round(entry_price * quantity, 2)
        days_held   = (datetime.now() - entry_date).days

        ind = get_indicators(symbol)
        if ind is None:
            new_positions.append(pos)
            continue

        price = ind['price']

        hi_since = high_since(ind['bars'], entry_date.date())
        trail_stop_price = round(hi_since * (1 - TRAIL_STOP_PCT / 100), 2) if hi_since else None

        target_hit = price >= ind['bb_upper']
        ema_stop   = (ind['ema9'] is not None and ind['ema30'] is not None
                      and ind['ema9'] < ind['ema30'])
        trail_stop = trail_stop_price is not None and price <= trail_stop_price

        exit_type   = None
        exit_reason = None

        if target_hit:
            exit_type   = 'TARGET'
            exit_reason = f"Price at/above BB Upper ({ind['bb_upper']})"
        elif ema_stop and trail_stop:
            exit_type   = 'STOP_BOTH'
            exit_reason = (f"9EMA<30EMA ({ind['ema9']}<{ind['ema30']}) AND "
                            f"trailing stop hit ({trail_stop_price}, "
                            f"high since entry {hi_since})")
        elif ema_stop:
            exit_type   = 'STOP_EMA'
            exit_reason = f"9EMA crossed below 30EMA ({ind['ema9']} < {ind['ema30']})"
        elif trail_stop:
            exit_type   = 'STOP_TRAIL'
            exit_reason = f"Trailing stop hit ({trail_stop_price}, high since entry {hi_since})"

        pnl     = round((price - entry_price) * quantity, 2)
        pnl_pct = round((price - entry_price) / entry_price * 100, 2)

        if exit_type:
            record = {
                'Symbol':     symbol,
                'EntryDate':  pos['EntryDate'],
                'EntryPrice': entry_price,
                'Quantity':   quantity,
                'Capital':    capital,
                'ExitDate':   datetime.now().strftime('%Y-%m-%d'),
                'ExitPrice':  price,
                'PnL':        pnl,
                'PnL%':       pnl_pct,
                'DaysHeld':   days_held,
                'ExitReason': exit_reason,
                'TrackType':  track_type,
            }
            exits.append(record)
            if pnl_pct > HIT_THRESHOLD_PCT:
                hit_log.append(record)
            else:
                miss_log.append(record)
        else:
            new_positions.append(pos)
            holds.append({
                'Symbol':     symbol,
                'EntryPrice': entry_price,
                'Price':      price,
                'PnL':        pnl,
                'PnL%':       pnl_pct,
                'DaysHeld':   days_held,
            })

            if (ind['ema200'] is not None and price < ind['ema200']
                    and ind['ema9'] is not None and ind['ema30'] is not None
                    and ind['ema9'] > ind['ema30']):
                warnings.append({
                    'Symbol':  symbol,
                    'Price':   price,
                    'EMA200':  ind['ema200'],
                    'EMA9':    ind['ema9'],
                    'EMA30':   ind['ema30'],
                    'PnL%':    pnl_pct,
                })

    return exits, holds, warnings, new_positions, hit_log, miss_log


# ─────────────────────────────────────────────
# PENDING LIST REVIEW (existing pending entries)
# ─────────────────────────────────────────────
def run_pending_review(pending, positions, entry_snapshots):
    """
    Re-check every symbol already in the pending list:
      - price < EMA200            -> dropped (no entry)
      - 9EMA crosses above 30EMA  -> PROMOTED (real position opens)
      - otherwise                 -> stays pending
    Returns (still_pending, dropped, promoted, positions, entry_snapshots)
    """
    still_pending   = []
    dropped         = []
    promoted        = []
    positions       = list(positions)
    entry_snapshots = list(entry_snapshots)

    for row in pending:
        symbol = row['Symbol'].strip()
        ind = get_indicators(symbol)

        if ind is None:
            still_pending.append(row)   # can't evaluate — leave as-is
            time.sleep(SLEEP)
            continue

        if ind['ema200'] is None or ind['price'] <= ind['ema200']:
            dropped.append({
                'Symbol': symbol,
                'Price':  ind['price'],
                'EMA200': ind['ema200'],
            })
            time.sleep(SLEEP)
            continue

        if (ind['ema9'] is not None and ind['ema30'] is not None
                and ind['ema9'] > ind['ema30']):
            quantity   = max(1, int(POSITION_SIZE / ind['price']))
            entry_date = datetime.now().strftime('%Y-%m-%d')

            positions.append({
                'Symbol':     symbol,
                'EntryDate':  entry_date,
                'EntryPrice': ind['price'],
                'Quantity':   quantity,
                'TrackType':  'Paper',
            })
            entry_snapshots.append({
                'Symbol':    symbol,
                'EntryDate': entry_date,
                'Price':     ind['price'],
                'EMA9':      ind['ema9'],
                'EMA30':     ind['ema30'],
                'EMA200':    ind['ema200'],
                'BBUpper':   ind['bb_upper'],
            })
            promoted.append({
                'Symbol':   symbol,
                'Industry': row.get('Industry', ''),
                'Price':    ind['price'],
                'EMA9':     ind['ema9'],
                'EMA30':    ind['ema30'],
                'BB Upper': ind['bb_upper'],
            })
            print(f"  Promoted: {symbol} @ Rs.{ind['price']} "
                  f"(9EMA {ind['ema9']} > 30EMA {ind['ema30']})")
        else:
            still_pending.append(row)

        time.sleep(SLEEP)

    return still_pending, dropped, promoted, positions, entry_snapshots


# ─────────────────────────────────────────────
# WATCHLIST SCAN (new BB-lower touches -> pending)
# ─────────────────────────────────────────────
def run_watchlist_scan(watchlist, positions, pending):
    """
    Scan the full watchlist for fresh BB-lower touches. A symbol already in
    positions or already pending is skipped. Adds new touches to pending.
    Returns (new_pending_entries, updated_pending)
    """
    open_symbols   = {p['Symbol'] for p in positions}
    pending_symbols = {p['Symbol'] for p in pending}
    new_pending    = []
    pending        = list(pending)

    for row in watchlist:
        symbol = row['Symbol'].strip()
        if symbol in open_symbols or symbol in pending_symbols:
            continue

        ind = get_indicators(symbol)
        if ind is None:
            time.sleep(SLEEP)
            continue

        # Daily freshness gate: price must be above EMA200 TODAY (the
        # screener only guarantees the structural slope shape — see
        # module docstring).
        if ind['ema200'] is None or ind['price'] <= ind['ema200']:
            time.sleep(SLEEP)
            continue

        if ind['price'] <= ind['bb_lower']:
            pending_row = {
                'Symbol':    symbol,
                'Industry':  row.get('Industry', ''),
                'IsBanking': row.get('IsBanking', ''),
            }
            pending.append(pending_row)
            pending_symbols.add(symbol)
            new_pending.append({
                'Symbol':    symbol,
                'Industry':  row.get('Industry', ''),
                'Price':     ind['price'],
                'BB Lower':  ind['bb_lower'],
                'EMA9':      ind['ema9'],
                'EMA30':     ind['ema30'],
                'EMA200':    ind['ema200'],
            })
            print(f"  New pending: {symbol} touched BB-lower @ Rs.{ind['price']}")

        time.sleep(SLEEP)

    return new_pending, pending


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────
def send_email(exits, new_pending, promoted, dropped, pending_full, holds,
               warnings, alltime_pnl, alltime_count, hit_count, miss_count):
    sender    = os.environ.get('GMAIL_SENDER')
    password  = os.environ.get('GMAIL_APP_PASSWORD')
    recipient = os.environ.get('GMAIL_RECIPIENT')
    repo_name = os.environ.get('GITHUB_REPO')
    today     = datetime.now().strftime('%d %b %Y')
    subject   = (f"NSE BB_EMA_CROSS — {today} | {len(promoted)} promoted | "
                 f"{len(pending_full)} pending | {len(holds)} open")

    def table_style():
        return 'border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;'

    def th_style():
        return 'background:#2c3e50;color:#fff;padding:8px 12px;text-align:left;'

    def td_style(align='left'):
        return f'padding:7px 12px;border-bottom:1px solid #eee;text-align:{align};'

    def section_header(title):
        return f'<h3 style="color:#2c3e50;margin:24px 0 8px 0;">{title}</h3>'

    hits   = [e for e in exits if e['PnL%'] > HIT_THRESHOLD_PCT]
    misses = [e for e in exits if e['PnL%'] <= HIT_THRESHOLD_PCT]

    html = f'''
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
    <h2 style="background:#2c3e50;color:#fff;padding:14px 18px;margin:0;border-radius:4px 4px 0 0;">
        NSE BB_EMA_CROSS — {today}
    </h2>
    '''

    # EXITS
    html += section_header(
        f'Exits Today ({len(exits)}) &mdash; {len(hits)} hit / {len(misses)} miss'
    ) if exits else section_header('Exits: None today')
    if exits:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'P&L %', 'P&L Rs', 'Days', 'Reason']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in exits:
            icon = '[H]' if r['PnL%'] > HIT_THRESHOLD_PCT else '[M]'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">Rs.{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
                <td style="{td_style()}">{r['ExitReason']}</td>
            </tr>'''
        html += '</tbody></table>'

    # NEW PENDING (fresh BB-lower touches today)
    html += section_header(f'New Pending — BB-Lower Touch Today ({len(new_pending)})') \
        if new_pending else section_header('New Pending: None today')
    if new_pending:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Industry', 'Price Rs', 'BB Lower Rs', '9EMA', '30EMA']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for e in new_pending:
            html += f'''<tr>
                <td style="{td_style()}"><b>{e['Symbol']}</b></td>
                <td style="{td_style()}">{e['Industry']}</td>
                <td style="{td_style('right')}">Rs.{e['Price']}</td>
                <td style="{td_style('right')}">Rs.{e['BB Lower']}</td>
                <td style="{td_style('right')}">{e['EMA9']}</td>
                <td style="{td_style('right')}">{e['EMA30']}</td>
            </tr>'''
        html += '</tbody></table>'

    # PROMOTED (pending -> real entry today)
    if promoted:
        html += section_header(f'Promoted to Positions Today ({len(promoted)})')
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Industry', 'Price Rs', '9EMA', '30EMA', 'BB Upper Rs']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for e in promoted:
            html += f'''<tr>
                <td style="{td_style()}"><b>{e['Symbol']}</b></td>
                <td style="{td_style()}">{e['Industry']}</td>
                <td style="{td_style('right')}">Rs.{e['Price']}</td>
                <td style="{td_style('right')}">{e['EMA9']}</td>
                <td style="{td_style('right')}">{e['EMA30']}</td>
                <td style="{td_style('right')}">Rs.{e['BB Upper']}</td>
            </tr>'''
        html += '</tbody></table>'

    # DROPPED (pending -> removed, price fell below EMA200)
    if dropped:
        html += section_header(f'Dropped from Pending Today ({len(dropped)}) — price fell below EMA200')
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Price Rs', 'EMA200 Rs']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for d in dropped:
            html += f'''<tr>
                <td style="{td_style()}"><b>{d['Symbol']}</b></td>
                <td style="{td_style('right')}">Rs.{d['Price']}</td>
                <td style="{td_style('right')}">Rs.{d['EMA200']}</td>
            </tr>'''
        html += '</tbody></table>'

    # FULL PENDING LIST (current contents, every run — not just today's changes)
    html += section_header(f'Pending List — Full ({len(pending_full)})') \
        if pending_full else section_header('Pending List: Empty')
    if pending_full:
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Industry', 'Price Rs', '9EMA', '30EMA', 'EMA200 Rs']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for p in pending_full:
            html += f'''<tr>
                <td style="{td_style()}"><b>{p['Symbol']}</b></td>
                <td style="{td_style()}">{p.get('Industry', '')}</td>
                <td style="{td_style('right')}">{p.get('Price', 'N/A')}</td>
                <td style="{td_style('right')}">{p.get('EMA9', 'N/A')}</td>
                <td style="{td_style('right')}">{p.get('EMA30', 'N/A')}</td>
                <td style="{td_style('right')}">{p.get('EMA200', 'N/A')}</td>
            </tr>'''
        html += '</tbody></table>'

    # OPEN POSITIONS
    if holds:
        total_pnl = sum(r['PnL'] for r in holds)
        pnl_color = '#27ae60' if total_pnl >= 0 else '#e74c3c'
        html += section_header(
            f'Open Positions ({len(holds)}) &nbsp;|&nbsp; '
            f'Total P&L: <span style="color:{pnl_color}">Rs.{total_pnl:+.0f}</span>'
        )
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['', 'Symbol', 'Entry Rs', 'Price Rs', 'P&L %', 'P&L Rs', 'Days']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for r in holds:
            icon = '[+]' if r['PnL'] >= 0 else '[-]'
            html += f'''<tr>
                <td style="{td_style()}">{icon}</td>
                <td style="{td_style()}"><b>{r['Symbol']}</b></td>
                <td style="{td_style('right')}">Rs.{r['EntryPrice']:.2f}</td>
                <td style="{td_style('right')}">Rs.{r['Price']:.2f}</td>
                <td style="{td_style('right')}">{r['PnL%']:+.2f}%</td>
                <td style="{td_style('right')}">Rs.{r['PnL']:+.0f}</td>
                <td style="{td_style('right')}">{r['DaysHeld']}d</td>
            </tr>'''
        html += '</tbody></table>'
    else:
        html += section_header('Open Positions: None')

    # BELOW-EMA200 WARNING (open positions only — pending has hard removal)
    if warnings:
        html += section_header(f'⚠ Below EMA200, Still EMA-Uptrend ({len(warnings)})')
        html += f'<table style="{table_style()}"><thead><tr>'
        for col in ['Symbol', 'Price Rs', 'EMA200 Rs', '9EMA', '30EMA', 'P&L %']:
            html += f'<th style="{th_style()}">{col}</th>'
        html += '</tr></thead><tbody>'
        for w in warnings:
            html += f'''<tr>
                <td style="{td_style()}"><b>{w['Symbol']}</b></td>
                <td style="{td_style('right')}">Rs.{w['Price']:.2f}</td>
                <td style="{td_style('right')}">Rs.{w['EMA200']:.2f}</td>
                <td style="{td_style('right')}">{w['EMA9']}</td>
                <td style="{td_style('right')}">{w['EMA30']}</td>
                <td style="{td_style('right')}">{w['PnL%']:+.2f}%</td>
            </tr>'''
        html += '</tbody></table>'

    # CUMULATIVE TRADE LOG P&L
    if alltime_pnl is not None:
        at_color = '#27ae60' if alltime_pnl >= 0 else '#e74c3c'
        hit_rate = f'{(hit_count / alltime_count * 100):.0f}%' if alltime_count else 'N/A'
        html += section_header('All-Time Trade Log')
        html += f'''
        <table style="{table_style()}"><tbody>
            <tr>
                <td style="{td_style()}">Closed trades</td>
                <td style="{td_style('right')}">{alltime_count} ({hit_count} hit / {miss_count} miss, {hit_rate} hit rate)</td>
            </tr>
            <tr>
                <td style="{td_style()}">Cumulative P&amp;L</td>
                <td style="{td_style('right')}"><span style="color:{at_color}"><b>Rs.{alltime_pnl:+,.0f}</b></span></td>
            </tr>
        </tbody></table>
        '''

    # FOOTER
    html += f'''
    <p style="margin-top:24px;font-size:12px;color:#888;">
        <a href="https://github.com/{repo_name}/blob/master/data/trade_log_hit_{SYSTEM_CODE}.csv" style="color:#2c3e50;">
            View hit log
        </a> &nbsp;|&nbsp;
        <a href="https://github.com/{repo_name}/blob/master/data/trade_log_miss_{SYSTEM_CODE}.csv" style="color:#2c3e50;">
            View miss log
        </a><br>
        — NSE BB_EMA_CROSS (automated)
    </p>
    </div>
    '''

    msg = MIMEMultipart()
    msg['From']    = sender
    msg['To']      = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    print(f"  Email sent to {recipient}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main(args):
    print("\n" + "="*50)
    print("  NSE BB_EMA_CROSS — DO Functions Run")
    print("="*50)

    pat       = os.environ.get('GITHUB_PAT')
    repo_name = os.environ.get('GITHUB_REPO')

    pos_path      = f'data/positions_{SYSTEM_CODE}.csv'
    hit_log_path  = f'data/trade_log_hit_{SYSTEM_CODE}.csv'
    miss_log_path = f'data/trade_log_miss_{SYSTEM_CODE}.csv'
    wl_path       = f'data/watchlist_{SYSTEM_CODE}.csv'
    pending_path  = f'data/pending_{SYSTEM_CODE}.csv'
    snap_path     = f'data/entry_snapshot_{SYSTEM_CODE}.csv'

    try:
        # Load data from GitHub
        print("\n[1/6] Loading data from GitHub...")
        pos_content, pos_sha       = github_get(repo_name, pos_path, pat)
        hitlog_content, hit_sha    = github_get(repo_name, hit_log_path, pat)
        misslog_content, miss_sha  = github_get(repo_name, miss_log_path, pat)
        wl_content, _              = github_get(repo_name, wl_path, pat)
        pending_content, pend_sha  = github_get(repo_name, pending_path, pat)
        snap_content, snap_sha     = github_get(repo_name, snap_path, pat)

        positions       = parse_csv(pos_content)
        hit_log         = parse_csv(hitlog_content)
        miss_log        = parse_csv(misslog_content)
        watchlist       = parse_csv(wl_content)
        pending         = parse_csv(pending_content)
        entry_snapshots = parse_csv(snap_content)
        print(f"      {len(positions)} open positions | {len(pending)} pending | "
              f"{len(watchlist)} watchlist stocks")

        # Exit monitor
        print("\n[2/6] Exit Monitor...")
        exits, holds, warnings, positions, hit_log, miss_log = run_exit(positions, hit_log, miss_log)
        print(f"      {len(exits)} exit(s) | {len(holds)} holding | {len(warnings)} below-EMA200 warning(s)")

        # Pending review — promote or drop existing pending entries
        print("\n[3/6] Pending Review...")
        pending, dropped, promoted, positions, entry_snapshots = run_pending_review(
            pending, positions, entry_snapshots)
        print(f"      {len(promoted)} promoted | {len(dropped)} dropped | {len(pending)} still pending")

        # Watchlist scan — new BB-lower touches
        print("\n[4/6] Watchlist Scan (new BB-lower touches)...")
        new_pending, pending = run_watchlist_scan(watchlist, positions, pending)
        print(f"      {len(new_pending)} new pending signal(s)")

        # Build a live-refreshed view of the full pending list for the email
        # (re-fetch is already done in run_pending_review/scan for the ones
        # touched this run; for a lightweight display, just show what's on
        # file — Symbol/Industry always available, indicator columns filled
        # in only for rows freshly touched/reviewed this run).
        pending_display = []
        promoted_symbols_today = {p['Symbol'] for p in promoted}
        new_pending_lookup = {p['Symbol']: p for p in new_pending}
        for row in pending:
            sym = row['Symbol']
            if sym in new_pending_lookup:
                np = new_pending_lookup[sym]
                pending_display.append({
                    'Symbol': sym, 'Industry': row.get('Industry', ''),
                    'Price': np['Price'], 'EMA9': np['EMA9'],
                    'EMA30': np['EMA30'], 'EMA200': np['EMA200'],
                })
            else:
                pending_display.append({
                    'Symbol': sym, 'Industry': row.get('Industry', ''),
                    'Price': 'N/A', 'EMA9': 'N/A', 'EMA30': 'N/A', 'EMA200': 'N/A',
                })

        # Sync to GitHub
        print("\n[5/6] Syncing to GitHub...")
        commit_msg = f"Auto-update — {datetime.now().strftime('%Y-%m-%d')}"

        pos_fields     = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 'TrackType']
        log_fields     = ['Symbol', 'EntryDate', 'EntryPrice', 'Quantity', 'Capital',
                           'ExitDate', 'ExitPrice', 'PnL', 'PnL%', 'DaysHeld',
                           'ExitReason', 'TrackType']
        pending_fields = ['Symbol', 'Industry', 'IsBanking']
        snap_fields    = ['Symbol', 'EntryDate', 'Price', 'EMA9', 'EMA30', 'EMA200', 'BBUpper']

        github_put(repo_name, pos_path, pat,
                   to_csv(positions, pos_fields), pos_sha, commit_msg)
        github_put(repo_name, hit_log_path, pat,
                   to_csv(hit_log, log_fields), hit_sha, commit_msg)
        github_put(repo_name, miss_log_path, pat,
                   to_csv(miss_log, log_fields), miss_sha, commit_msg)
        github_put(repo_name, pending_path, pat,
                   to_csv(pending, pending_fields), pend_sha, commit_msg)
        github_put(repo_name, snap_path, pat,
                   to_csv(entry_snapshots, snap_fields), snap_sha, commit_msg)

        # Cumulative trade-log P&L
        alltime_pnl = (sum(float(r['PnL']) for r in hit_log) +
                       sum(float(r['PnL']) for r in miss_log))
        alltime_count = len(hit_log) + len(miss_log)

        # Send email
        print("\n[6/6] Sending email...")
        send_email(exits, new_pending, promoted, dropped, pending_display, holds,
                   warnings, alltime_pnl, alltime_count, len(hit_log), len(miss_log))

        print("\n  Done.\n")
        return {"statusCode": 200, "body": "Pipeline complete"}

    except Exception as e:
        import traceback
        print(f"\n  ERROR: {str(e)}")
        print(traceback.format_exc())
        return {"statusCode": 500, "body": str(e)}
