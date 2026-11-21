# iPhone Alpaca PAPER-only bot

## Important
This is an educational, **paper-only**, scheduled crypto bot, not a profitable or production-ready strategy. It does not execute real-money trades and cannot be switched to live trading by configuration. The default mode is **dry-run** (`ENABLE_ORDERS=false`). With `ENABLE_ORDERS=true`, it sends simulated Alpaca paper orders only.

The bot checks 1-hour completed crypto candles for an EMA 9/21 bullish crossover and uses ATR-based risk sizing (1% account equity, max 25% allocated), with 2% daily loss pause based on Alpaca `last_equity`. It permits one position at a time and avoids repeat entries in the same symbol for 24 hours.

**Critical limitation:** Crypto exits are bot-managed only, not exchange-native stops. A crash or GitHub delay/outage can cause much greater losses than the planned 1%. ATR is recalculated on each run, so exit thresholds can move. Do not use for live trading. Scheduled GitHub Actions may be delayed or skipped, and this is not a continuous 24/7 protective monitoring service.

## iPhone setup
1. Create an Alpaca PAPER account at https://app.alpaca.markets and obtain PAPER API key and secret.
2. Create a Telegram bot with @BotFather, send it `/start`, and retrieve `chat.id` from `https://api.telegram.org/bot<TOKEN>/getUpdates`. Keep the token private.
3. Create a PRIVATE GitHub repository with a README. Upload `bot.py`, `requirements.txt`, and `.github/workflows/paper-bot.yml` maintaining the exact paths. GitHub mobile web may require Safari desktop-site mode for uploading files and creating folders.
4. GitHub repository > Settings > Secrets and variables > Actions > **Secrets** > New repository secret. Create four secrets named exactly `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`.
5. Repository > Actions > Alpaca paper bot > Run workflow. Initially it runs in dry-run mode. Check the Actions logs and Telegram.
6. When ready to permit **paper** orders, go to Settings > Secrets and variables > Actions > **Variables** > New repository variable, name `ENABLE_ORDERS`, value `true`. Trigger another workflow manually and inspect the Alpaca paper dashboard.
7. To stop, change `ENABLE_ORDERS` to `false` AND disable the workflow in Actions. Inspect Alpaca paper positions and close manually if needed. Disabling the workflow stops automated software exits too.

## Notes
- The workflow checks at minutes 7,22,37,52 each hour. Actual execution can be delayed. GitHub free quotas and scheduling policies may change.
- Starting paper balance may be set to $250 for learning; real cash is not needed. Very small balances may not generate orders because of minimum order sizing.
- Supported initial symbols: BTC/USD, ETH/USD, SOL/USD. Stock support, backtesting, multi-timeframe scoring, journal persistence, and calibrated confidence are **not yet implemented**.
- The bot does not implement the entire Version 3 natural-language prompt; it implements only the deterministic subset described above.
- Orders are **submitted**, not assumed filled; check Alpaca's paper dashboard.
- Never share or commit API secrets, including in screenshots or logs.
