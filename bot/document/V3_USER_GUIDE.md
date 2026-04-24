# AlgoSoft V3 Strategy: User Guide & Workflow Documentation

This document provides a comprehensive guide to the **Sell Side V3 Strategy**, explaining every configuration option in the UI and the underlying logic workflow.

---

## 1. The V3 Trade Lifecycle

The V3 engine follows a strict, state-aware lifecycle to ensure data stability and capital protection.

### Stage A: Startup & Priming Wait
When the bot starts or a new session begins at Market Open:
1.  **Start Time Gate:** The bot ignores all data until the configured `Start Time` (e.g., 09:20).
2.  **Indicator Priming:** Technical indicators like RSI and ROC are "warmed up" using historical data from the REST API.
3.  **Priming Wait:** The bot waits for a minimum number of **closed candles** to calculate momentum-based indicators:
    *   **VWAP/ROC/RSI:** Requires 1 closed candle (e.g., at 09:16 for 1m TF).
    *   **V-Slope:** Requires **2 closed candles** to establish a trend (e.g., at 09:17 for 1m TF).
    *   *Note:* The bot will display a "Priming: Waiting..." status on the dashboard during this period.

### Stage B: Synchronized Pulse (The "05s" Rule)
To ensure technical signals are evaluated on **finalized data** and not "repainting" candles, the V3 engine "pulses" its evaluation:
*   **Timing:** Evaluation occurs at the **Minute Boundary + 5 seconds** (e.g., 09:20:05, 09:21:05).
*   **Logic:** The 5-second buffer allows the data feed to finalize the previous candle's OHLC and VWAP before the bot makes a decision.

### Stage C: Entry Selection & Gating
1.  **Concept Selection:** Based on the `Entry Workflow Mode`, the bot selects strikes using either the **Beginning Concept** (ATM-focused) or **Re-entry Concept** (Pool-focused).
2.  **Rule Engine Evaluation:** The candidate pair is passed through the `Entry Rules`. These rules (VWAP, RSI, etc.) are checked on a **Closing Basis** (using the last finalized candle).
3.  **Execution:** If all rules pass, the bot places the orders across all active brokers.

### Stage D: Active Management
While a trade is live, the bot monitors:
1.  **Tick-by-Tick Guardrails:** Session PnL, Single Trade SL, and VWAP Rise SL are checked on every price update.
2.  **Boundary-Based Management:** Rollovers (Smart Rolling), LTP Decay, and Technical Exit Rules are checked at the minute boundaries.

### Stage E: Exit & Cooldown
1.  **Exit Trigger:** If any exit condition is met, the bot squares off the position.
2.  **Cooldown:** A mandatory 60-second cooldown is applied before the bot starts scanning for the next entry.
3.  **Stop for Day:** If a **Global Guardrail** (Session PnL) is hit, the bot shuts down for the day to protect capital.

---

## 2. Configuration Reference (Sell Side V3 Tab)

### **V3 Strategy Status (General)**
*   **V3 Mode (Master Switch):** Enables the V3 engine globally. When ON, legacy Buy/Sell toggles are bypassed.
*   **Start Time:** The time the bot begins active monitoring (e.g., `09:20:00`).
*   **Entry End Time:** No new trades will be initiated after this time (e.g., `14:00:00`). Existing trades will continue to be managed.
*   **Bot Close Time:** Final square-off time for the session (e.g., `15:20:00`).
*   **Max Trades:** Maximum number of trades allowed per day. Set to `0` for unlimited.
*   **Pool Range:** Number of strikes to scan above and below ATM during a **Pool Scan** (e.g., `4` means a 9-strike scan).
*   **LTP Target:** The minimum premium value required for an anchor strike (e.g., `50.0`).
*   **Entry Workflow Mode:**
    *   `Hybrid`: Uses *Beginning Concept* for the first trade and *Re-entry Concept* for all subsequent trades.
    *   `Beginning Only`: Always uses the ATM-focused selection logic.
    *   `Re-entry Only`: Always uses the Pool Scan logic.
