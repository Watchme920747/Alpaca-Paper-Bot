"""Educational Alpaca PAPER-only crypto trading bot. No live trading endpoints."""
import os, sys, math, time, requests
from datetime import datetime, timedelta, timezone

KEY = os.environ['ALPACA_PAPER_KEY']
SECRET = os.environ['ALPACA_PAPER_SECRET']
BASE = 'https://paper-api.alpaca.markets'
DATA = 'https://data.alpaca.markets'
HEAD = {'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SECRET}
SYMBOLS = ['BTC/USD', 'ETH/USD', 'SOL/USD']
RISK = 0.01
MAX_ALLOCATION = 0.25
DAILY_LOSS_LIMIT = 0.02
ENABLE_ORDERS = os.getenv('ENABLE_ORDERS', 'false').lower() == 'true'
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')


def notify(msg):
    print(msg, flush=True)
    if TOKEN and CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                          json={'chat_id': CHAT, 'text': msg[:4000]}, timeout=12).raise_for_status()
        except Exception as ex:
            print(f'Telegram failed: {ex}', file=sys.stderr)


def api(method, path, *, params=None, payload=None, data=False):
    root = DATA if data else BASE
    r = requests.request(method, root + path, headers=HEAD, params=params, json=payload, timeout=25)
    r.raise_for_status()
    return r.json()


def ema(values, span):
    a = 2 / (span + 1)
    result = values[0]
    out = []
    for v in values:
        result = a*v + (1-a)*result
        out.append(result)
    return out


def bars(symbol):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=8)
    rows = api('GET', '/v1beta3/crypto/us/bars', data=True, params={
        'symbols': symbol, 'timeframe': '1Hour',
        'start': start.isoformat().replace('+00:00', 'Z'),
        'end': end.isoformat().replace('+00:00', 'Z'),
        'limit': 1000,
    })['bars'].get(symbol, [])
    # Ignore any candle still forming. UTC timestamp denotes bar start.
    completed = []
    for b in rows:
        stamp = datetime.fromisoformat(b['t'].replace('Z', '+00:00'))
        if stamp + timedelta(hours=1) <= end:
            completed.append(b)
    if len(completed) < 60:
        raise RuntimeError(f'Insufficient complete bars for {symbol}: {len(completed)}')
    if datetime.fromisoformat(completed[-1]['t'].replace('Z', '+00:00')) < end - timedelta(hours=3):
        raise RuntimeError(f'Stale bars for {symbol}')
    return completed


def signal(rows):
    closes = [float(b['c']) for b in rows]
    fast, slow = ema(closes, 9), ema(closes, 21)
    trs = []
    for i in range(1, len(rows)):
        hi, lo, pc = float(rows[i]['h']), float(rows[i]['l']), closes[i-1]
        trs.append(max(hi-lo, abs(hi-pc), abs(lo-pc)))
    atr = sum(trs[-14:]) / 14
    price = closes[-1]
    # New upward crossover on the last completed 1-hour candle only.
    cross = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
    trend = price > slow[-1]
    return price, atr, bool(cross and trend), rows[-1]['t']


def positions():
    return api('GET', '/v2/positions')


def orders():
    after = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return api('GET', '/v2/orders', params={'status':'all','limit':100,'after':after,'direction':'desc'})


def place(symbol, side, *, notional=None, qty=None):
    body = {'symbol':symbol, 'side':side, 'type':'market', 'time_in_force':'gtc'}
    if notional is not None:
        body['notional'] = str(round(notional, 2))
    else:
        body['qty'] = str(qty)
    return api('POST', '/v2/orders', payload=body)


def main():
    acct = api('GET', '/v2/account')
    equity = float(acct['equity'])
    buying_power = float(acct['buying_power'])
    previous = float(acct.get('last_equity') or equity)
    if equity <= 0: raise RuntimeError('Account equity is zero')
    if previous > 0 and equity < previous * (1 - DAILY_LOSS_LIMIT):
        notify('PAPER BOT PAUSED: account down >2% vs previous close. Review manually.')
        return
    current = positions()
    pending = api('GET', '/v2/orders', params={'status':'open', 'limit':100})
    recent = orders()
    # Avoid new entries while any position/order exists; avoid repeat buys in past 24h.
    for symbol in SYMBOLS:
        try:
            candles = bars(symbol)
            price, atr, enter, timestamp = signal(candles)
            name = symbol.replace('/', '')
            pos = next((p for p in current if p['symbol'].replace('/','') == name), None)
            if pos:
                entry_price = float(pos['avg_entry_price'])
                # Stops are software-managed, checked on scheduled runs only.
                # Dynamic ATR from current bars; not an exchange-native protective stop.
                stop = entry_price - 1.5*atr
                take = entry_price + 3*atr
                if price <= stop or price >= take:
                    reason = 'STOP' if price <= stop else 'TARGET'
                    if ENABLE_ORDERS:
                        result = place(symbol, 'sell', qty=pos['qty_available'])
                        notify(f'PAPER SELL SUBMITTED {symbol}: {reason}, price~{price:.5f}, order={result["id"]}')
                    else:
                        notify(f'DRY RUN sell signal {symbol}: {reason}, price~{price:.5f}')
                continue
            if not enter: continue
            if current or pending: continue
            if any(o.get('side')=='buy' and o.get('symbol','').replace('/','')==name for o in recent):
                continue
            stop_distance = 1.5*atr
            if stop_distance <= 0: continue
            risk_dollars = equity * RISK
            units = risk_dollars / stop_distance
            notional = min(units*price, equity*MAX_ALLOCATION, buying_power*0.95)
            if notional < 1.05: continue
            planned_loss = notional/price * stop_distance
            msg = (f'PAPER SIGNAL {symbol} | bar {timestamp}\nEntry~${price:.5f}; '
                   f'notional ${notional:.2f}; estimated risk ${planned_loss:.2f}; '
                   f'stop~${price-stop_distance:.5f}; target~${price+3*atr:.5f}; '
                   f'hold: hours to days; mode: {"PAPER ORDERS" if ENABLE_ORDERS else "DRY RUN"}')
            notify(msg)
            if ENABLE_ORDERS:
                result = place(symbol, 'buy', notional=notional)
                notify(f'PAPER BUY SUBMITTED {symbol}: order={result["id"]} (not necessarily filled)')
                break
        except Exception as ex:
            notify(f'Bot check error for {symbol}: {type(ex).__name__}: {str(ex)[:180]}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        notify(f'PAPER BOT ERROR: {type(exc).__name__}: {str(exc)[:200]}')
        sys.exit(1)