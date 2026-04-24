# Sell Side V3: Technical Logic & Condition Flow

This document provides a line-by-line technical breakdown of how the **Sell Side V3 Strategy** evaluates the market. It explains every core `if` condition used to decide when to enter, exit, or shut down for the day.

---

## 1. Rule Evaluation Engine (`base.py`)
The "Brain" of the strategy is the `evaluate_rules` method. It takes a list of rules (like RSI < 50) and converts them into a final PASS or FAIL.

### **A. VWAP Condition**
```python
if indicator == 'vwap':
    # 1. Fetch Candle for selected Timeframe (e.g., 5m)
    ohlc_c = await self.orchestrator.indicator_manager.get_robust_ohlc(ce_key, tf, finalized_anchor)

    # 2. Extract CLOSE price of that candle
    c_price = float(ohlc_c.iloc[-1]['close']) + float(ohlc_p.iloc[-1]['close'])

    # 3. Compare Combined CLOSE against Combined VWAP
    passed = (c_price < c_vwap) if is_entry else (c_price > c_vwap)
```
- **Entry Logic:** If `Combined Close < Combined VWAP`, it passes. We want to sell when premiums are "fair" or "cheap" relative to volume.
- **Exit Logic:** If `Combined Close > Combined VWAP`, it triggers an exit. This indicates premiums are exploding/inflating beyond the volume average.
- **Strict Close:** As requested, the bot uses the **Sum of CE/PE Candle Closes** for the comparison, not the live LTP, ensuring data stability on finalized candles.

### **B. V-Slope (Momentum) Condition**
```python
if indicator == 'vwap_slope':
    if is_entry:
        # Strict Momentum: Is the downward speed increasing?
        passed = (prev_s is not None and curr_s < prev_s)
    else:
        # Reversal: Is the premium speed increasing/turning up?
        passed = (prev_s is not None and curr_s > prev_s)
```
- **Entry Logic:** We only enter if `Current Slope < Previous Slope`.
  - *Example:* If Slope goes from -0.05 to -0.08 (getting faster), we enter.
  - *Example:* If Slope goes from +0.10 to +0.02 (slowing down), we enter.
- **Exit Logic:** We exit if `Current Slope > Previous Slope`. This detects "Turning Points" where decay stops and momentum shifts up.

### **C. RSI Condition**
```python
if indicator == 'rsi':
    # STRICT: Stick to the limit without buffers
    passed = (rsi < threshold) if is_entry else (rsi > threshold)
```
- **Entry Logic:** Passes only if `RSI < Threshold` (e.g., 50.0). No buffer is allowed.
- **Exit Logic:** Triggers exit if `RSI > Threshold`.

### **D. ROC (Volatility) Condition**
```python
if indicator == 'roc':
    if is_entry:
        # Is the volatility between these two limits?
        passed = (lower <= roc <= upper)
```
- **Entry Logic:** Checks if the 5-minute Rate of Change is within a "Quiet Zone" (e.g., -10 to +10). If the price jumps 20 points in 5 mins, the bot rejects the trade.

---

## 2. Entry Logic Workflow (`entry_logic.py`)

### **A. Startup Timing Gate**
```python
is_priming = self._is_in_priming_wait(timestamp)
if is_priming:
    # Requires 2 closed candles of the configured Timeframe
    return # Wait until 09:25 AM for 5m TF
```
- The bot **cannot** calculate momentum (`curr < prev`) until it has at least 2 closed candles of history for its slowest indicator.
- **Example (5m TF):**
  - Bot Starts: 09:15 AM
  - 1st Candle Closed: 09:20 AM (provides `prev` anchor)
  - 2nd Candle Closed: 09:25 AM (provides `curr` anchor)
  - First Gating Check: **09:25 AM**.

### **B. One Entry Per Candle Rule**
```python
if self.manager.last_entry_bucket == current_bucket:
    return # Already traded this minute/candle
```
- Prevents the bot from firing multiple orders on the same signal if volatility causes a flick.

---

## 3. Exit Logic & Guardrails (`exit_logic.py`)

### **A. Global PnL Guardrail (Daily Stop)**
```python
# 1. First, the bot checks if the toggle is ENABLED in the UI
if self._v3_cfg('guardrail_pnl.enabled', False, bool):
    # 2. Check for Profit Target (Must be a positive number)
    if g_pnl_target > 0 and pts_pnl >= g_pnl_target:
        await self.manager._execute_full_exit(..., stop_for_day=True)

    # 3. Check for Stop Loss (Must be a negative number)
    if g_pnl_sl < 0 and pts_pnl <= g_pnl_sl:
        await self.manager._execute_full_exit(..., stop_for_day=True)
```
- **Logic:** This is the ultimate safety net.
- **Toggle Check:** If the toggle is **DISABLED**, the bot skips these checks entirely.
- **Stop for Day:** If either limit is hit, the bot sets `strangle_closed = True`. This effectively "kills" the bot until the next morning to protect your capital.

### **B. Technical Exit Rules (Reversals)**
```python
exit_rules = self._v3_cfg('exit_rules', [])
if exit_rules:
    is_exit_met = await self.evaluate_rules(exit_rules, ...)
    if is_exit_met:
        await self.manager._execute_full_exit(timestamp, "Technical Exit")
```
- This evaluates your custom Exit Gate (e.g., `Price > VWAP AND RSI > 50`).
- Unlike Guardrails, this **does not** stop the bot for the day. It just closes the current trade and waits for a fresh, safe entry signal.

