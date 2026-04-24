# Summary of Changes - Market Open & MCX Optimization

This document summarizes the changes made to align the bot with dynamic market open timings and optimize MCX (Crude Oil) trading behavior.

## 1. Dynamic Market Open Timing
Previously, the bot had several hardcoded references to `09:15` as the market open time. This caused issues for MCX instruments like CRUDEOIL, which start at `09:00`.

- **Affected Components**: `EngineManager`, `SellManagerV3`, `PositionManager`, `IndicatorManager`, `SignalMonitor`, `SignalEvaluator`, and `BreachGateManager`.
- **Logic**: The system now dynamically determines the market open anchor:
  - **MCX (CRUDEOIL, NATURALGAS, etc.)**: 09:00:00 AM.
  - **NSE/BSE**: 09:15:00 AM.
- **Why**: Ensures that S&R levels, VWAP anchors, and entry gates are initialized correctly at the actual start of the trading session for each instrument.

## 2. MCX Pricing Correction (Futures vs Index)
For MCX instruments, the Index price is often not traded or reliable for ATM selection. The user requested to use Futures prices instead.

- **Changes**:
  - `hub/price_feed_handler.py`: Updated `_handle_spot_feed` to use the Futures price as the anchor for `SPOT_PRICE_UPDATE` events for MCX instruments.
  - `hub/sell_manager_v3.py`: Refined ATM calculation to strictly use Futures price (`state_manager.spot_price`) for MCX.
- **Why**: Accurate ATM selection and indicator calculations for commodities.

## 3. V3 Re-entry Pool Scan Optimizations
- **ATM Proximity Sorting**: The pool scanner now calculates the distance to the exact market price (Futures for MCX) rather than a rounded ATM value. It sorts candidates by distance (ascending) to prioritize the absolute nearest straddle.
- **Independent Entry/Exit Timeframes**: Introduced support for independent `rsi_entry.tf`, `rsi_exit.tf`, `vwap_entry.tf`, and `vwap_exit.tf` settings in the V3 Sell strategy. This allows momentum (RSI) and price positioning (VWAP) to be evaluated on different horizons for entry vs exit.
- **RSI Calculation Efficiency**: Optimized the re-entry pool scan to use `skip_api=True` for RSI. The bot will now wait for live data to accumulate for newly discovered strikes instead of triggering slow REST API calls that could hang the bot.

## 4. UI and Documentation Updates
- **UI Labels**: Renamed "Range 9:15 Gate" to **"Market Open Gate"** in the Strategy Settings dashboard to be instrument-agnostic.
- **Documentation**: Updated `MARKET_OPEN_BEHAVIOR.md`, `SELL_V3_WORKFLOW.md`, `SELL_V3_STRATEGY_EXPLANATION.md`, and `VWAP_PEAK_CALCULATION.md` to reflect the dynamic timing and wait periods.
- **Verification Scripts**: Updated `verify_sr_api.py` and `verify_sr_levels.py` to support dynamic open times for verification.

## 5. Affected Sections Summary
| Section | Files Modified |
| :--- | :--- |
| **Core Logic** | `hub/engine_manager.py`, `hub/sell_manager_v3.py`, `hub/indicator_manager.py`, `hub/signal_monitor.py`, `utils/support_resistance.py` |
| **Data Handling** | `hub/price_feed_handler.py` |
| **Web UI** | `web/templates/strategy.html` |
| **Documentation** | `MARKET_OPEN_BEHAVIOR.md`, `SELL_V3_WORKFLOW.md`, `SELL_V3_STRATEGY_EXPLANATION.md`, `VWAP_PEAK_CALCULATION.md` |
| **Scripts** | `scripts/verify_sr_api.py`, `scripts/verify_sr_levels.py` |

## 6. Improved Exit Commentary
To provide better transparency in the order book, the V3 strategy now provides detailed reasons for trade closures.

- **Changes**:
  - `hub/sell_manager_v3.py`: Refined `Technical Signal Reversal` and `VWAP Slope Rise` exit reasons to include real-time technical values (RSI, Price, VWAP).
- **Why**: Allows users to understand exactly which technical condition triggered a full exit when reviewing their trade history.
