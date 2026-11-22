
"""Alpaca crypto signal scanner. NO ORDER EXECUTION; designed for paper testing.

Requires ALPACA_PAPER_KEY and ALPACA_PAPER_SECRET in environment.
Optional TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
This scanner NEVER sends trade orders, regardless of ENABLE_ORDERS.
"""
import os
import sys
import math
import json
import requests
from datetime import datetime, timedelta, timezone

KEY = os.environ['ALPACA_PAPER_KEY']
SECRET = os.environ['ALPACA_PAPER_SECRET']
HEAD = {'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SECRET}
PAPER_API = 'https://paper-api.alpaca.markets'
DATA_API = 'https://data.alpaca.markets'
CORE_SYMBOLS = ('BTC/USD', 'ETH/USD', 'SOL/USD')
MAX_MARKETS = max(3, min(40, int(os.getenv('MAX_MARKETS', '20'))))
SCAN_ALL_CRYPTO = os.getenv('SCAN_ALL_CRYPTO', 'true').lower() == 'true'
# No live or paper orders: this scanner only produces candidate signals.
ENABLE_ORDERS = False
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
MAX_TRADE_USD = min(100.0, max(0.0, float(os.getenv('MAX_TRADE_USD', '100'))))
MIN_SCORE = max(0, min(100, int(os.getenv('MIN_CONFIDENCE_SCORE', '70'))))
DAILY_LOSS_PCT = min(2.0, max(0.0, float(os.getenv('MAX_DAILY_LOSS_PCT', '2'))))
# Limit risk even at the highest score; not a probability-of-success estimate.
RISK_TIERS = ((95, .01, 100), (90, .0075, 75), (80, .005, 50), (70, .0025, 25))
RUN_RESULTS = []


def notify(message):
    print(message, flush=True)
    if TOKEN and CHAT:
        try:
            r = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                              json={'chat_id': CHAT, 'text': message[:4000]}, timeout=12)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f'Telegram notification failed: {exc}', file=sys.stderr)


def api(root, path, params=None):
    r = requests.get(root + path, headers=HEAD, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def discover_markets():
    """Discover Alpaca-tradeable crypto pairs, rather than assuming every token is supported."""
    if not SCAN_ALL_CRYPTO:
        return list(CORE_SYMBOLS)
    assets = api(PAPER_API, '/v2/assets', {'status': 'active', 'asset_class': 'crypto'})
    candidates = set(CORE_SYMBOLS)
    for asset in assets:
        if not asset.get('tradable', False):
            continue
        symbol = asset.get('symbol', '')
        if '/' not in symbol or not symbol.endswith('/USD'):
            continue
        candidates.add(symbol)
    # The first scan prioritizes the three core symbols; cap to limit free CI/API use.
    return list(CORE_SYMBOLS) + sorted(candidates - set(CORE_SYMBOLS))[:MAX_MARKETS-3]


def ema(values, span):
    result = values[0]
    alpha = 2 / (span + 1)
    out = []
    for value in values:
        result = alpha * value + (1 - alpha) * result
        out.append(result)
    return out


def get_bars(symbol, timeframe, hours, min_count):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    # Fetch one symbol at a time. Crypto feeds may contain gaps in hourly bars.
    response = api(DATA_API, '/v1beta3/crypto/us/bars', {
        'symbols': symbol, 'timeframe': timeframe,
        'start': start.isoformat().replace('+00:00', 'Z'),
        'end': now.isoformat().replace('+00:00', 'Z'),
        'limit': 1000,
        'sort': 'asc',
    })
    rows = response.get('bars', {}).get(symbol, [])
    if not isinstance(rows, list):
        raise RuntimeError(f'Invalid bars response for {symbol}')
    duration = timedelta(hours=1 if timeframe == '1Hour' else 4)
    completed = []
    for row in rows:
        timestamp = datetime.fromisoformat(row['t'].replace('Z', '+00:00'))
        if timestamp + duration <= now:
            completed.append(row)
    completed = list({b['t']: b for b in completed}.values())
    completed.sort(key=lambda b: b['t'])
    if len(completed) < min_count:
        raise RuntimeError(f'Only {len(completed)} completed {timeframe} bars for {symbol}')
    last_start = datetime.fromisoformat(completed[-1]['t'].replace('Z', '+00:00'))
    # Do not use an arbitrary 3-hour cutoff. Report gaps and skip stale markets.
    # For 1h, accept up to 4h since last bar close; for 4h, up to 8h.
    freshness = timedelta(hours=4 if timeframe == '1Hour' else 8)
    age = now - (last_start + duration)
    if age > freshness:
        raise RuntimeError(f'Stale {timeframe} bars for {symbol}: last completed close '
                           f'{(last_start + duration).isoformat()}, age {age}')
    return completed


def rsi14(closes):
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(0.0, c) for c in changes[-14:]]
    losses = [max(0.0, -c) for c in changes[-14:]]
    avg_gain, avg_loss = sum(gains)/14, sum(losses)/14
    return 100.0 if avg_loss == 0 else 100 - 100/(1 + avg_gain/avg_loss)


