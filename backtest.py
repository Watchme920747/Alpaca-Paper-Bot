"""Walk-forward evaluation of the bot's existing hourly crypto strategy.

Usage: python backtest.py candles.json
Input: chronological Alpaca-style hourly bars [{t,o,h,l,c,v}, ...].
No orders or network requests are made. Simplified close-to-close fills;
fees and slippage are deducted from each completed trade.
"""
import json
import sys
from bot import evaluate

def backtest(bars, fee_bps=10, slippage_bps=5):
    trades = []
    position = None
    for end in range(240, len(bars) + 1):
        window = bars[:end]
        try:
            market = evaluate(window)
        except ValueError:
            continue
        price = market["price"]
        if position is not None:
            position["bars"] += 1
            if price <= position["stop"] or price >= position["target"] or position["bars"] >= 12:
                gross = price / position["entry"] - 1
                net = gross - 2 * (fee_bps + slippage_bps) / 10000
                trades.append({"entry_bar": position["bar"], "exit_bar": market["bar"],
                               "return_pct": round(net * 100, 5)})
                position = None
        if position is None and (market["score"] >= 75 and market["trigger"]
                and market["trend1"] and market["trend4"] and market["macd_ok"]):
            # Signal is generated on a completed bar; entry at this same close is optimistic.
            # Use NEXT completed bar open, if available, to avoid look-ahead.
            if end >= len(bars):
                continue
            entry = float(bars[end]["o"])
            if entry <= 0:
                continue
            distance = 1.5 * market["atr"]
            position = {"entry": entry, "bar": bars[end]["t"], "bars": 0,
                        "stop": entry - distance, "target": entry + 2 * distance}
    wins = sum(t["return_pct"] > 0 for t in trades)
    compounded = 1
    peak = 1
    max_drawdown = 0
    for trade in trades:
        compounded *= 1 + trade["return_pct"] / 100
        peak = max(peak, compounded)
        max_drawdown = max(max_drawdown, 1 - compounded / peak)
    return {"trades": len(trades), "win_rate": round(wins / len(trades), 4) if trades else 0,
            "total_return_pct": round((compounded - 1) * 100, 4),
            "max_drawdown_pct": round(max_drawdown * 100, 4),
            "avg_trade_pct": round(sum(t["return_pct"] for t in trades) / len(trades), 4) if trades else 0,
            "trade_log": trades}

if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as file:
        rows = json.load(file)
    print(json.dumps(backtest(rows), indent=2))
