"""Alpaca PAPER-ONLY crypto scanner and optional paper order executor.

Python 3.10+. Dependency: requests>=2.31,<3.
Environment: ALPACA_PAPER_KEY, ALPACA_PAPER_SECRET.
Optional: ENABLE_PAPER_ORDERS=false, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
MAX_MARKETS=20, MIN_CONFIDENCE_SCORE=75, MAX_TRADE_USD=50,
MAX_DAILY_LOSS_PCT=2, RISK_PER_TRADE_PCT=0.5, MAX_OPEN_POSITIONS=3.

IMPORTANT: Stop/target levels are monitored by subsequent runs, NOT broker-native
protective orders. Running periodically cannot guarantee a stop fill. Paper only.
"""
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
LOG = logging.getLogger("paperbot")
CORE = ("BTC/USD", "ETH/USD", "SOL/USD")
STATE_PATH = Path(os.getenv("STATE_FILE", "paper_bot_state.json"))
REPORT_PATH = Path(os.getenv("REPORT_FILE", "scan_results.json"))


def env_float(name, default, low, high):
    try:
        value = float(os.getenv(name, default))
        if not math.isfinite(value):
            raise ValueError("not finite")
        return min(high, max(low, value))
    except ValueError as exc:
        raise ValueError(f"Invalid {name}: {exc}") from exc


def env_int(name, default, low, high):
    try:
        return min(high, max(low, int(os.getenv(name, default))))
    except ValueError as exc:
        raise ValueError(f"Invalid {name}: {exc}") from exc


