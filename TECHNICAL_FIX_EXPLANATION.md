# Technical Fix Explanation: VWAP vs. Close Logic

This document explains the technical changes implemented to ensure the Sell V3 strategy correctly evaluates technical rules, specifically addressing the discrepancy between "Live" and "Intraday Historical" data during signal generation.

## 1. The Core Issue: Look-ahead Bias
In your logs, we observed cases where a trade was taken despite the chart showing that the condition (e.g., `VWAP > CLOSE`) should have failed at the candle close.

**Reason:**
The bot evaluates entry rules at the **Boundary + 5 seconds** (e.g., at 09:21:05 for a 1-minute candle that closed at 09:21:00).
*   **CLOSE:** The bot correctly retrieved the finalized `CLOSE` price of the 09:20 candle (recorded at 09:20:59).
*   **VWAP:** Previously, the bot was retrieving the **LATEST** Average Traded Price (ATP) from the exchange at **09:21:05**.

If the price moved significantly in those first 5 seconds of the **new** candle, the "Live" VWAP shifted instantly. This caused a rule that *should* have failed at 09:21:00 to "pass" at 09:21:05 because it was using "data from the future" (the current active candle). This is known as **Look-ahead Bias**.

## 2. The Implementation Fix
I have modified the **Indicator Manager** and **Price Feed Handler** to ensure the bot uses **Current Intraday snapshots** instead of live instantaneous data for historical candle checks.

### A. Gated "Current Data" Logic
The bot now distinguishes between "Live" requests and "Historical Intraday" requests:
*   **Candle Boundary Check:** When evaluating a finished candle (e.g., checking the 09:20 candle while the time is 09:21:05), the bot is now **strictly forbidden** from using the latest live tick price.
*   **Intraday Snapshot Lookup:** Instead, it looks up the **exact Intraday VWAP snapshot** that was recorded at the moment that specific minute ended (`09:21:00:00`).
*   **OHLC Fallback:** If a snapshot is missing, it reconstructs the VWAP using only the 1-minute candles from **today**, ensuring it never "leaks" data from the current active minute into the decision for the previous minute.

### B. Standardized Data Storage (Type-Safe)
To ensure the bot never misses a snapshot due to technical mismatches (e.g., comparing a standard Python time to a Pandas high-precision timestamp), I have standardized the internal storage:
*   Every price update is now saved using a **standardized timezone-aware key** (Asia/Kolkata).
*   The lookup logic has been made **type-agnostic**, so it can find the correct VWAP regardless of how the strategy requests it.

## 3. What to Expect in Live Trading
When you set a rule such as `VWAP(1m) > CLOSE(1m)`:
1.  **Waiting for Close:** The bot waits for the 1-minute candle to fully finish (e.g., at 09:21:00).
2.  **Execution Pulse:** It performs the check at 09:21:05.
3.  **Data Retrieval:**
    *   It fetches the **final Close** of the 09:20 candle.
    *   It fetches the **Intraday VWAP snapshot** at 09:21:00 (the exact value at the close of the 09:20 candle).
4.  **Comparison:** It compares these two finalized values.

**Result:** The bot's trade decision will now strictly match what you see on the chart at the moment the candle closes. It will no longer be "fooled" by price spikes that happen in the first few seconds of a new candle.
