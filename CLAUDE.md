# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What This Is

**AlgoSoft** — a multi-broker, multi-client options-selling (short-strangle) trading bot for NSE/MCX instruments (NIFTY, BankNifty, etc.). It runs a V3 Sell strategy with live WebSocket feeds, paper trading, a FastAPI web dashboard, and support for 11 broker integrations.

**Git remote:** `https://github.com/ssrajpal2001/Option_Selling_May_2026.git`
> Always run `git pull` before starting work. Local can fall behind remote.

---

## Running the Bot

```bash
# Install dependencies (Python 3.12+, using uv)
uv pip install -r bot/requirements.txt

# Run live trading
python main.py --config config/config_trader.ini

# Run backtest
python main.py --config config/config_trader.ini --backtest_enabled

# Run web server only (FastAPI on :5000)
cd bot && python -m uvicorn web.server:app --host 0.0.0.0 --port 5000 --workers 1

# PM2 production (Ubuntu EC2)
pm2 status
pm2 restart algosoft-bot
pm2 logs algosoft-bot --lines 100

# Health check
curl http://localhost:5000/health
curl http://localhost:5000/api/admin/bot-status
```

---

## Knowledge Graph — Component Relationships

```
┌─────────────────────────────────────────────────────────────────┐
│                        main.py (entry)                          │
│                      EngineManager                              │
│          ┌──────────────┴──────────────┐                        │
│   LiveOrchestrator              BacktestOrchestrator            │
└──────────┬──────────────────────────────────────────────────────┘
           │
    ┌──────┴───── DATA LAYER ────────────────────┐
    │  DataManager (option chain, contracts)     │
    │  DualFeedManager (Upstox + Dhan WS feeds)  │
    │  AtmManager (ATM strike + expiry tracking) │
    └──────┬─────────────────────────────────────┘
           │ ticks
    ┌──────┴───── PROCESSING ────────────────────┐
    │  TickProcessor → IndicatorManager          │
    │    VWAP (timezone-safe, no look-ahead)     │
    │    RSI, ROC                                │
    └──────┬─────────────────────────────────────┘
           │ signals
    ┌──────┴───── STRATEGY (V3) ─────────────────┐
    │  SellManagerV3                             │
    │  ├─ sell_v3/entry_logic.py (candidate sel) │
    │  ├─ sell_v3/exit_logic.py  (exit rules)    │
    │  └─ sell_v3/base.py        (shared config) │
    │  SignalEvaluator / ExitEvaluator           │
    │  LifecycleManager (session start/EOD)      │
    └──────┬─────────────────────────────────────┘
           │ trade events (EventBus pub/sub)
    ┌──────┴───── EXECUTION ─────────────────────┐
    │  BrokerManager (routes per user)           │
    │  PositionManager (CE/PE state)             │
    │  PortfolioManager (multi-pos P&L)          │
    │  Brokers: Zerodha, Upstox, Dhan, AngelOne  │
    │           Fyers, Groww, AliceBlue, Paper   │
    └──────┬─────────────────────────────────────┘
           │ status / state
    ┌──────┴───── PERSISTENCE ───────────────────┐
    │  StateManager (JSON state per instrument)  │
    │  SQLite (algosoft.db — client/user data)   │
    │  PostgreSQL (optional, multi-tenant)       │
    └──────┬─────────────────────────────────────┘
           │
    ┌──────┴───── WEB LAYER ─────────────────────┐
    │  FastAPI server (:5000)                    │
    │  admin_api.py   → /api/admin/*             │
    │  client_api.py  → /api/client/*            │
    │  broker_api.py  → /api/broker/*            │
    │  auth_api.py    → /api/auth/*              │
    │  Jinja2 templates (client_dashboard.html)  │
    └────────────────────────────────────────────┘
```

---

## V3 Strategy — Trading Day Workflow

```
09:15 AM ──► LifecycleManager.start()
               │
               ▼
         [Indicator Priming]  ← wait 2–5 min for enough history
               │
               ▼
         [Entry Pulse Loop]  ← fires at each max-entry-TF boundary (e.g. 1m, 5m)
               │
        entry_logic.check_entry()
               ├── ATM candidates selected (beginning concept: ATM + N ITM)
               ├── Re-entry pool scan (V-slope filter)
               ├── VWAP slope, RSI, ROC gates evaluated
               └── PASS → EventBus: EXECUTE_TRADE_REQUEST
                              │
                     BrokerManager.place_order()  ← Sell CE + PE simultaneously
                              │
                     PositionManager.track()
                              │
               ▼
         [Exit Monitor — every tick]
        exit_logic.check_exit()
               ├── Profit target (% of combined entry premium)
               ├── Stop-loss (LTP < min)
               ├── Ratio exit (max_LTP / min_LTP ≥ threshold)
               ├── Scalable TSL (locked profit per lot)
               └── VWAP slope spike (> threshold% above session low)
                              │
                     EventBus: EXIT_TRADE_REQUEST
                              │
                     BrokerManager.exit_order()
                              │
                     [Smart Rolling?] ──► loop back to Entry Pulse
               │
15:15 PM ──► LifecycleManager.eod_squareoff()  (NSE)
23:00 PM ──► LifecycleManager.eod_squareoff()  (MCX)
```

