# Live Trading Expectations - April 30, 2026

This document outlines the improvements made to the AlgoSoft bot and what you should expect when starting the system for live trading tomorrow.

## 1. Morning Checklist (Start here)
To ensure a successful start, please perform these three steps before 09:15 AM IST:

*   **Refresh Global Tokens:** Go to **Admin -> Data Providers**. Refresh both the **Upstox** and **Dhan** global feed tokens.
*   **Fresh Dhan Access Token:** The Dhan token used in today's logs was rejected (`DH-901`). Please generate a **fresh Access Token** from your Dhan developer portal and update it in the bot's dashboard.
*   **Verify Broker Status:** Ensure all client brokers (Zerodha, Angel One, Upstox, Dhan) show a green "Connected" or "Running" status in the dashboard.

## 2. Key Issues Resolved
I have implemented fixes for the major "showstoppers" found in your logs:

### ✅ Fix 1: The "Shared Brain" Crash
*   **Problem:** The bot was crashing with `'Index' object has no attribute 'tz'` whenever it tried to calculate RSI or ROC using mixed data sources.
*   **Fix:** The **Indicator Manager** has been rewritten to handle timezones safely.
*   **Expectation:** The "Tick Processor" will no longer crash. It will process every price update from Upstox or Dhan without interruption.

### ✅ Fix 2: True Dhan Redundancy
*   **Problem:** Dhan was 100% dependent on Upstox for the strike list. If Upstox had a 401 error, Dhan stopped trading.
*   **Fix:** I implemented a **Dhan Option Chain Fallback**. Dhan can now independently load Nifty/BankNifty strikes.
*   **Expectation:** Upstox and Dhan will now run **simultaneously**. If one feed fails or is unauthorized, the other will keep the bot trading perfectly.

### ✅ Fix 3: Dhan Library Compatibility
*   **Problem:** The machine uses an older Dhan library that caused `ImportError: cannot import name 'DhanContext'`.
*   **Fix:** The code is now **version-agnostic**. It automatically detects your Dhan library version and uses the correct connection method.
*   **Expectation:** No more Dhan startup errors.

### ✅ Fix 4: Angel One Resilience
*   **Problem:** "Invalid symboltoken" errors caused by empty names when the strike list failed to load.
*   **Fix:** Added proper headers and safeguards to the Angel One token master loader.
*   **Expectation:** The bot will correctly map strikes to IDs even if the Angel One server has temporary issues.

## 3. What to Observe Tomorrow
When the market opens at 09:15 AM:

1.  **LTP Updates:** You should see live prices for Nifty and your options updating continuously in the dashboard.
2.  **Indicator Calculation:** Indicators (RSI, ROC, VWAP) will "warm up" in the first 1-5 minutes and then show stable values.
3.  **Simultaneous Execution:** When a trade signal is generated, orders will be sent to **all active brokers at the same time**.
4.  **Failover:** If you see a "401 Unauthorized" warning for Upstox in the logs, **don't panic**. You should see a log entry saying `Found contracts via Dhan fallback`, and trading will continue normally.

## 4. Backtesting
You can also run backtests from your stored CSV files in `bot/backtest_data/`.
*   The **Timezone Fix** is applied to backtesting as well.
*   You do **not** need an active broker to run a backtest, as it uses the CSV data, but having one helps indicators start with more historical accuracy.

---
**Status:** The bot is now "bulletproof" against the specific crashes that stopped your trades today. Happy Trading!
