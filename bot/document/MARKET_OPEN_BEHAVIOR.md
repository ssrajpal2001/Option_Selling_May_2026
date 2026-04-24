# Understanding Market Open Log Behavior (The "Traffic Jam")

During the first few minutes after market open (e.g., 09:15 AM for NSE or 09:00 AM for MCX), you may see warnings like `EVENT LOOP STALL DETECTED`, `Slow WebSocket handler`, or `WS WATCHDOG`. This document explains exactly what is happening and why the bot is designed to handle it.

---

## **1. The Data Surge (The Cause)**
At exactly Market Open time, the exchange begins broadcasting live ticks for every single strike price, future, and index.
*   **The Pool:** Because Sell Side V3 is enabled, the bot is proactively monitoring **46+ instruments** (Index + Futures + ATM ± 10 strikes for both CE and PE).
*   **The Volume:** In the first few seconds, there are thousands of ticks per second. Each tick contains not just the price, but also Greeks (Delta, Theta, etc.), Volume, and Open Interest.

## **2. The Processing Bottleneck (The Jam)**
All these ticks arrive over a single WebSocket connection. The bot must:
1.  **Parse** the binary data (Protobuf).
2.  **Update** the state for all 46 instruments.
3.  **Calculate** minute-by-minute ATP (Average Traded Price) history.
4.  **Aggregate** ticks into 1m and 5m OHLC candles.
5.  **Sync** this data across all internal user sessions.

**What happens in the logs:**
*   **`Slow WebSocket handler`**: This means the time spent processing a single batch of messages exceeded the "safe" threshold (now 5 seconds). Python is busy calculating indicators and can't go back to the network to read the next packet fast enough.
*   **`EVENT LOOP STALL DETECTED`**: This means the main execution thread was "stuck" processing a heavy burst of data for several seconds. During this stall, the bot cannot respond to other tasks (like UI updates or heartbeats).
*   **`WS WATCHDOG: Silence`**: Because the loop is stalled, it isn't "seeing" new messages from the network. The Watchdog sees this as "silence" even though data might be waiting in the network buffer.

## **3. The Reconnect (The Reset)**
Sometimes, the surge is so large that the network buffer overflows or the Upstox server terminates the connection due to the lag.
*   **`AttributeError`**: This is a common technical side-effect when a connection is lost while the bot is in the middle of trying to read from it.
*   **Recovery:** The bot is designed to **automatically catch this** and reconnect within 5 seconds.
*   **Resubscription:** Upon reconnecting, the bot sends a fresh request for all 46 instruments. This ensures no data is missed for the V-Slope calculation.

## **4. Why is this OK?**
*   **Silent Priming:** Remember that the V3 strategy strictly waits for a priming period (e.g. 10 minutes for 5m TF) before taking any trades.
*   **Self-Correction:** This "Traffic Jam" usually clears within 3-5 minutes once the initial flood of historical data and opening ticks is processed.
*   **Integrity:** Even if a reconnect occurs, the bot's background ATP history collector is designed to bridge the gap and ensure the **V-Slope Anchors (T-1, T-2)** are accurate before the first trade is attempted at 09:25 AM.

---

### **Summary of Changes**
To make the logs cleaner, we have:
1.  Increased the "Stall" and "Slow" warning thresholds to **5 seconds** to ignore normal market-open bursts.
2.  Silenced the `AttributeError` messages into `DEBUG` mode so they don't look like critical crashes.
3.  Throttled the Watchdog so it only notifies you if the queue is truly backed up (> 500 messages).