---

## 4. Manager & State Control (`sell_manager_v3.py`)

### **A. Stop for Day Logic**
```python
async def _execute_full_exit(self, ..., stop_for_day=False):
    if stop_for_day:
        self.strangle_closed = True # Permanent lock until tomorrow
```
- When `strangle_closed` is `True`, the `on_tick` method immediately returns, effectively "killing" the strategy for the session to protect profits or limit losses.

### **B. Timeframe Boundary Enforcement**
```python
max_rule_tf = max(rule_tfs)
if timestamp.minute % max_rule_tf == 0:
    # Only check rules at the end of the candle to prevent "Repainting"
```
- This ensures that if you have a 15m rule, the bot doesn't exit at 09:01 just because of a temporary spike. It waits until the 15m candle is **finalized**.

### **C. V-Slope Pool Scan Logic**
```python
for s in strikes:
    # 1. Resolve Slopes for this specific strike
    _, _, slopes = await self._check_v_slope_gate_for_strikes(ce_key, pe_key, ...)

    # 2. Evaluate ALL Entry Rules for this strike
    gate_res = await self.evaluate_rules(rules_re, ...)

    if gate_res:
        # 3. If passed, find the one closest to user ROC target
        candidates.append({'roc_dist': abs(roc_val - roc_target), ...})
```
- The bot doesn't just look at the ATM strike.
- It scans a **Pool** (e.g., ±2 strikes around ATM).
- **Strike 1 IF:** It must pass ALL technical rules (VWAP, RSI, Slope).
- **Strike 2 IF:** Out of all that passed, it selects the one whose **ROC** is closest to your "Target ROC" (e.g., near 0.0 for stability).

---

## 6. Advanced Management Logic (`exit_logic.py`)

### **A. Scalable TSL (Trailing Stoploss)**
```python
if self._v3_cfg('tsl_scalable.enabled', False, bool):
    if profit_rs >= base_profit:
        locked_rs = base_lock + (max(0, (profit_rs - base_profit) // step_profit)) * step_lock
        if profit_rs < locked_rs:
            await self.manager._execute_full_exit(..., reason="Scalable TSL locked")
```
- **IF 1:** Is Scalable TSL enabled?
- **IF 2:** Is the current profit (in Rupees) greater than the `base_profit` (e.g., 1000)?
- **Calculation:** It calculates a `locked_rs` amount that "trails" the peak profit.
- **IF 3:** If the profit drops below the `locked_rs` floor, it exits to protect the gain.

### **B. Smart Rolling (Strike Adjustment)**
```python
if ce_new['strike'] == ce_old['strike'] and pe_new['strike'] == pe_old['strike']:
    # Virtual Roll: Update entry price only
    self.manager.active_trades['CE']['entry_price'] = ce_new['ltp']
else:
    # Physical Roll: Close old, open new
    await self.manager._execute_full_exit(..., cooldown=False)
    await self.manager._execute_straddle_entry(...)
```
- **Logic:** If a Smart Roll is triggered (by % profit or decay), the bot finds the best straddle candidates.
- **IF:** If the best candidates are the **same strikes** you already hold, it does a **Virtual Roll** (resets the entry price anchor).
- **ELSE:** It closes the current position and opens the new, more balanced strikes.

### **C. Ratio & Decay Exits**
```python
# 1. Ratio Exit (Delta Balance)
if current_ratio >= ratio_threshold:
    await self.manager._execute_full_exit(..., reason="Ratio Breach")

# 2. LTP Decay (Profit Extraction)
if ltp_ce < decay_thresh or ltp_pe < decay_thresh:
    await self.perform_smart_roll(..., reason="LTP Decay")
```
- **Ratio:** Compares `max(CE, PE) / min(CE, PE)`. If one side becomes too large (e.g., 3x the other), the trade is unbalanced and risky. The bot exits to re-center.
- **Decay:** If either side drops below a minimum value (e.g., 20.0), there is very little decay left to collect on that side. The bot rolls to a higher strike to keep collecting premium.

---

## 7. The Core Engine (The "Box Work")

The strategy operates within an asynchronous event loop that processes every single tick. Here is the high-level execution flow:

1.  **Tick Arrival:** `on_tick(ticks, timestamp)` is called.
2.  **Phase Check:** If `strangle_closed` is `True` (from a Guardrail hit), the bot exits immediately.
3.  **Priming Check:** If it's before 09:25 AM (for 5m TF), the bot logs "Wait" and exits.
4.  **Indicator Update:** The bot calculates the latest RSI, ROC, and VWAP for the current ATM strike.
5.  **Exit Scan:**
    *   Checks if a position is open.
    *   If yes, evaluates `check_exits()`.
    *   If any exit condition (Technical or Guardrail) is met, it fires `_execute_full_exit`.
6.  **Entry Scan:**
    *   If no position is open AND the bot isn't "closed for the day."
    *   Evaluates `check_entry()`.
    *   Checks timeframe boundaries (e.g., only scan every 5 minutes).
    *   Runs the **Rule Engine** against the candidate strikes.
    *   If all `if` statements pass, fires `_execute_straddle_entry`.
7.  **State Save:** Every minute, the bot saves its entire state (active trades, session PnL) to a JSON file so it can recover if the server restarts.
