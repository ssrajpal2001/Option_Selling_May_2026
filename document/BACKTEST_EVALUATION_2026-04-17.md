# Backtest Evaluation Report: NIFTY (2026-04-17)

## 📊 Performance Summary
| Metric | Value |
| :--- | :--- |
| **Total Net P&L** | **+₹1046.50** |
| **Total Trades** | 2 Straddle/Strangle Pairs (4 Legs) |
| **Win Rate** | 50.00% |
| **Start Time** | 09:20:00 |
| **End Time** | 15:20:00 |
| **Industrial Status** | Rust Core [ACTIVE], Zero-Stall I/O [ACTIVE] |

---

## 🔍 Execution Deep-Dive

### 1. Startup & Priming (09:15 - 09:20)
- **Status:** The bot correctly stayed `IDLE` until the **Start Time (09:20)**.
- **Priming:** It performed a 5-minute priming wait for the `ADVANCED(5m)` indicator. This ensured that RSI and ROC calculations were based on finalized market data from the opening bell.

### 2. Trade 1: ATM Straddle (09:20 - 10:08)
- **Entry:** 24200 CE (172.50) & 24200 PE (179.35).
- **Selection Concept:** `RE-ENTRY CONCEPT (Pool Scan)` with `Balanced Premium`.
- **Management:** The bot maintained the trade through minor fluctuations.
- **Exit:** Triggered at **10:08:00** via **Dynamic Technical Exit**.
- **Reason:** The condition `LTP>VWAP(1m) AND RSI>VALUE(2m) AND ROC>VALUE(2m) AND SLOPE>VALUE(1m)` was fully met.
- **Outcome:** Small loss of **-₹172.25**. This exit was effective as it prevented holding through a potential whipsaw.

### 3. Trade 2: Adjusted Strangle (10:08 - 15:20)
- **Entry:** 24250 CE (182.50) & 24300 PE (187.85).
- **Management (Scalable TSL):**
    - **12:09:29**: Profit crossed ₹1,000 threshold. Bot activated **Base Lock of ₹250**.
    - **13:28:00**: Profit reached ₹1,500. Bot increased lock to **₹750** (Base + 2 Steps).
- **Exit:** Closed at **15:20:00** via **EOD Square-off**.
- **Outcome:** Profit of **+₹1218.75**.

---

## ✅ Industrial Hardening Verification
- **Rust Math Core:** Logic pulses (RSI, VWAP, ROC) were evaluated in **<0.5ms**. Even when the Event Loop detected system load stalls (due to high-fidelity 1s backtest data), the trading decisions themselves remained atomic and synchronized.
- **Zero-Stall I/O:** The session logs and dashboard status files were written in the background, ensuring no blocking of the main execution loop.
- **Timing Discipline:** The bot respected the `Start Time`, `Entry End Time`, and `Square-off Time` with 100% precision.

---

## 💡 Suggested Improvements

### 1. Strategy Logic: "LTP vs CLOSE" for Exits
- **Observation:** The Dynamic Exit currently uses `LTP > VWAP(1m)`.
- **Suggestion:** For industrial stability, consider changing `LTP` to `CLOSE` in your rule builder. `LTP` reacts to every single tick, which can cause "fake-out" exits on spikes. `CLOSE` ensures the candle has actually finished above the VWAP before exiting.

### 2. Indicator Timeframe Alignment
- **Observation:** You are mixing `2m` values (RSI/ROC) with `1m` values (LTP/Slope).
- **Suggestion:** Standardize the technical exit rules to a single timeframe (e.g., all 1m or all 2m) to ensure momentum and price action are perfectly synchronized.

### 3. Enable Single Trade Target
- **Observation:** Your `Single Trade Target` and `SL` were set to `0.0`.
- **Suggestion:** Set a conservative `Single Trade Target` (e.g., 40-50 points). This allows the bot to capture rapid premium decay profits automatically without waiting for a technical pulse at the minute boundary.

### 4. Hybrid Workflow for First Entry
- **Observation:** You used `Pool Scan` for the very first entry.
- **Suggestion:** Switch `entry_workflow_mode` to `Hybrid`. This uses the `Beginning Concept` (strictly ATM) for the first trade of the day and switches to `Pool Scan` (Balanced) for retries. This often yields a better "starting delta" for the morning session.
