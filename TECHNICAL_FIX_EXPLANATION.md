# Technical Fix Explanation: VWAP vs. Close Logic

This document explains the technical changes implemented to ensure the Sell V3 strategy correctly evaluates technical rules, specifically addressing the discrepancy between "Live" and "Historical" data during signal generation.

## 1. The Core Issue: Look-ahead Bias
In your logs, we observed cases where a trade was taken despite the chart showing that the condition (e.g., `VWAP > CLOSE`) should have failed.

**Reason:**
The bot evaluates entry rules at the **Boundary + 5 seconds** (e.g., at 09:21:05 for a 1-minute candle that closed at 09:21:00).
*   **CLOSE:** The bot correctly retrieved the finalized `CLOSE` price of the 09:21:00 candle.
*   **VWAP:** Previously, the bot was retrieving the **LATEST** Average Traded Price (ATP) from the exchange at 09:21:05.

If the price moved significantly in those first 5 seconds of the new candle, the "Live" VWAP shifted, causing a rule that *should* have failed at 09:21:00 to "pass" at 09:21:05. This is known as **Look-ahead Bias**.

## 2. The Implementation Fix
I have modified `IndicatorManager.calculate_vwap` and `PriceFeedHandler` to ensure absolute synchronization between the Close price and the VWAP.

### A. Gated ATP Retrieval
The bot now uses **Gated Fallback** logic:
*   **Historical Requests:** When evaluating a closed candle (e.g., checking the 09:20:00 boundary while the time is 09:21:05), the bot is **forbidden** from using the latest live tick price.
*   **Snapshot Lookup:** Instead, it looks up the exact intraday VWAP snapshot stored in `atp_history` at the specific minute boundary (`09:21:00:00`).
*   **OHLC Fallback:** If the snapshot is missing, it reconstructs the VWAP from 1-minute OHLC data for that specific day, ensuring it never "leaks" data from the future (the current active candle).

### B. Standardized Data Storage
To ensure the bot never misses a snapshot due to technical "type mismatches" (e.g., comparing a Python `datetime` to a Pandas `Timestamp`), I have standardized the `atp_history` storage:
*   All price updates are now keyed using standardized `pd.Timestamp` objects in the **Asia/Kolkata** timezone.
*   This ensures that when the Strategy asks "What was the VWAP at 09:21:00?", the lookup is instant and 100% accurate.

## 3. What to Expect in Live Trading
When you set a rule such as `VWAP(1m) > CLOSE(1m)`:
1.  The bot will wait for the 1-minute candle to finish.
2.  At the "Pulse" (5 seconds into the next minute), it will look back.
3.  It will fetch the **final Close** of that 1m candle.
4.  It will fetch the **Intraday VWAP snapshot** at the exact moment that candle closed.
5.  It will compare these two values.

This ensures the bot's decision matches what you see on the chart at the close of the candle, regardless of what happens in the few seconds after the boundary.