def atr14(rows):
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = float(rows[i]['h']), float(rows[i]['l']), float(rows[i-1]['c'])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / 14


def evaluate(hourly, four_hour):
    closes = [float(b['c']) for b in hourly]
    volumes = [float(b.get('v', 0)) for b in hourly]
    higher = [float(b['c']) for b in four_hour]
    e9, e21, e50 = (ema(closes, n) for n in (9, 21, 50))
    h21, h50 = ema(higher, 21), ema(higher, 50)
    price = closes[-1]
    atr = atr14(hourly)
    if price <= 0 or atr <= 0:
        raise RuntimeError('Invalid price or ATR')
    # Score measures agreement of technical evidence, not calibrated win probability.
    trend4 = higher[-1] > h21[-1] > h50[-1]
    trend1 = price > e21[-1] > e50[-1]
    ema_good = e9[-1] > e21[-1] and e9[-1] >= e9[-2]
    recent_breakout = price > max(float(b['h']) for b in hourly[-7:-1])
    vol_avg = sum(volumes[-21:-1]) / 20
    volume_good = vol_avg > 0 and volumes[-1] >= 1.2 * vol_avg
    candle_good = float(hourly[-1]['c']) > float(hourly[-1]['o'])
    rsi = rsi14(closes)
    rsi_good = 45 <= rsi <= 72
    atr_pct = atr / price
    volatility_good = .002 <= atr_pct <= .08
    # Previous 24 fully completed hourly bars: high/low and breakout context.
    day_high = max(float(b['h']) for b in hourly[-25:-1])
    day_low = min(float(b['l']) for b in hourly[-25:-1])
    day_range = day_high - day_low
    range_position = (price - day_low) / day_range if day_range > 0 else 0.5
    # Entry requires an actual price trigger, not merely a high score.
    trigger = (recent_breakout or (closes[-2] <= e9[-2] and price > e9[-1])) and range_position >= 0.5
    score = (25 * trend4 + 20 * trend1 + 15 * ema_good +
             10 * recent_breakout + 10 * volume_good +
             5 * candle_good + 10 * volatility_good + 5 * rsi_good)
    return dict(price=price, atr=atr, score=int(score), trigger=trigger,
                trend4=trend4, trend1=trend1, volume=volume_good,
                bar=hourly[-1]['t'], day_high=day_high, day_low=day_low,
                range_position=range_position, rsi=rsi,
                atr_pct=100*atr_pct, breakout=recent_breakout)


def sizing(score, equity, buying_power, price, atr):
    tier = next(((risk, dollars) for cutoff, risk, dollars in RISK_TIERS
                 if score >= cutoff), None)
    if tier is None:
        return None
    risk_fraction, tier_dollars = tier
    # Stops based on volatility; farther stops result in smaller position sizes.
    stop_distance = 1.5 * atr
    if stop_distance <= 0 or stop_distance >= price:
        return None
    risk_budget = equity * risk_fraction
    notional = min(MAX_TRADE_USD, tier_dollars, .25 * equity,
                   .95 * max(0, buying_power), risk_budget / stop_distance * price)
    if notional < 1.05:
        return None
    return dict(notional=notional, stop=price-stop_distance,
                target=price+3*stop_distance,
                estimated_loss=notional/price*stop_distance,
                risk_pct=risk_fraction*100)


def diagnose_crypto_feed():
    """Compare latest crypto bars with historical endpoint."""
    for symbol in ('BTC/USD', 'ETH/USD', 'SOL/USD'):
        try:
            result = api(
                DATA_API,
                '/v1beta3/crypto/us/latest/bars',
                {'symbols': symbol}
            )
            latest = result.get('bars', {}).get(symbol)
            if latest:
                print(
                    f'FEED DIAGNOSTIC {symbol}: '
                    f'latest_timestamp={latest.get("t")} '
                    f'latest_close={latest.get("c")}',
                    flush=True
                )
            else:
                print(
                    f'FEED DIAGNOSTIC {symbol}: '
                    f'No latest bar returned; response={str(result)[:300]}',
                    flush=True
                )
        except Exception as exc:
            print(
                f'FEED DIAGNOSTIC {symbol}: '
                f'{type(exc).__name__}: {str(exc)[:300]}',
                flush=True
            )


