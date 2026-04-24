# Sell Side V3 Strategy: Indicator Documentation

This document explains the behavior of technical indicators in the **Sell Side V3 (Option Straddle)** strategy.

Each indicator can be configured independently for **Entry** (with specific toggles for the initial "Beginning" trade and subsequent "Re-entry" trades) and **Exit**.

---

## 1. RSI (Relative Strength Index)
The RSI measures the speed and change of combined straddle price movements.

### **Entry (Beginning & Re-entry)**
*   **Logic**: The combined price series of the CE and PE must have an RSI value **below** your configured threshold (e.g., < 50).
*   **ON (Beginning)**: The first trade of the day will wait until RSI conditions are met.
*   **ON (Re-entry)**: New straddles in the `CONTINUE` phase will only be placed if RSI is favorable.
*   **OFF**: RSI is ignored during entry selection.

### **Exit**
*   **Logic**: A Full Exit is triggered if the active straddle's RSI rises **above** your configured exit threshold (e.g., > 50).
*   **ON**: Protects against rapid price increases in the straddle.
*   **OFF**: RSI is not used for technical reversal exits.

---

## 2. VWAP (Volume Weighted Average Price)
VWAP represents the average price the straddle has traded at throughout the day, weighted by volume.

### **Entry (Beginning & Re-entry)**
*   **Logic**: The current combined price of the straddle must be **below** the combined VWAP.
*   **ON**: Ensures you are entering a straddle that is "cheap" relative to its daily average.
*   **OFF**: The bot may enter above the VWAP.

### **Exit**
*   **Logic**: A Full Exit is triggered if the combined price rises **above** the combined VWAP.
*   **ON**: Exits the trade when the straddle breaks above its daily average.
*   **OFF**: VWAP is not used to trigger technical exits.

### **VWAP Rise SL (1m Safety)**
*   This is a special high-frequency guard. If enabled, the bot will exit if the current VWAP rises by a certain percentage (e.g., +1.0%) from the lowest VWAP observed during the session.

---

## 3. V-Slope (Deceleration Logic)
V-Slope compares consecutive VWAP slopes to detect momentum changes.

### **Entry (Beginning & Re-entry)**
*   **Logic**: The current VWAP slope must be **lower** than the previous VWAP slope (Current Slope < Previous Slope).
*   **ON**: Ensures the straddle price rise is slowing down before you sell.
*   **OFF**: Momentum direction is ignored.

### **Exit**
*   **Logic**: A Full Exit is triggered if the VWAP slope becomes **positive/rising** on a macro timeframe (e.g., 15m).
*   **ON**: Detects straddle "price breakouts" early.
*   **OFF**: V-Slope momentum is not used for exits.

---

## 4. ROC (Rate of Change)
ROC measures the percentage change in straddle price over a specific number of periods. It is the primary tool for detecting "Neutral Momentum."

### **Entry (Beginning & Re-entry)**
*   **Logic**: The straddle ROC must fall **between** your configured limits to allow an entry.
    *   **Lower Limit (e.g., -5.0)**: The ROC must be **above** this value. This prevents entering during a sharp price crash (excessive downward momentum).
    *   **Upper Limit (e.g., +5.0)**: The ROC must be **below** this value. This prevents entering during a sharp price spike (excessive upward momentum).
*   **Candidate Selection**: During re-entry pool scans, the bot calculates the ROC for every straddle in the scan range.
    1.  It first filters out any strikes that are outside the range (e.g., if a strike has an ROC of +10 or -15, it is rejected).
    2.  From the remaining "safe" strikes, it selects the one where the **ROC is nearest to 0.0**. This ensures you are selling the straddle with the most stable, neutral momentum at that moment.
*   **ON**: Enables strict momentum gating and neutral-selection logic.
*   **OFF**: Entry is allowed regardless of ROC. Candidates are selected based on LTP balance alone.

### **Exit**
*   **Logic**: A Full Exit is triggered if the straddle's ROC breaks out of your defined macro safety zone.
    *   **Lower Limit (e.g., -40.0)**: If the straddle price drops extremely fast (ROC goes below -40), the bot exits to protect against high-velocity "flash" volatility.
    *   **Upper Limit (e.g., +20.0)**: If the straddle price spikes up (ROC goes above +20), a Full Exit is triggered immediately to cut losses during a breakout.
*   **ON**: Acts as a "Macro Volatility Stop Loss."
*   **OFF**: ROC volatility is ignored for exits.

---

## Phase-Specific Toggle Summary

| Phase | Scenario | Example Configuration |
| :--- | :--- | :--- |
| **Beginning** | Initial trade of the day. | You can turn **OFF** all indicators for "Beginning" to enter immediately on LTP balance, but keep them **ON** for Re-entry. |
| **Re-entry** | Subsequent trades after a Roll or Exit. | Typically uses **ON** for ROC and V-Slope to ensure the bot doesn't enter during high-volatility spikes. |
