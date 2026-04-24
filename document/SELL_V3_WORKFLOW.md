# Sell Side V3 Strategy: Detailed Workflow

This document outlines the step-by-step chronological lifecycle of the **Sell Side V3** strategy, from market open/bot startup to the end-of-day square-off.

---

## **Phase 1: Initial Startup & Data Priming (0 - 10 Minutes)**
The strategy begins with a mandatory "Silent Priming" phase to ensure all technical indicators have high-fidelity historical anchors.

1.  **Subscription:** Upon startup (or Market Open time), the bot identifies the **ATM ± 10 strikes** and proactively subscribes to their live feeds.
2.  **Silent Priming:** The bot enters a **wait period** (minimum 2x V-Slope timeframe).
    *   It collects and stores the **Average Traded Price (ATP)** history for every strike in the pool.
    *   It **refuses all entry signals** during this time to prevent false entries based on incomplete data.
    *   **Dashboard Status:** `WAITING`.
3.  **Anchor Establishment:** During this wait, it builds the **T-1** and **T-2** anchors required for the **V-Slope (Deceleration)** calculation.

---

## **Phase 2: BEGINNING Phase (The First Trade Attempt)**
Once the priming period is over, the bot attempts its first trade of the session at the next timeframe boundary (e.g., 09:10:05 AM for MCX or 09:25:05 AM for NSE).

1.  **Strike Selection (Balanced Straddle):**
    *   The bot checks the **Index Spot Price** to determine the current ATM strike.
    *   **The "Both Legs ≥ 50" Rule:** It searches for a CE and PE pair where both have a premium of at least 50.
    *   **ITM Searching:** If the ATM premium is < 50, the bot searches up to 15 strikes **In-The-Money (ITM)** for each side independently until it finds a strike meeting the threshold.
        *   *For CE:* Searches lower strikes.
        *   *For PE:* Searches higher strikes.
    *   **Scenario Analysis & OTM Re-balancing:**
        *   *The Higher Side Anchor:* After moving into ITM to satisfy the ≥ 50 rule, the bot identifies which leg has the higher LTP. This becomes the **Anchor Side**.
        *   *The Strictly Lower Rule:* The bot then searches for the counterpart strike whose LTP is **≥ 50 BUT strictly less than** the Anchor Side.
        *   *Scenario 1 (OTM Balancing Shift):*
            - ATM CE is 45 (Too Low), ATM PE is 70 (Anchor candidate).
            - Bot moves CE to 1-strike ITM (e.g., 21950). New CE is **60**.
            - Since 60 < 70 (ATM PE), the bot makes the **CE (60) the new base**.
            - It now finds a PE strike whose premium is **≥ 50 BUT < 60**.
            - Result: The bot shifts the PE to **OTM** (e.g., 22100) to find a price like 58.
        *   *Scenario 2 (Standard Higher Balance):*
            - ATM CE is 45 (Too Low), ATM PE is 70.
            - Bot moves CE to 1-strike ITM (e.g., 21950). New CE is **80**.
            - Since 80 > 70 (ATM PE), the **CE (80) is the Anchor**.
            - Since the ATM PE (70) is already strictly lower than 80 and ≥ 50, it selects it immediately.
        *   *Scenario 3 (Low Premium):* Even 15 strikes ITM, premium is < 50. Bot will **not enter**.
2.  **The V-Slope Gate:**
    *   It calculates the **Combined VWAP Slope** for this specific balanced pair.
    *   **Rule:** `Current Slope (T-0 to T-1) <= Previous Slope (T-1 to T-2)`.
3.  **Decision Branch:**
    *   **PASS:** If momentum is decelerating, it executes the **Initial Entry** immediately.
    *   **FAIL (The "Immediate Jump"):** If the specific balanced pair is rejected because its momentum is still rising, the bot does not stop or wait for the next candle.
        - **Phase Switch:** It immediately switches its internal state from `BEGINNING` to `CONTINUE`.
        - **Pool Scan:** It "jumps" straight into scanning the entire **ATM ± 10 pool**.
        - **Finding an Alternative:** It searches for *any other* straddle or strangle combination in the pool that *is* currently decelerating (satisfies V-Slope) and meets the technical criteria (RSI/VWAP).
        - **Why?** This ensures that even if the primary ATM straddle is too volatile to sell, the bot can still capture safe premium on slightly skewed or ITM/OTM strikes that have already started to stabilize.

---

## **Phase 3: In-Trade Management (The Lifecycle)**
While a trade is active, the bot monitors multiple exit triggers simultaneously to protect capital and lock in decay.

1.  **Tick-by-Tick Monitoring:**
    *   **Target Profit (12%):** If combined premium decays by 12%, trigger a **Smart Roll**.
    *   **Ratio Exit (3:1):** If one leg becomes 3x the price of the other, trigger a **Smart Roll**.
    *   **LTP < 20:** If either leg's price drops below 20, trigger a **Smart Roll**.
    *   **Scalable TSL:** Locks in profit in Rupee terms (e.g., lock 200 pts at 1000 profit).
2.  **Momentum Monitoring:**
    *   **VWAP Slope Exit (1%):** If the Combined VWAP rises by **1.0%** from the session's minimum recorded value, it triggers a **Full Exit**.
3.  **Technical Monitoring (Scanner Mode):**
    *   At every 5-minute boundary, it checks if **Combined Price > Combined VWAP AND Combined RSI > 50**. If true, it triggers a **Full Exit**.

---

## **Phase 4: Smart Rolling (Continuous Decay)**
A "Smart Roll" is designed to stay in the market and continue capturing decay without a cooldown.

1.  **Strike Re-selection:** The bot finds new strikes where premium is again ≥ 50.
2.  **Virtual vs. Physical:**
    *   **Virtual Roll:** If the optimal strikes are the same as current, the bot simply resets the entry price reference in its memory (saving brokerage).
    *   **Physical Roll:** If the strikes have shifted, it exits the old ones and enters the new ones.
3.  **Zero Cooldown:** Smart Rolls happen instantly to maintain delta-neutral exposure.

---

## **Phase 5: Full Exit & Re-entry Scanning**
A "Full Exit" (triggered by VWAP Slope, TSL, or Technical Reversal) indicates a trend change or risk event.

1.  **Full Close:** Both legs are exited immediately on all configured brokers.
2.  **Cooldown:** A mandatory **60-second wait** is enforced to let the market stabilize.
3.  **CONTINUE Phase (Re-entry Scanning):**
    *   After the cooldown, the bot enters "Scanner Mode".
    *   It scans the entire **ATM ± 10 pool** at every 5-minute boundary.
    *   **Scanner Entry Rule:** Combined Price < Combined VWAP **AND** Combined RSI < 50.
    *   **V-Slope Pool Scan (Optional):** If enabled, it also ensures the candidate pair satisfies the `Current Slope <= Previous Slope` rule.

---

## **Phase 6: End of Day (EOD) Square-off**
1.  **Forced Exit:** At **15:20:00**, all active positions are closed regardless of profit/loss.
2.  **Bot Finalization:** The bot stops scanning for new entries.
3.  **Status:** `STRANGLE CLOSED`.
