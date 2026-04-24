# Why is the Data Provider used in Backtest Mode?

Even when running simulations using local CSV files, the trading bot still requires a connection (or at least a configuration) for a Data Provider (e.g., Zerodha/Kite). This document explains the technical reasons behind this design.

## 1. Instrument Master (Contract Resolution)
Option tokens (e.g., `123456`, `789012`) are dynamic and change every week/month based on the exchange's expiry cycle.
- **The Problem:** A strike price like `NIFTY 24000 CE` will have a different internal ID today than it did last year.
- **The Solution:** The bot uses the Data Provider to fetch the "Instrument Master" for the specific backtest date. This allows the bot to map human-readable strikes to the exact historical data keys stored in your CSV files.

## 2. Historical Look-back for Indicators
Indicators like **VWAP**, **RSI**, and **ROC** are "stateful"—they depend on data that happened *before* the current moment.
- **The Problem:** If you start a backtest at 09:15, the bot needs to know the VWAP or RSI calculated from 09:00 or even the previous day to be accurate at 09:16.
- **The Solution:** The Data Provider handles fetching these "pre-roll" candles (the look-back period) so your technical gates (e.g., "RSI < 40") are calculated correctly from the very first minute of the simulation.

## 3. Fallback Mechanism (Gap Filling)
Local CSV files might occasionally have missing minutes or data gaps due to recording interruptions.
- **The Problem:** If a specific strike price is missing from the local file, the backtest would typically crash or show "Nothing" in the UI.
- **The Solution:** The `BacktestOrchestrator` uses the Data Provider as a secondary source. If data isn't found locally, it attempts to fetch it from the provider's official historical API to ensure the simulation continues smoothly.

## 4. Offline Mode Support
If you do not have an active internet connection or a valid session, the bot is designed to be resilient:
- **Resilience:** If `backtest_enabled` is set to `True` in the configuration, the `ApiClientManager` will log a warning instead of a fatal error if the login fails.
- **Pure Offline:** In this state, the bot will rely **entirely** on your local CSV files. However, you must ensure that your CSV files contain all the necessary strikes and pre-roll data, or indicators may start with `None` values.

---

### Key Log Indicators
When a backtest starts, you will see these in `logs/backtest_ui.log`:
- `[Backtest] Loading instrument master...` (Provider used)
- `[Backtest] Pre-fetching 100 candles for RSI look-back...` (Provider used)
- `[Backtest] ATP file not found... using synthetic OHLC.` (Fallback mode)
