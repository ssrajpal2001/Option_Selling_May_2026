# Indicator Logic & Chart Discrepancy Guide

This document explains the technical implementation of indicators in the V3 Strategy and addresses why values in the bot logs may differ from your trading charts.

---

## 1. Indicator Technical Logic & Formulas

The bot calculates all indicators on the **Combined Straddle Premium** (Total Price = Call LTP + Put LTP) to ensure a stable technical signature of the position.

### **VWAP (Volume Weighted Average Price)**
*   **Formula:** `VWAP = sum(Price * Volume) / sum(Volume)`
*   **Implementation:**
    *   **In Live Mode:** Calculates based on tick-by-tick data (Price & Volume) received from the broker.
    *   **In Backtest Mode:** Strictly uses the **Exchange-reported ATP (Average Traded Price)** from high-fidelity CSV data. This ensures 1:1 alignment with the actual exchange-calculated VWAP.
*   **Straddle Logic:** The bot adds the individual VWAPs of both legs.
    *   `Combined VWAP = CE_VWAP + PE_VWAP`

### **ROC (Rate of Change)**
*   **Formula:** `ROC = 100 * (Source - Source[length]) / Source[length]`
*   **Source:** Total Straddle Premium (`LTP_CE + LTP_PE`).
*   **Default Length:** 9 periods (candles).
*   **Logic:** Measures the speed of premium expansion.
    *   Positive ROC: Premiums are rising/expanding (dangerous for sellers).
    *   Negative ROC: Premiums are falling/decaying (profitable for sellers).

### **RSI (Relative Strength Index)**
*   **Formula (Wilder's Smoothing):**
    1.  `Delta = Price - Previous_Price`
    2.  `Gain = max(Delta, 0)`, `Loss = max(-Delta, 0)`
    3.  `Avg_Gain = EMA(Gain, alpha=1/period)`
    4.  `Avg_Loss = EMA(Loss, alpha=1/period)`
    5.  `RS = Avg_Gain / Avg_Loss`
    6.  `RSI = 100 - (100 / (1 + RS))`
*   **Input:** Close prices of the combined straddle.
*   **Stabilization:** Uses a 14-period lookback. The bot utilizes a 42-candle buffer (3x period) to ensure the Wilder's RMA is stable before triggering a trade.

### **V-Slope (VWAP Slope)**
*   **Formula:** `Slope = Current_Combined_VWAP - Previous_Combined_VWAP`
*   **Logic:** Measures the momentum of the VWAP.
    *   `PASS` if `Current_Slope <= Previous_Slope`: Indicates the expansion is losing steam (Deceleration).
    *   `FAIL` if `Current_Slope > Previous_Slope`: Indicates aggressive price movement (Momentum).

### **ATR (Average True Range)**
*   **Formula:** `ATR = SMA(True Range, Length)`
*   **True Range:** `max(High - Low, abs(High - PrevClose), abs(Low - PrevClose))`
*   **Usage:** Used for volatility-adjusted Stop Losses.

---

## 2. Risk Management & Guardrails

### **ROC Guardrail (15m)**
Calculated every 15 minutes to prevent holding through massive spikes.
*   **Profit Target:** Exit if `ROC <= Negative_Target` (e.g., -20%).
*   **Stop Loss:** Exit if `ROC >= Positive_SL` (e.g., +10%).

### **PnL Guardrail (Points)**
Real-time monitoring of absolute point PnL.
*   `PnL_Points = (Sum of Entry Prices) - (Sum of Current LTPs)`
*   If `PnL_Points >= Target` or `PnL_Points <= StopLoss`, the bot squares off.

### **VWAP Rise SL**
Monitors the "bounce" of the straddle premium from its session low.
*   `Rise_% = (Current_VWAP - Session_Min_VWAP) / Session_Min_VWAP * 100`
*   If premium recovers too much (e.g. 1%), it suggests a trend reversal.

---

## 3. Why Bot Logs differ from Charts

### **A. Timeframe (TF) Mismatch**
The bot defaults to **1-minute** candles for entry/exit sensitivity. Most charts show **5m** or **15m**.
*   A 1-minute spike is averaged out over 5 minutes on a chart.
*   Always check the log label: `ROC(1m)` vs `ROC(5m)`.

### **B. Combined vs. Single Leg**
Standard charts show RSI for **one strike** (e.g., 22500 CE). The bot calculates RSI on the **Sum of CE + PE**. These values will never match a single-leg chart.

### **C. Closed Candles vs. Live Data**
To avoid "Look-ahead Bias", the bot only acts on **Finalized Candles**.
*   A rule might "Pass" on your live chart at 10:14:30 but "Fail" at 10:14:59 when the candle closes.
*   The bot only checks rules at the end of the candle.

### **D. Exchange ATP vs. Candle Close**
The bot's VWAP uses **Average Traded Price (ATP)**, which includes every single trade on the exchange. Chart VWAP often uses only the **OHLC Close**, which is an estimate. The bot is more accurate.