def main():
    account = api(PAPER_API, '/v2/account')
    equity = float(account['equity'])
    previous = float(account.get('last_equity') or equity)
    power = float(account.get('buying_power') or 0)
    if equity <= 0:
        raise RuntimeError('Paper account equity is zero')   
    diagnose_crypto_feed()
    symbols = discover_markets()
    notify(f'PAPER SIGNAL SCAN started | {len(symbols)} crypto markets | equity ${equity:.2f} | '
           f'mode SIGNALS ONLY (no orders)')
    if previous > 0 and equity <= previous * (1 - DAILY_LOSS_PCT/100):
        notify(f'PAUSED: equity below previous close by {DAILY_LOSS_PCT:.2f}% or more')
        return
    positions = api(PAPER_API, '/v2/positions')
    open_orders = api(PAPER_API, '/v2/orders', {'status': 'open', 'limit': 100})
    blocked = bool(positions or open_orders)
    if blocked:
        notify('Entry alerts blocked: paper account has existing positions or open orders; market scans continue')
    failures = []
    candidates = []
    for symbol in symbols:
        try:
            hourly = get_bars(symbol, '1Hour', 24*12, 70)
            four_hour = get_bars(symbol, '4Hour', 24*25, 60)
            result = evaluate(hourly, four_hour)
            score = result['score']
            RUN_RESULTS.append({'symbol': symbol, 'time_utc': datetime.now(timezone.utc).isoformat(),
                                'bar': result['bar'], 'score': score,
                                'price': result['price'], 'atr14': result['atr'],
                                'rsi14': round(result['rsi'], 2),
                                'high_24h': result['day_high'], 'low_24h': result['day_low'],
                                'trigger': bool(result['trigger']), 'entries_blocked': blocked})
            print(f'{symbol} previous 24h high ${result["day_high"]:.5f}; '
                  f'low ${result["day_low"]:.5f}; '
                  f'range position {result["range_position"]:.0%}', flush=True)
            if blocked or score < MIN_SCORE or not result['trigger'] or not result['trend4'] or not result['trend1']:
                print(f'{symbol}: score {score}/100; no qualified entry; '
                      f'RSI {result["rsi"]:.1f}; bar {result["bar"]}', flush=True)
                continue
            size = sizing(score, equity, power, result['price'], result['atr'])
            if not size:
                print(f'{symbol}: signal but risk/size constraints reject entry', flush=True)
                continue
            candidates.append((score, symbol, result, size))
            print(f'QUALIFIED {symbol} score {score}/100, allocation ${size["notional"]:.2f}', flush=True)
        except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
            failures.append(f'{symbol}: {type(exc).__name__}: {str(exc)[:220]}')
            RUN_RESULTS.append({'symbol': symbol, 'error': failures[-1],
                                'time_utc': datetime.now(timezone.utc).isoformat()})
            notify(f'MARKET DATA ERROR {failures[-1]}')
    # Rank every valid candidate; report only the strongest to reduce alert spam.
    candidates.sort(key=lambda item: (item[0], -item[3]['estimated_loss']), reverse=True)
    if candidates:
        score, symbol, result, size = candidates[0]
        notify(f'TOP PAPER CANDIDATE: {symbol} | score {score}/100 (not win probability)\n'
               f'1h candle {result["bar"]} | reference ${result["price"]:.6f}\n'
               f'24h high ${result["day_high"]:.6f} / low ${result["day_low"]:.6f}\n'
               f'Allocation ${size["notional"]:.2f} | planned risk ${size["estimated_loss"]:.2f}\n'
               f'Indicative stop ${size["stop"]:.6f} / target ${size["target"]:.6f}\n'
               f'NO ORDER PLACED; NO AUTOMATED STOP ACTIVE.')
    else:
        print('No qualified candidate. No trade recommended.', flush=True)
    with open('scan_results.json', 'w', encoding='utf-8') as report:
        json.dump({'generated_utc': datetime.now(timezone.utc).isoformat(),
                   'signals_only': True, 'equity': equity, 'scanned': len(symbols), 'results': RUN_RESULTS},
                  report, indent=2)
    if len(failures) == len(symbols):
        raise RuntimeError('All market scans failed. Review logs; do not trade.')
    notify(f'PAPER SCAN finished | {len(symbols)-len(failures)}/{len(symbols)} markets checked')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        notify(f'PAPER BOT FAILED: {type(exc).__name__}: {str(exc)[:250]}')
        sys.exit(1)
