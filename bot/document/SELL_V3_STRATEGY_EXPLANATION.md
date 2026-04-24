# Sell Side V3 Strategy Explanation

This document provides a detailed breakdown of the entry and exit logic for the **Sell Side V3** strategy, designed for short straddle/strangle premium decay with advanced trend protection.

---

## 1. Entry Logic

### Timing & Instrument Selection
*   **Start Time (Scanning):** The bot initiates pool scanning shortly after **Market Open** (respects `sell.start_time` in strategy configuration, defaults to 09:00:05 AM for MCX, 09:15:05 AM for NSE).
*   **Entry Gate (Wait):** The bot strictly waits for a period (e.g., 10 minutes for 5m TF) from market open OR bot startup to ensure high-fidelity V-Slope anchors (T-1, T-2) are primed with tick-by-tick data. (Bypassed in **Backtest Mode** for immediate simulation).
*   **ATM Reference:** The At-The-Money (ATM) strike is determined using the **Market Price** (Futures for MCX, Index for NSE).
*   **Instrument Pools:**
    *   **Balanced Pool:** Used for initial balanced straddle selection.
    *   **V-Slope Pool:** Used during the pool scan (when V-Slope or SCANNER filters are active). Controlled by `v_slope_pool_offset` (ATM ± 10, default).
*   **Subscription:** Proactively subscribes to ATM and +/- 10 strikes to ensure price availability for indicators.

### Strike Balancing & ITM Rule
To ensure the straddle starts with sufficient premium and remains balanced:
1.  **Strict Premium Threshold:** The bot searches deeper ITM if necessary to ensure that **BOTH** CE and PE legs have an LTP >= **50** (configurable via `ltp_target`).
2.  **Strictly Lower Balancing:** Once the primary leg is selected, it searches for the *other* side strike whose LTP is the nearest value **strictly less than** the first side's LTP to ensure neutral decay start.

---

## 2. V-Slope (Momentum Deceleration) - Everything You Need to Know

The **V-Slope** logic is the core momentum filter for the V3 strategy. Its goal is to prevent the bot from selling premium when the market is trending strongly and instead wait for momentum to **decelerate (exhaustion)**.

### **How the Calculation is Done**
The calculation is performed on the **Combined Premium Series** (CE + PE) to evaluate the total straddle momentum.

1.  **Anchors (Three Points):** The bot identifies three specific points in time based on finalized candles (e.g., if using a 5-minute timeframe):
    *   **Anchor 1 (T-0):** The end of the *most recently closed* candle (e.g., 09:25:00).
    *   **Anchor 2 (T-1):** The end of the *previous* candle (e.g., 09:20:00).
    *   **Anchor 3 (T-2):** The end of the candle *before that* (e.g., 09:15:00 for NSE, 09:00:00 for MCX).

2.  **Combined VWAP:** At each anchor point, the bot calculates the Combined Volume Weighted Average Price (VWAP) for the specific strikes:
    *   `Combined VWAP = VWAP_CE + VWAP_PE`.

3.  **Slope Math:** The "Slope" is the percentage change between these VWAP points:
    *   `Current Slope = (Combined VWAP at T-0 - Combined VWAP at T-1) / Combined VWAP at T-1`
    *   `Previous Slope = (Combined VWAP at T-1 - Combined VWAP at T-2) / Combined VWAP at T-2`

### **The Deceleration Rule**
The trade only proceeds if the following logic is true:
*   **`Current Slope <= Previous Slope`**
*   **Why?** This means even if the premium is rising, it is rising **slower** than before (deceleration). If the premium is falling, it means it is falling **faster** or at the same rate (accelerating decay). Both are ideal for selling.

### **How the Trade is Taken (V-Slope Application)**
*   **In BEGINNING Phase (Initial Entry):**
    *   The bot identifies the best Balanced Straddle.
    *   It waits for the 5-minute boundary (e.g., 09:25:00).
    *   Immediately upon candle close, it calculates the slopes for that pair.
    *   If the rule passes, it enters. If it fails, it **jumps** to the CONTINUE phase (Scanning mode).

*   **In CONTINUE Phase (Re-entry Scanning):**
    *   If `v_slope_reentry_enabled` is ON, the bot scans every second across the **V-Slope Pool** (`v_slope_pool_offset`).
    *   For **each candidate pair** in the pool, it calculates the slopes using the last three finalized candles.
    *   Only pairs that satisfy the `Current Slope <= Previous Slope` rule are considered.
    *   From those, the best one matching other filters (RSI/VWAP) is selected for entry.

---

## 3. Workflow Phases

### Workflow Phase: BEGINNING (Sequential Entry)
This is the logic used for the first trade of the day.
1.  **Step 1: Initial Balanced Check:** At the first timeframe boundary (e.g., 09:20:00 or 09:25:00), the bot identifies the best **Balanced Straddle**.
2.  **V-Slope Gate:** As described above.
3.  **The "Jump":** If the balanced pair fails the gate, the bot immediately shifts to **CONTINUE** phase re-entry scanning.

### Workflow Phase: CONTINUE (Re-entry Modes)
*   **BALANCED Mode:** Re-enters a new balanced straddle immediately after an exit.
*   **SCANNER Mode (Technical):** Evaluated strictly on a **closing basis**.
    *   **Combined Close < Combined VWAP** (evaluated on the finalized candle).
    *   **Combined RSI < 50**.
    *   **V-Slope Re-entry (Optional):** If enabled, the V-Slope rule must also pass for the candidate pair.

---

## 4. Exit Logic & Smart Rolling

### Smart Roll Triggers (Immediate Re-balancing)
1.  **Profit Target (12%):** Combined premium decays by 12% from entry.
2.  **Ratio Exit (3:1):** One leg becomes 3x more expensive than the other.
3.  **LTP Decay (< 20):** Either leg's LTP falls below 20.
*   **Virtual Roll:** If new strikes are same as old, reference price resets without broker orders.

### Full Exit Triggers (With Cooldown)
4.  **VWAP Slope Exit (1%):** Combined VWAP rises 1% from its session low.
5.  **Scalable TSL:** Per-lot rupee-based trailing profit locking.
6.  **Technical Reversal (SCANNER only):** Combined Close > Combined VWAP AND Combined RSI > 50.
7.  **Bot Close Time:** Square off everything at the configured `close_time` (default 15:20:00).

---

## 5. Concrete Example Scenario

**Step 1: Initial Entry (09:25:00)**
*   Pair: 21950 CE / 22050 PE.
*   Rule: `Slope(09:20-09:25) <= Slope(09:15-09:20)`.
*   Passes -> Entry @ 100.5.

**Step 2: Smart Roll (10:30 AM)**
*   Premium decays to 88.0 (12% profit).
*   Bot rolls to fresh strikes near 50 LTP.

**Step 3: VWAP Slope Exit (11:15 AM)**
*   Combined VWAP hits a session low of 80.0, then rises to 80.8 (1%).
*   **ACTION:** Full exit and 60-second cooldown.

**Step 4: Technical Re-entry (11:16:01 AM)**
*   Scanning starts immediately. Evaluates 11:10-11:15 finalized candle.
*   If Close < VWAP and RSI < 50 (and V-Slope passes), it re-enters.