*   **Best Strike Metric:** How the bot chooses the "winner" among strikes that pass technical gates in a Pool Scan:
    *   `Closest to Target ROC`: Selects the strike with the most stable/neutral momentum.
    *   `Min VWAP % Deviation`: Selects the strike where price is most "discounted" relative to volume.
    *   `Balanced Premium`: Selects the pair with the smallest price difference (`ABS(CE-PE) / (CE+PE)`).
*   **Multi-Strike Scan**: *Automatically managed by the Pool Range setting.*
*   **Strict LTP Balancing**: *Internal logic now prioritizes the 'Best Strike Metric' for optimal entry.*

---

### **Rule Builder (Entry & Exit Rules)**
The Rule Builder allows for complex logical conditions using `AND`, `OR`, and `Parentheses`.

#### **Supported Indicators:**
*   **VWAP:** Compares Combined Price to Combined VWAP.
*   **Slope:** Measures the momentum of the VWAP. `Pass` if the trend is decreasing (Slope <= 0).
*   **RSI:** Relative Strength Index on the combined premium.
*   **ROC:** Rate of Change (Volatility filter).
*   **Advanced Rule Builder:** Allows you to compare two specific operands.
    *   **Operands:** `LTP` (Live Price), `CLOSE` (Finalized Candle Close), `VWAP` (Session Volume Average), `RSI`, `ROC`, `SLOPE`, and `VALUE` (Fixed Number).
    *   **Operators:** `>`, `<`, `>=`, `<=`, `==`.
    *   **Example:** `RSI > VALUE(60)` AND `CLOSE < VWAP`.

#### **Evaluation Logic:**
*   **Closing Basis:** All technical indicators are evaluated using the **Close** price of the last finalized candle of the selected timeframe.
*   **Short-Circuiting:** If an `AND` condition fails, the bot stops evaluating further rules in that set to save processing time (unless an `OR` is present).

---

### **V3 Rollover / Smart Roll**
*   **Smart Rolling (Master):** If ON, the bot will transition between trades without a cooldown if the rollover criteria are met.
*   **Profit Target (%):** If the trade reaches this profit (e.g., 12%), the bot will perform a **Smart Roll**.
*   **LTP Decay Enabled:** If one side of the strangle decays below the `LTP Exit Min` (e.g., 20.0), the bot rolls to a higher strike to maintain decay speed.
*   **Ratio Exit:** If the price of one leg is `N` times larger than the other (e.g., 3:1), the trade is considered unbalanced. The bot will exit and re-scan.

---

### **Risk Management (Guardrails)**
Guardrails are the ultimate safety filters.

*   **Single Trade Risk (Points):** Immediate Exit if the current trade's PnL hits the **Target** or **Stop Loss** points. These can be overridden by **Day-Wise** settings.
*   **Scalable TSL (Trailing SL):**
    *   **Base Profit:** Profit level where the lock activates.
    *   **Base Lock:** Amount of profit to protect initially.
    *   **Step Profit:** Increment of additional profit required to move the lock.
    *   **Step Lock:** Amount to increase the trailing lock by.
    *   *Example:* After 1000 profit, lock 200. Every +250 profit, increase lock by 200.
*   **Global Session PnL:** Monitors the total points PnL for the entire day. If hit, the bot **stops for the day**.
*   **ROC Trend SL:** Uses a macro timeframe (e.g., 15m) to detect market-wide spikes or crashes.
*   **VWAP Rise SL:** Exits if the combined premium recovers/bounces by more than `N%` from its session low (detects trend reversals).

---

## 3. Advanced Logic Glossary

| Feature | Technical Definition |
| :--- | :--- |
| **Beginning Concept** | Selects the ATM strike with the **Greater LTP** as the Anchor. Then finds a partner on the opposite side with an LTP strictly **Lower than the Anchor**. |
| **Re-entry Concept** | Scans all strike pairs within the `Pool Range`. Filters those that fail technical gates. Selects the "Best" based on the `Best Strike Metric`. |
| **Virtual Roll** | If a rollover is triggered but the *same* strikes are still the best candidates, the bot simply resets the entry price "anchor" without placing new orders. |
| **WaitData Status** | Occurs when the bot is waiting for the broker to provide at least one tick for a newly selected strike before it can calculate indicators. |
| **Closing Basis** | The bot uses `Finalized_Candle.Close` for all technical comparisons to prevent "noise" or "whipsaws" from live LTP fluctuations. |