class Alpaca:
    def __init__(self):
        key = os.getenv("ALPACA_PAPER_KEY")
        secret = os.getenv("ALPACA_PAPER_SECRET")
        if not key or not secret:
            raise RuntimeError("Set ALPACA_PAPER_KEY and ALPACA_PAPER_SECRET")
        self.session = requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def request(self, method, base, path, *, params=None, payload=None):
        # Retry GET only. Retrying POST can create duplicate orders after timeouts.
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, base + path, params=params,
                                                json=payload, timeout=25)
                if method == "GET" and response.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    wait = min(12, 2 ** attempt)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json() if response.content else {}
            except requests.RequestException:
                if method != "GET" or attempt == attempts - 1:
                    raise
                time.sleep(min(12, 2 ** attempt))

    def account(self):
        return self.request("GET", PAPER, "/v2/account")

    def assets(self):
        return self.request("GET", PAPER, "/v2/assets", params={"status": "active", "asset_class": "crypto"})

    def positions(self):
        return self.request("GET", PAPER, "/v2/positions")

    def orders(self):
        return self.request("GET", PAPER, "/v2/orders", params={"status": "open", "limit": 500})

    def bars(self, symbol, hours=24 * 40):
        now = datetime.now(timezone.utc)
        params = {"symbols": symbol, "timeframe": "1Hour",
                  "start": (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
                  "end": now.isoformat().replace("+00:00", "Z"),
                  "limit": 1000, "sort": "asc"}
        found = {}
        token = None
        for _ in range(12):
            if token:
                params["page_token"] = token
            response = self.request("GET", DATA, "/v1beta3/crypto/us/bars", params=params)
            for bar in response.get("bars", {}).get(symbol, []):
                stamp = parse_time(bar["t"])
                if stamp + timedelta(hours=1) <= now:
                    found[stamp] = bar
            next_token = response.get("next_page_token")
            if not next_token or next_token == token:
                break
            token = next_token
        rows = [found[t] for t in sorted(found)]
        if len(rows) < 240:
            raise ValueError(f"Insufficient hourly candles: {len(rows)} (need 240)")
        last_close = parse_time(rows[-1]["t"]) + timedelta(hours=1)
        if now - last_close > timedelta(hours=4):
            raise ValueError(f"Stale hourly candle: {last_close.isoformat()}")
        return rows

    def submit_buy(self, symbol, dollars, client_id):
        return self.request("POST", PAPER, "/v2/orders", payload={
            "symbol": symbol, "notional": f"{dollars:.2f}", "side": "buy",
            "type": "market", "time_in_force": "gtc", "client_order_id": client_id})

    def submit_sell(self, symbol, qty, client_id):
        return self.request("POST", PAPER, "/v2/orders", payload={
            "symbol": symbol, "qty": str(qty), "side": "sell",
            "type": "market", "time_in_force": "gtc", "client_order_id": client_id})


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def as_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite market value")
    return number


def ema(values, period):
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def rsi(values, period=14):
    diffs = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(d, 0) for d in diffs[:period]) / period
    loss = sum(max(-d, 0) for d in diffs[:period]) / period
    for diff in diffs[period:]:
        gain = (gain * (period - 1) + max(diff, 0)) / period
        loss = (loss * (period - 1) + max(-diff, 0)) / period
    if gain == loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def atr(rows, period=14):
    true_ranges = []
    for previous, current in zip(rows, rows[1:]):
        high, low, prior_close = (as_number(current["h"]), as_number(current["l"]),
                                  as_number(previous["c"]))
        true_ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def aggregate_four_hour(rows):
    buckets = {}
    for bar in rows:
        ts = parse_time(bar["t"])
        bucket = ts.replace(hour=(ts.hour // 4) * 4, minute=0, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(bar)
    result = []
    for bucket, bars in sorted(buckets.items()):
        # Require all four expected consecutive hourly candles.
        stamps = {parse_time(bar["t"]) for bar in bars}
        if stamps != {bucket + timedelta(hours=i) for i in range(4)}:
            continue
        bars.sort(key=lambda b: b["t"])
        result.append({"t": bucket.isoformat(), "o": bars[0]["o"],
                       "h": max(as_number(b["h"]) for b in bars),
                       "l": min(as_number(b["l"]) for b in bars),
                       "c": bars[-1]["c"], "v": sum(as_number(b.get("v", 0)) for b in bars)})
    return result


def evaluate(rows):
    higher = aggregate_four_hour(rows)
    if len(higher) < 55:
        raise ValueError(f"Insufficient complete 4-hour candles: {len(higher)}")
    closes = [as_number(b["c"]) for b in rows]
    volumes = [as_number(b.get("v", 0)) for b in rows]
    four_closes = [as_number(b["c"]) for b in higher]
    if any(x <= 0 for x in closes) or any(x < 0 for x in volumes):
        raise ValueError("Invalid price/volume")
    e9, e21, e50 = (ema(closes, n) for n in (9, 21, 50))
    h21, h50 = ema(four_closes, 21), ema(four_closes, 50)
    macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    signal = ema(macd, 9)
    price, volatility = closes[-1], atr(rows)
    if volatility <= 0:
        raise ValueError("Zero ATR")
    trend4 = four_closes[-1] > h21[-1] > h50[-1]
    trend1 = price > e21[-1] > e50[-1]
    momentum = e9[-1] > e21[-1] and e9[-1] > e9[-2]
    breakout = price > max(as_number(b["h"]) for b in rows[-7:-1])
    avg_volume = sum(volumes[-21:-1]) / 20
    volume_ok = avg_volume > 0 and volumes[-1] >= avg_volume * 1.2
    rsi_value = rsi(closes)
    rsi_ok = 45 <= rsi_value <= 72
    macd_ok = macd[-1] > signal[-1] and macd[-1] > macd[-2]
    volatility_pct = volatility / price
    volatility_ok = 0.002 <= volatility_pct <= 0.08
    day_high = max(as_number(b["h"]) for b in rows[-25:-1])
    day_low = min(as_number(b["l"]) for b in rows[-25:-1])
    position = (price - day_low) / (day_high - day_low) if day_high > day_low else 0.5
    trigger = (breakout or (closes[-2] <= e9[-2] and price > e9[-1])) and position >= 0.5
    score = (20 * trend4 + 20 * trend1 + 15 * momentum + 10 * breakout +
             10 * volume_ok + 10 * macd_ok + 5 * rsi_ok + 10 * volatility_ok)
    return {"price": price, "atr": volatility, "score": int(score),
            "rsi": round(rsi_value, 2), "trigger": bool(trigger),
            "trend4": bool(trend4), "trend1": bool(trend1), "macd_ok": bool(macd_ok),
            "bar": rows[-1]["t"], "high24": day_high, "low24": day_low,
            "volume_ok": bool(volume_ok), "breakout": bool(breakout)}


def notify(message):
    LOG.info("%s", message)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                     json={"chat_id": chat, "text": message[:4000]}, timeout=12)
            response.raise_for_status()
        except requests.RequestException as exc:
            LOG.warning("Telegram notification failed: %s", exc.__class__.__name__)


def load_state():
    if not STATE_PATH.exists():
        return {"managed": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state.get("managed"), dict):
            raise ValueError("Invalid state format")
        return state
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot safely read state: {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def position_symbol(item):
    return str(item.get("symbol", "")).replace("/", "").upper()


def update_simulations(state, latest, max_bars=12):
    """Evaluate paperless shadow entries only on later completed bars."""
    sims = state.setdefault("simulations", [])
    for sim in sims:
        if sim.get("status") != "open":
            continue
        market = latest.get(sim["symbol"])
        if not market or market["bar"] <= sim["entry_bar"]:
            continue
        sim["bars_held"] = sim.get("bars_held", 0) + 1
        price = market["price"]
        if price <= sim["stop"] or price >= sim["target"] or sim["bars_held"] >= max_bars:
            # Conservative close-based evaluation; actual intrabar stops may differ.
            sim["exit_price"] = price
            sim["return_pct"] = round(100 * (price / sim["entry_price"] - 1), 4)
            sim["status"] = "closed"
            sim["exit_bar"] = market["bar"]
    state["simulations"] = sims[-500:]


def performance(state):
    closed = [s for s in state.get("simulations", []) if s.get("status") == "closed"]
    recent = closed[-30:]
    wins = sum(s["return_pct"] > 0 for s in recent)
    win_rate = wins / len(recent) if recent else 0
    avg_return = sum(s["return_pct"] for s in recent) / len(recent) if recent else 0
    # Never increase size from scan counts, and require enough resolved trades.
    proven = len(recent) >= 20 and win_rate >= 0.60 and avg_return > 0
    degraded = len(recent) >= 10 and (win_rate < 0.45 or avg_return <= 0)
    allocation = 25.0 if proven else (5.0 if degraded else 10.0)
    return {"closed": len(closed), "recent": len(recent), "wins": wins,
            "win_rate": round(win_rate, 4), "avg_return_pct": round(avg_return, 4),
            "paper_allocation_cap": allocation}


def main():
    execute = os.getenv("ENABLE_PAPER_ORDERS", "false").lower() == "true"
    max_markets = env_int("MAX_MARKETS", 20, 3, 100)
    min_score = env_int("MIN_CONFIDENCE_SCORE", 75, 0, 100)
    max_trade = env_float("MAX_TRADE_USD", 50, 0, 1000)
    risk_pct = env_float("RISK_PER_TRADE_PCT", 0.5, 0, 2)
    loss_pct = env_float("MAX_DAILY_LOSS_PCT", 2, 0, 10)
    max_positions = env_int("MAX_OPEN_POSITIONS", 3, 1, 20)
    api = Alpaca()
    account = api.account()
    equity = as_number(account["equity"])
    buying_power = max(0, as_number(account.get("buying_power") or 0))
    last_equity = as_number(account.get("last_equity") or equity)
    if equity <= 0:
        raise RuntimeError("Paper equity must be positive")
    # Note: Alpaca last_equity is not necessarily a rolling 24-hour high-water mark.
    daily_block = last_equity > 0 and equity <= last_equity * (1 - loss_pct / 100)
    positions = api.positions()
    open_orders = api.orders()
    state = load_state()
    owned = {position_symbol(p): p for p in positions}
    pending = {position_symbol(o) for o in open_orders}
    assets = [a for a in api.assets() if a.get("tradable") and
              a.get("status") == "active" and str(a.get("symbol", "")).endswith("/USD")]
    asset_symbols = {a["symbol"] for a in assets}
    symbols = [s for s in CORE if s in asset_symbols]
    symbols += sorted(asset_symbols - set(symbols))[:max(0, max_markets - len(symbols))]
    if not symbols:
        raise RuntimeError("No active tradeable USD crypto pairs found")
    notify(f"PAPER BOT start | mode={'PAPER ORDERS' if execute else 'SIGNALS ONLY'} | "
           f"markets={len(symbols)} equity=${equity:.2f}")
    if daily_block:
        notify("Daily equity-loss threshold reached: NEW ENTRIES BLOCKED")
    results, candidates, errors = [], [], []
    latest_by_symbol = {}
    for symbol in symbols:
        try:
            result = evaluate(api.bars(symbol))
            latest_by_symbol[symbol.replace("/", "")] = result
            qualified = (result["score"] >= min_score and result["trigger"] and
                         result["trend1"] and result["trend4"] and result["macd_ok"])
            rejection_reasons = []
            if result["score"] < min_score:
                rejection_reasons.append(f"score {result['score']}<{min_score}")
            for flag in ("trigger", "trend1", "trend4", "macd_ok"):
                if not result[flag]:
                    rejection_reasons.append(flag)
            result.update({"symbol": symbol, "qualified": bool(qualified),
                           "rejection_reasons": rejection_reasons})
            results.append(result)
            LOG.info("%s score=%d RSI=%.1f qualified=%s bar=%s", symbol,
                     result["score"], result["rsi"], qualified, result["bar"])
            if qualified:
                candidates.append(result)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            error = f"{symbol}: {type(exc).__name__}: {str(exc)[:180]}"
            LOG.warning("Market skipped: %s", error)
            errors.append(error)
            results.append({"symbol": symbol, "error": error})
    update_simulations(state, latest_by_symbol)
    stats = performance(state)
    # Shadow-test promising signals, including near misses, before allocating paper capital.
    for item in sorted((r for r in results if "error" not in r),
                       key=lambda r: r["score"], reverse=True)[:5]:
        if item["score"] < 55 or not item["trend1"] or not item["macd_ok"]:
            continue
        symbol = item["symbol"].replace("/", "")
        active = any(s.get("status") == "open" and s["symbol"] == symbol
                     for s in state["simulations"])
        if active or any(s["symbol"] == symbol and s["entry_bar"] == item["bar"]
                         for s in state["simulations"]):
            continue
        distance = 1.5 * item["atr"]
        if distance <= 0 or distance >= item["price"]:
            continue
        state["simulations"].append({
            "symbol": symbol, "entry_bar": item["bar"], "entry_price": item["price"],
            "stop": item["price"] - distance, "target": item["price"] + 2 * distance,
            "status": "open", "bars_held": 0, "score": item["score"]})
    state["simulations"] = state["simulations"][-500:]
    notify(f"SHADOW TEST | closed={stats['closed']} recent_win_rate="
           f"{stats['win_rate']:.0%} avg_return={stats['avg_return_pct']:.3f}% "
           f"paper_cap=${stats['paper_allocation_cap']:.2f}")
    # For safety, managed exits require a persistent state file AND a fresh scan.
    # If running on ephemeral CI, persist state across runs or use signals-only mode.
    if execute:
        for symbol, plan in list(state["managed"].items()):
            if symbol not in owned:
                del state["managed"][symbol]
                continue
            if symbol in pending or symbol not in latest_by_symbol:
                continue
            market = latest_by_symbol[symbol]
            price = market["price"]
            if price <= plan["stop"] or price >= plan["target"]:
                qty = as_number(owned[symbol].get("qty_available", owned[symbol].get("qty", 0)))
                if qty <= 0:
                    continue
                side = "STOP" if price <= plan["stop"] else "TARGET"
                client_id = f"pb-exit-{symbol.lower()}-{int(time.time())}"
                try:
                    api.submit_sell(symbol, qty, client_id)
                    notify(f"PAPER {side} EXIT submitted {symbol} qty={qty:.9f} ref=${price:.6f}")
                    pending.add(symbol)
                except requests.RequestException as exc:
                    notify(f"EXIT SUBMISSION UNCERTAIN {symbol}: {type(exc).__name__}; check Alpaca orders")
                    pending.add(symbol)
    candidates.sort(key=lambda r: (r["score"], r["volume_ok"]), reverse=True)
    for item in candidates[:5]:
        price, distance = item["price"], 1.5 * item["atr"]
        if distance <= 0 or distance >= price:
            continue
        risk_dollars = equity * risk_pct / 100
        notional = min(max_trade, stats["paper_allocation_cap"], buying_power * 0.95, equity * 0.25,
                       risk_dollars * price / distance)
        if notional < 1.05:
            continue
        stop, target = price - distance, price + 2 * distance
        notify(f"CANDIDATE {item['symbol']} score={item['score']}/100 RSI={item['rsi']} "
               f"ref=${price:.6f} size=${notional:.2f} stop=${stop:.6f} target=${target:.6f}")
        normalized = item["symbol"].replace("/", "")
        if not execute or daily_block or normalized in owned or normalized in pending:
            continue
        if len(owned) + len(pending) >= max_positions:
            continue
        # One entry per completed bar; requires durable state across invocations.
        last_bar = state.get("last_entry_bar", {}).get(normalized)
        if last_bar == item["bar"]:
            continue
        # Persist intent BEFORE submission, to reduce repeat entries on restart/timeouts.
        state.setdefault("last_entry_bar", {})[normalized] = item["bar"]
        save_json(STATE_PATH, state)
        client_id = f"pb-entry-{normalized.lower()}-{int(time.time())}"
        try:
            api.submit_buy(item["symbol"], notional, client_id)
            state["managed"][normalized] = {"stop": stop, "target": target,
                                             "entry_reference": price, "client_id": client_id}
            save_json(STATE_PATH, state)
            notify(f"PAPER BUY submitted {item['symbol']} ${notional:.2f}; "
                   "stop/target SOFTWARE-MONITORED, not broker orders")
            pending.add(normalized)
        except requests.RequestException as exc:
            notify(f"BUY SUBMISSION UNCERTAIN {item['symbol']}: {type(exc).__name__}; "
                   "inspect Alpaca before retrying")
            pending.add(normalized)
    # Report near misses so zero-candidate scans are actionable.
    near_misses = sorted((r for r in results if "error" not in r and not r["qualified"]),
                         key=lambda r: r["score"], reverse=True)[:5]
    if not candidates:
        summary = "; ".join(
            f"{r['symbol']} {r['score']}/100: {', '.join(r['rejection_reasons'])}"
            for r in near_misses)
        notify("NO QUALIFIED SETUPS | Top near misses: " + (summary or "none"))
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "paper_orders_enabled": execute, "daily_entries_blocked": daily_block,
              "equity": equity, "markets": len(symbols), "performance": stats, "errors": errors, "results": results}
    save_json(REPORT_PATH, report)
    save_json(STATE_PATH, state)
    notify(f"PAPER BOT complete | {len(symbols)-len(errors)}/{len(symbols)} scans succeeded | "
           f"{len(candidates)} candidates | {len(errors)} errors")
    if len(errors) == len(symbols):
        raise RuntimeError("Every market scan failed; see scan_results.json")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except Exception as exc:
        LOG.exception("PAPER BOT FAILED: %s", exc)
        sys.exit(1)
