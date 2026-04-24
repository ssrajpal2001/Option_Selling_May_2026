# Changelog - Today's Enhancements & Fixes

## 1. High-Fidelity Backtest Support
*   **Enriched Data Ingestion:** Overhauled `IndicatorManager` and `BacktestOrchestrator` to ingest strike-specific technical metrics (ATP, LTP, RSI, ROC) from `atp_data_*.csv` files.
*   **Strike-Level Precision:** Each option leg in a backtest now uses its own unique momentum and trend data instead of relying on generic index proxies.
*   **Synthetic OHLC Injection:** Implemented automated creation of 1-minute OHLC candles from ATP/LTP history to support standard technical indicators (RSI, ROC) in simulations.

## 2. Robust Indicator Logic
*   **Cold-Start Solved:** Fixed a critical "NoOHLC" issue where indicators failed immediately after market open.
*   **Index Proxy Fallback:** Enhanced `VWAP` and `RSI` rules to automatically use underlying index data as a proxy during the "Priming Wait" period (first 2-10 minutes of a session).
*   **V-Slope Refinement:** Standardized the `V-Slope` calculation to strictly use finalized candle boundaries (T-1, T-2), eliminating look-ahead bias and ensuring reliable trend detection.
*   **Timezone Alignment:** Ensured all timestamp lookups and data processing explicitly use the `Asia/Kolkata` timezone, preventing data-miss errors in historical simulations.

## 3. Enriched Order Book & UI
*   **Real-Time Dashboard Metrics:** "Live Metrics" now flow correctly to the UI with accurate ROC, RSI, and Price vs VWAP values.
*   **Enriched Running Order Book:** Updated the dashboard to display:
    *   **Entry Time & Exit Time:** Precise timestamps for every trade leg.
    *   **Technical Snapshots:** Real-time values of RSI, ROC, and V-Slope at the exact moment of entry and exit.
    *   **Trade Reasons:** Clear explanations of which technical gate (e.g., "ROC Pass", "VWAP Rise SL") triggered the trade.
*   **Mobile-Friendly Improvements:** Improved column formatting in `dashboard.html` and `admin_backtest.html`.

## 4. Stability & Persistence
*   **Database Schema Update:** Added `entry_indicators` and `exit_indicators` columns to the `trade_history` table in SQLite (`web/db.py`) for permanent storage of technical snapshots.
*   **Persistence Integration:** Modified `SellManagerV3` to capture and store detailed technical state during trade execution for post-trade analysis.
*   **Phantom Trade Prevention:** Improved order handling in live mode to ensure the bot only tracks trades if broker order placement actually succeeds.

## 5. Critical Bug Fixes
*   **Resolved `UnboundLocalError`:** Fixed variable scoping issue in `hub/status_writer.py` that crashed the UI logger.
*   **Resolved `NameError`:** Fixed missing variable in `hub/sell_v3/dashboard_logic.py`.
*   **Resolved `IndentationError`:** Corrected a logic block in `hub/indicator_manager.py`.
*   **Expiry Resolution:** Fixed "No option expiries found" error by forcing simulation dates to be treated as "today" in backtests.

---
**Status:** The system is now stable, data-accurate, and providing full visibility into the strategy's technical decision-making process.
