# V3 Sell Side Strategy: Backtest Logic & Workflow

This document explains the complete logic, workflow, and rules governing the **Sell Side V3 Strategy** during backtesting. It specifically addresses how entries, exits, and re-entries are handled, and why certain behaviors (like delayed re-entries) occur.

---

## 1. Strategy Overview
The V3 Sell Side strategy is designed to capture market decay by selling option straddles (or strangles) and managing them through technical indicators (RSI, VWAP, V-Slope).

### Core Components:
- **`SellManagerV3`**: The main logic controller for V3.
- **`BacktestOrchestrator`**: Manages the simulation loop, feeds historical data, and tracks PnL.
- **`IndicatorManager`**: Calculates technical signals (RSI, VWAP, VWAP Slope) using historical bars.

---

## 2. The Backtest Loop
In backtest mode, the bot runs a synchronous loop:
1.  **Data Loading**: Historical 1-minute OHLC data and ATP (Average Traded Price) are loaded for the target date.
2.  **Tick Processing**: For every 1-minute "tick", the orchestrator updates prices, feeds indicators, and calls the strategy manager.

---

## 3. Entry Logic (Beginning Phase)
When the bot starts or a new day begins, it enters the `BEGINNING` workflow phase.

### Rules:
1.  **Market Open Timing**: The bot starts evaluating entries after the instrument's market open time (e.g., 09:15 for NIFTY).
2.  **Strict 2-Candle Wait (Priming)**:
    - **EVERY entry must be gated.** There are no ungated entries, even at market open.
    - The bot waits for at least **2 full finalized candles** of the configured `v_slope_entry.tf` (e.g., 2 minutes for a 1m TF) after market open before any trade is evaluated.
    - For a 1m TF starting at 09:15, the first possible entry signal is evaluated at **09:17:00**.
    - **Note:** The real-world "Bot Startup Wait" is completely ignored in backtest mode.
3.  **One Entry Per Candle**: The bot uses a "Bucket" system based on the Entry TF.
    - If an entry occurs at 09:17, no new entry can occur until the timestamp reaches the next bucket (e.g., `09:18:00`).
4.  **Workflow Jump**: If the initial balanced entry condition fails or no suitable strikes are found, the bot **immediately** (within the same tick) jumps to the `CONTINUE` phase to perform a pool scan.
5.  **Strictly Lower Balancing**: In the beginning phase, the bot uses a specific balancing rule:
    - It identifies the cheaper of the two ATM legs (>= 50 LTP) at ATM as the anchor.
    - It then shifts the other leg OTM until its LTP is **strictly lower** than the anchor.
    - This pair must pass all technical gates (V-Slope, RSI, VWAP) to be entered.

---

## 4. Re-entry Logic (Continue Phase)
Once the initial position is closed, the bot enters the `CONTINUE` phase.

### Rules:
1.  **Timeframe Boundary Constraint**: Re-entry scans occur on every boundary of the configured `v_slope_entry.tf` (default **1m**).
2.  **V-Slope Pool Scan**:
    - The bot evaluates all straddle combinations in the **ATM ± reentry_offset** range (default 2).
    - The `v_slope_pool_offset` (default 10) is used only for the internal subscription pool (ATP storage).
    - Re-entry candidates are strictly sorted by **ATM Proximity** (absolute distance from market price).
3.  **Technical Gates (1m TF)**: For a strike pair to be chosen, it must pass:
    - **V-Slope Gate**: `Current Slope <= Previous Slope` (Slope is falling or flat).
    - **RSI Gate**: `Combined RSI < 50` (using `rsi_entry.tf`).
    - **VWAP Gate**: `Combined Price < Combined VWAP` (using `vwap_entry.tf`).
4.  **Cooldown (Bypassed in Backtest)**: The 60-second real-clock cooldown between trades is ignored in backtest, but the **Bucket Rule** still applies.

---

## 5. Exit Logic
The bot monitors several conditions for a "Full Exit" or "Smart Roll":

1.  **VWAP Rise SL (1m TF)**: Fast stop-loss if Combined VWAP rises from the session low by more than the threshold (e.g., 1.0%).
    - Checked every minute.
    - Requires at least 2 full finalized 1m candles since market open.
2.  **Technical Reversal / Slope Reversal (Macro TF)**: Stable trend-following exit.
    - **Boundary Rule**: Strictly evaluated **ONLY on macro boundaries** (default 15m: XX:00, XX:15, XX:30, XX:45).
    - **Macro Ready Rule**: An exit is only possible after **3 macro boundaries** have passed since the boundary of position entry.
        - *Example:* If entered at 09:30, boundaries are 09:45 (1), 10:00 (2). The first possible technical exit check is at **10:15:00** (3).
    - Controlled by `exit_mode` (RSI_ONLY, VWAP_ONLY, RSI_AND_VWAP, RSI_OR_VWAP, VSLOPE_ONLY).
    - **RSI**: Uses `rsi_exit.tf` and `rsi_exit.threshold`.
    - **VWAP**: Uses `vwap_exit.tf`.
    - **Slope Exit**: If 15m V-Slope is rising (Current > Previous), and mode includes VSLOPE.
3.  **Ratio Breach (3:1)**: **FULL EXIT**. If one leg becomes 3x the price of the other, the bot exits completely and returns to re-entry scanning.
4.  **Smart Roll**: Triggered by Profit Target (12%) or LTP falling below 20.
    - A "Physical Roll" closes the old position and enters a new one immediately.
    - A "Virtual Roll" simply resets the entry price if the strikes remain the same.

---

## 6. Logging in Backtest
- **Throttling**: Status logs are throttled to once per minute (simulated time).
- **Boundary Scan Visibility**: On every timeframe boundary, the bot **always** logs the full "Pool Scan Report" showing every strike combination checked and why it passed or failed.
- **Waiting Logs**: If an entry scan is skipped during priming, the bot logs:
    - `[SellManagerV3] Market Open Wait: Waiting until 09:17:05 for V-Slope anchors.`