---

## Key Files

| Category | File | Purpose |
|----------|------|---------|
| **Entry** | `main.py` | Process entry, wires EngineManager |
| **Strategy** | `bot/hub/sell_manager_v3.py` | V3 state machine |
| | `bot/hub/sell_v3/entry_logic.py` | Entry candidate selection + gate evaluation |
| | `bot/hub/sell_v3/exit_logic.py` | Exit condition checks |
| | `bot/hub/lifecycle_manager.py` | Market open/close/EOD timing |
| **Data** | `bot/hub/data_manager.py` | Option chain + contract loading |
| | `bot/hub/dual_feed_manager.py` | Upstox + Dhan parallel WebSocket feeds |
| | `bot/hub/tick_processor.py` | Raw tick → indicator pipeline |
| **Indicators** | `bot/hub/indicator_manager.py` | VWAP/RSI/ROC with timezone-safe caching |
| | `bot/indicators/vwap.py` | VWAP (finalized snapshot — no look-ahead) |
| **Execution** | `bot/hub/broker_manager.py` | Routes orders to correct broker per user |
| | `bot/hub/position_manager.py` | Tracks open CE/PE state |
| | `bot/hub/event_bus.py` | Pub/sub trade event bus |
| **Brokers** | `bot/brokers/base_broker.py` | Abstract broker interface |
| | `bot/brokers/zerodha_client.py`, `upstox_client.py`, `dhan_client.py` | Broker SDKs |
| **Web** | `bot/web/admin_api.py` | Admin: data providers, client mgmt (113KB) |
| | `bot/web/client_api.py` | Client: positions, orders, logs, toggles (124KB) |
| | `bot/web/templates/client_dashboard.html` | Main UI (3,935 lines — planned refactor) |
| **Config** | `bot/utils/config_manager.py` | Parses `config/config_trader.ini` |
| | `bot/utils/json_config_manager.py` | Parses `strategy_logic.json` (per-instrument V3 rules) |

---

## Configuration Files

| File | Role |
|------|------|
| `config/config_trader.ini` | Brokers, timeframes, indicators, paper/live toggle |
| `strategy_logic.json` | V3 rules per instrument (entry TF, exit thresholds, VWAP slope, max trades/day) |
| `config/sell_v3_state_NIFTY.json` | Persisted V3 state (last pulse, candidates, trade count) — written at runtime |
| `bot/config/algosoft.db` | SQLite: client accounts, broker tokens, trade history |
| `ecosystem.config.js` | PM2 process manager (auto-restart, log rotation) |

---

## Known Invariants

- **VWAP must use a finalized intraday snapshot**, not live value at boundary + 5s. The look-ahead bias was fixed in commits `a73ac40`–`bf412e7`. Do not revert this pattern.
- **All timestamps must be IST (Asia/Kolkata)**. Mixing UTC and IST causes silent indicator misalignment.
- **Dhan client auto-detects SDK version** — `DhanContext` vs legacy import. Do not assume a single import path.
- **DualFeedManager** redundancy: if Upstox returns 401, Dhan feed continues trading. Never short-circuit one feed unconditionally.
- **Paper trading mode** executes identical code paths to live — only `BrokerManager` switches the order client.

---

## Pending Critical Tasks (from pending-tasks-reference.md)

| # | Task | Key Files |
|---|------|-----------|
| #3 | Redesign client dashboard (clean UI, two-toggle UX) | `client_dashboard.html`, `client_api.py` |
| #4 | Automate Dhan token renewal (30-day expiry) | `admin_api.py`, `dhan_client.py` |
| #5 | Wire V3 Sell live execution (trades not firing) | `sell_manager_v3.py`, `broker_manager.py` |
| #12 | UX polish: market status, capital gauge, mobile CSS | `client_dashboard.html` |

---

## Agent Coordination Rules

Named agents coordinate via `SendMessage`, not polling.

```
Lead ←→ architect ←→ coder ←→ tester ←→ reviewer
```

- Spawn ALL agents in ONE message with `run_in_background: true`
- Include who to message next in every agent prompt
- After spawning: tell user what's running, wait for results

**When to use swarm** (3+ files, new features, cross-module changes, API changes):
```bash
npx @claude-flow/cli@latest swarm init --topology hierarchical --max-agents 8 --strategy specialized
```

**Before any task:**
```bash
npx @claude-flow/cli@latest memory search --query "[task keywords]" --namespace patterns
```

**After success:**
```bash
npx @claude-flow/cli@latest memory store --namespace patterns --key "[name]" --value "[what worked]"
```

### Agent Routing

| Task | Agents |
|------|--------|
| Bug Fix | researcher, coder, tester |
| Feature | architect, coder, tester, reviewer |
| Security | security-architect, auditor |
| Performance | perf-engineer, coder |
