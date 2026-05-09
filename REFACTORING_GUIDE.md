# AlgoSoft — Refactoring Migration Plan

A concrete, ordered plan to make the bot more reliable, robust, modular,
and mobile-friendly without rewriting it. Every step is reversible and
ships value on its own — no big-bang releases.

**Prerequisites:** all work happens on a feature branch. Don't merge to
your main trading branch until each step has been smoke-tested in paper
mode for at least one full trading day.

---

## Step 0 — Baseline safety (Day 1, 2 hours)

Before changing anything, lock in current behavior so you can detect drift.

1. Drop `test_strategy_baseline.py` into `tests/`.
2. Run `pytest tests/test_strategy_baseline.py --update-baseline`.
   This records current VWAP/RSI numbers per backtest date into
   `tests/baseline.json`. Commit that file.
3. Add a CI job (GitHub Actions, however basic) that runs
   `pytest tests/` on every PR. Even just running the file matters —
   if a refactor changes `bot/hub/indicators/vwap.py`, the baseline test
   tells you immediately.
4. The end-to-end P&L tests are marked `xfail` deliberately; they need
   `BacktestOrchestrator` to expose a headless `run_for_date()` method.
   That's a small wrapper task — see Step 5 below.

**Why first:** every later step is safer if you can prove it didn't move
a number. Without this, you're refactoring blind.

---

## Step 1 — Mobile CSS (Day 1–2, half a day)

Drop-in fix that makes `client_dashboard.html` actually usable on a phone.

1. Copy `mobile-fixes.css` into `bot/web/static/`.
2. Edit `client_dashboard.html` (and any other dashboard templates):
   add **after** the existing inline `<style>` block in `<head>`:

   ```html
   <link rel="stylesheet" href="/static/mobile-fixes.css">
   ```

3. Test on a real phone, not just devtools. iPhone Safari and Android
   Chrome behave differently on `100dvh`, safe-area insets, and tap
   targets — both should feel right.
4. Tables: anywhere you have `<table class="trade-table">` etc., add
   `data-label="..."` to each `<td>`. The CSS already handles the
   transformation to a card-stack layout once labels exist:

   ```html
   <td data-label="Symbol">{{ trade.symbol }}</td>
   <td data-label="Qty">{{ trade.qty }}</td>
   ```

5. Anything that genuinely should stay tabular (e.g. a wide options
   chain) gets `class="trade-table keep-table"` and a wrapper
   `<div class="table-scroll">` — that gives horizontal scroll
   instead of card-stack.

**Effort:** ~4 hours for the CSS link + table label pass on the main
dashboard. Another ~4 hours for the same on `strategy.html` and
`brokers.html`.

**Validation:** open dashboard at 375px wide (iPhone SE), 414px (Pro Max),
768px (iPad portrait). All three should be usable without horizontal
scroll except where you explicitly want it (broker tabs, options chain).

---

## Step 2 — Split the dashboard template (Day 3–5, ~2 days)

The 3,935-line `client_dashboard.html` is the single biggest UX risk:
one bug breaks everything, and progressive enhancement is impossible
when everything shares scope.

