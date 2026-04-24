# AlgoSoft V3 Strategy Documentation

This document provides a detailed technical explanation of the **Sell Side V3 Strategy** criteria, including entry logic, risk management (guardrails), and exit conditions.

---

## 1. Entry Gates (Beginning & Re-entry)
The entry gates are the "Master Filters" that decide when it is safe to sell a strangle. Both **Beginning Entry** (first trade of the day) and **Re-entry Gates** (subsequent trades) usually share the same logic.

### Criteria: `Price < VWAP(5m) AND ROC(5m, -10 to 10) AND Slope(5m)`

#### **A. Price < VWAP (5m)**
- **What it does:** Compares the current combined price (CE LTP + PE LTP) against the 5-minute Volume Weighted Average Price (VWAP).
- **Goal:** We only sell when premiums are "cheap" or "stable" relative to the day's volume. If Price > VWAP, it means premiums are inflating/exploding, and it is dangerous for a seller.
- **Scenario:**
  - Combined Price: 500 | VWAP: 510 -> **PASS** (Safe to sell)
  - Combined Price: 520 | VWAP: 510 -> **FAIL** (Stay out)

#### **B. ROC (5m, -10 to 10)**
- **What it does:** Measures the **Rate of Change** over the last 5 minutes.
- **Goal:** Ensures we don't enter during a period of extreme volatility. If the premium is jumping up or down by more than 10 points in 5 minutes, the bot waits for the market to calm down.
- **Example:**
  - 5 mins ago Price: 500 | Current: 505 (ROC = +5) -> **PASS** (Correct)
  - 5 mins ago Price: 500 | Current: 515 (ROC = +15) -> **FAIL** (Volatile)

#### **C. V-Slope (5m)**
- **What it does:** Measures the velocity and acceleration of premium movement.
- **Strict Momentum Rule:** A trade only passes if the **Current Slope < Previous Slope**.
- **Goal:** Ensures that premiums are not just negative, but are actively **accelerating downwards** or "rolling over."
- **Scenario:**
  - Prev Slope: -0.05 | Curr Slope: -0.08 -> **PASS** (Accelerating down)
  - Prev Slope: +0.10 | Curr Slope: +0.05 -> **PASS** (Topping out/Turning down)
  - Prev Slope: -0.08 | Curr Slope: -0.05 -> **REJECT** (Losing downward momentum)

---

## 2. Rollovers (Workflow Management)
Rollovers manage the trade while it is running. If the market moves too much, we "roll" to a new strike to remain balanced.

### Settings: `Target: 12.0% | Decay: 20 Pts | Ratio: 4.0`

- **Target (12%):** If your sold strangle value drops by 12% (e.g., from 500 to 440), the bot books the profit and rolls to a new ATM strike to continue collecting decay.
- **Decay (20 Pts):** Similar to the % target, but uses absolute points. If you collect 20 points of decay, you roll.
- **Ratio (4.0):** This is for **Delta Balancing**. If your CE is 80 and your PE is 20 (Ratio = 4.0), the trade is one-sided. The bot exits and enters a new balanced strangle (e.g., CE 50 / PE 50).

---

## 3. Guardrails (Global Risk Control)
Guardrails are "Kill Switches" that protect the entire session.

### **A. ROC Trend (15m)**
- **Logic:** `ON(15m, T: -2000.0 / SL: 1000.0)`
- **What it does:** Tracks the long-term trend of the market using a 15-minute window.
- **Scenario:** If the market starts crashing or mooning rapidly, the 15m ROC will hit the threshold. The bot will **close the trade** and start checking for a **fresh trade** once the market stabilizes. It does NOT stop for the day.
- **Toggle:** Setting these values to `0` or `OFF` disables this guardrail.

### **B. PnL Guardrail (Points)**
- **Logic:** `Target: 100.0 Pts | SL: -100.0 Pts`
- **What it does:** Monitors the cumulative points PnL for the entire day across all trades.
- **Scenario:**
  - You made 40 pts in Trade 1 and 60 pts in Trade 2. Total = 100 pts. The bot hits the **Target Guardrail** and **stops trading for the day** (Profit Protected).
  - You lose 100 points total. The bot hits the **Stoploss Guardrail** and **shuts down for the day** (Loss Limited).
- **Toggle:** Setting these to `0` or `OFF` disables the daily limit.

---

## 4. Dynamic Exits (Technical Reversals)
While Guardrails use PnL, **Dynamic Exits** use technical indicators to get you out *before* the Stoploss is hit.

### Criteria: `Price > VWAP(5m) AND RSI(5m, >50) AND ROC(5m, >10) AND Slope(5m)`
*Note: These are joined by "AND", meaning all indicators must signal a reversal for the exit to trigger.*

- **Scenario:** The market starts moving against your short position.
  1. **Price > VWAP:** Combined price is now "expensive" relative to volume.
  2. **RSI > 50:** Strong bullish momentum detected.
  3. **ROC > 10:** Price is rising rapidly.
  4. **Slope Rising:** The velocity of premiums is increasing (`curr_s > prev_s`).
- **Result:** When ALL are valid, the bot recognizes a "High Probability Reversal" and exits immediately to save capital.

---

## 5. V-Slope Pool Scan & Extra Re-entry

### **V-Slope Pool Scan & Startup Wait**

- **Startup Timing (Priming Wait):** To calculate momentum (is the trend getting faster or slower?), the bot requires **2 closed candles** of history for its slowest indicator. For example, if your V-Slope or RSI is set to a **5-minute** timeframe, the bot will wait for two 5-minute intervals (10 minutes total) after market open or bot startup.
  - *Calculation:* `Market Open (09:15) + (2 * 5 minutes) = 09:25 AM`.
  - The first trade evaluation will begin at **09:25 AM**.
- **Pool Scan (N x N Matrix):** When the **Re-entry Concept** is active (and specifically for the **Balanced Premium** metric), the bot performs an exhaustive scan of the pool range. Instead of picking one anchor and varying the other, it evaluates **all combinations** (Straddles & Strangles) of CE and PE strikes within the defined range (ATM ± Pool Range).
  - *Example:* If Pool Range = 2, the bot checks 25 unique combinations (5 CE strikes x 5 PE strikes).
  - Each combination is individually verified against technical rules (VWAP, RSI, etc.) before the best metric winner is selected.

### **V-Slope Extra Re-entry (Aggression)**
- **What it does:** This logic allows for re-entries even when RSI is at the threshold, provided the downward momentum (V-Slope < -0.1) is exceptionally strong.
- **Note:** No additional buffer is applied; the bot strictly respects the configured RSI threshold but uses the strong slope to justify the entry "on the edge."

---

## 6. Scalable TSL (Trailing Stoploss)
- **What it does:** As your profit grows, it moves your stoploss up.
- **Disabled:** In your current log, this is off, meaning you are using fixed PnL Guardrails instead of a trailing one.