1. Create `bot/web/templates/partials/`.
2. Use `_summary_header.html` as the pattern for extraction. Each
   partial gets:
   - A leading Jinja comment listing required context.
   - A scoped `<style>` block (everything class-prefixed by the
     partial's name to avoid global collisions).
   - A scoped `<script>` block that listens to `document` for
     `algosoft:*` custom events. **No cross-partial DOM access.**
3. Extract these in this order (lowest risk first):
   1. `_summary_header.html` — done as the example.
   2. `_market_bar.html` — already self-contained.
   3. `_log_panel.html` — read-only, no risk.
   4. `_position_table.html` — needs the data-label work from Step 1.
   5. `_strategy_panel.html` — has form state; trickiest.
   6. `_broker_tabs.html` — last, because it touches all the others.
4. For each extraction: render the page in paper mode, screenshot,
   extract, render again, diff screenshots. If they don't match,
   stop and fix.

**Why this matters:** once split, mobile-specific layouts per partial
become possible (e.g. `_strategy_panel_mobile.html` with progressive
disclosure), and you can A/B test individual sections.

---

## Step 3 — Typed events scaffolding (Week 2, 1 day)

Get the new event bus running alongside the old one. Zero behavior
change yet.

1. Drop `events.py` into `bot/hub/`.
2. Drop `event_bus_v2.py` into `bot/hub/`. Keep the existing
   `event_bus.py` untouched.
3. Add a `pip install pydantic` to `requirements.txt` if not present
   (your `pyproject.toml` already lists Pydantic, so likely fine).
4. In `main.py`, wire the new bus in parallel:

   ```python
   from hub.event_bus import event_bus           # legacy
   from hub.event_bus_v2 import bus              # new (typed)

   # Bridge: legacy publish → also dispatch typed handlers
   # (event_bus_v2 already handles this via LEGACY_EVENT_MAP)
   ```

5. Expose `bus.get_metrics()` and `bus.get_dlq()` on a debug API route
   — `/api/debug/event_bus`. Useful immediately even before any
   migration.

**Validation:** unit test that publishing legacy
`'EXECUTE_TRADE_REQUEST'` with kwargs successfully constructs a
`TradeExecuteRequest` typed event. The bridge code in
`event_bus_v2.publish_legacy()` already does this; just verify.

---

## Step 4 — Migrate one event family (Week 2–3, 2–3 days)

Pick the simplest event family and convert end-to-end as a pilot.
Recommendation: **trade execution** (`EXECUTE_TRADE_REQUEST` /
`EXIT_TRADE_REQUEST`) — only ~3 producers and 1 consumer.

1. Find every `event_bus.publish('EXECUTE_TRADE_REQUEST', ...)`.
   Replace with:

   ```python
   await bus.publish(TradeExecuteRequest(
       instrument_name=...,
       direction=Direction.PUT,
       action=TradeAction.SELL,
       strike=...,
       ...
   ))
   ```

2. Find every `event_bus.subscribe('EXECUTE_TRADE_REQUEST', handler)`.
   Replace with:

   ```python
   @bus.on(TradeExecuteRequest)
   async def handle_execute(event: TradeExecuteRequest):
       await broker_manager.execute(...)
   ```

3. Run the baseline tests. They should still pass (you didn't change
   strategy logic).
4. Run a paper-mode trading day. The bot's behavior should be identical;
   the only difference is logs now show typed event names and IDE
   autocomplete works in your editor.
5. Repeat for `ExitRequest`.

**Validation:** at the end of a paper day, `bus.get_metrics()` should
show non-zero counts for `TradeExecuteRequest` and `ExitRequest`, and
`bus.get_dlq()` should be empty.

---

## Step 5 — Decompose the giant managers (Week 3–6, ongoing)

The 1,337-line `sell_manager.py`, 891-line `sell_manager_v3.py`, and
927-line `signal_monitor.py` are the highest risk surface. Use the
typed events from Step 4 to break them up.

**Strategy: extract by VERB, not by NOUN.** Look for the things a
manager *does*, not the data it owns:

- `signal_monitor.py` does: ingest ticks, recompute features, evaluate
  patterns, emit signals, throttle/dedupe. Five verbs → potentially
  five smaller modules, each subscribing to the events upstream of it
  and emitting events downstream.
- Each new module starts at < 200 lines and stays there.
- Inputs and outputs are `events.py` types only. No reaching into
  `orchestrator.atm_manager.something`.

**Ground rule:** before extracting a method, check if the baseline
tests cover it. If not, write a test first. If you can't write a test
because the method has 14 hidden dependencies, that's a refactor signal:
the method needs to take its inputs as arguments, not pull them from
`self.orchestrator.X.Y.Z`.

**Order:**
1. `signal_evaluator.py` (smallest, ~626 lines, mostly pure math).
2. `signal_monitor.py` (split into 3–4 pieces around the typed events).
3. `sell_manager_v3.py` (the active version).
4. `sell_manager.py` (legacy — decide whether to delete or extract).

---

## Step 6 — Reliability & robustness (Week 4–6, parallel with Step 5)

These are the operational fixes that bite at the worst possible time
in real-money trading.

### 6a. Idempotency on order placement (1 day)

In `trade_executor.py`:
- Generate an idempotency key per signal (UUID, deterministic from
  `instrument + strike + signal_time`).
- Pass to broker layer; brokers that support idempotency keys (Zerodha,
  Upstox via order tag) use them.
- For brokers that don't, maintain a local "in-flight orders" set keyed
  by idempotency key — refuse to re-submit within a window if already
  pending.

### 6b. Cold-start recovery (2 days)

When the bot starts:
- Query each broker for currently-open positions.
- Reconcile against the last persisted state (`config/sell_v3_state_NIFTY.json`
  and Redis if enabled).
- If the broker shows positions the bot doesn't know about: log
  CRITICAL, surface in admin UI, do NOT trade further until human
  acknowledges.
- This single check would have prevented several classes of bugs you
  can see in the `attached_assets/` log files.

### 6c. Circuit breaker on broker layer (1 day)

In `broker_manager.py`:
- Track consecutive broker errors per broker.
- After N errors in M seconds, mark broker as "tripped" and stop
  routing trades to it.
- Half-open state: send one probe request after a cooldown.
- Surface trip state in `/api/status`.

### 6d. Audit `except:` blocks (1 day)

Grep `bot/hub/` and `bot/brokers/` for bare `except:` and `except Exception:`.
For each one, decide:
- Is this swallowing a bug? Add `logger.exception(...)` minimum.
- Is this a known recoverable error? Catch the specific exception type.
- Is the function silently returning `None` on failure? That's almost
  always wrong in trading code — make it raise or return an explicit
  error sentinel.

### 6e. WebSocket reconnect hardening (2 days)

`reconnect_manager.py` is 194 lines — probably handles the happy path
but not edge cases:
- What happens if the reconnect succeeds but the broker session
  expired (silent auth loss)?
- What if you reconnect *during* an order placement?
- Does the tick stream resume from the last seen tick or start fresh?

Write tests that simulate disconnect at each phase.

---

## Step 7 — Repo hygiene (Half a day, do whenever)

These don't change behavior but reduce friction.

1. **Move `attached_assets/` out of git.** It's ~28 MB of pasted log
   files. Use `git filter-repo` to scrub from history (warn collaborators
   first):
   ```bash
   git filter-repo --path attached_assets/ --invert-paths
   ```
   Add `attached_assets/` to `.gitignore`.

2. **Audit those logs for credentials before publishing the cleaned
   repo.** A quick `grep -rE "api[_-]?key|secret|token|password" attached_assets/`
   often surfaces things you didn't realize were committed.

3. **Build Tailwind locally instead of CDN.** `cdn.tailwindcss.com` is
   a JIT compiler that runs in the user's browser — slow on phones,
   blocks first paint. Replace with a built `tailwind.css` (one-time
   `npx tailwindcss -i ./input.css -o ./bot/web/static/tailwind.css --minify`).
   Visible mobile speed improvement.

4. **Single test file → tests/ directory.** Currently `tests/` has
   one file. Group: `tests/strategy/`, `tests/brokers/`, `tests/web/`.

---

## Effort summary

| Step | Effort | Risk | Visible benefit |
|------|--------|------|-----------------|
| 0 — Baseline tests | 2 hours | Zero | Refactor safety net |
| 1 — Mobile CSS | 1 day | Low | **Immediate UX win** |
| 2 — Split dashboard | 2 days | Medium | Maintainability + per-section mobile |
| 3 — Typed events scaffold | 1 day | Zero | Foundation only |
| 4 — Migrate trade events | 2 days | Low | Foundation proven |
| 5 — Decompose managers | 3–4 weeks | Medium | Real modularity |
| 6 — Reliability fixes | 1 week | Medium | Production hardening |
| 7 — Repo hygiene | Half day | Low | Faster clones, security |

**Total realistic timeline for a single dev:** 6–8 weeks part-time, or
3–4 weeks full-time, to land all of it.

**Minimum viable subset (1 week):** Steps 0, 1, 6a, 6b, 6d. That gets
you mobile UX, refactor safety, and the three reliability fixes that
matter most for real money.

---

## What NOT to do

- **Don't do a swarm/microservice rewrite.** I covered this earlier;
  it adds latency, debugging difficulty, and distributed-systems failure
  modes for no proportional gain.
- **Don't introduce LLM agents in the trading hot path.** Latency and
  hallucination risk make this dangerous for real money. LLMs are great
  for offline log analysis, post-trade journaling, and admin tooling.
  Keep them off the tick path.
- **Don't refactor without the baseline tests in place.** Trading bugs
  often surface only on rare conditions (gap-up open, expiry day,
  illiquid strike). Manual testing won't catch them; deterministic
  baselines against recorded ticks will.
- **Don't merge any of this directly to the main trading branch.**
  Run each step on a feature branch in paper mode for at least one
  trading day. The cost of a wrong refactor is real money.

---

## Files in this delivery

| File | Where it goes | Purpose |
|------|---------------|---------|
| `mobile-fixes.css` | `bot/web/static/` | Drop-in mobile responsive CSS |
| `events.py` | `bot/hub/` | Pydantic typed event definitions |
| `event_bus_v2.py` | `bot/hub/` | Improved event bus, back-compat |
| `test_strategy_baseline.py` | `tests/` | Golden-master test scaffold |
| `_summary_header.html` | `bot/web/templates/partials/` | Example partial extraction |
| `REFACTORING_GUIDE.md` | repo root | This document |

Each file is independent. You can adopt them in any order, though the
order in this guide minimizes risk.
