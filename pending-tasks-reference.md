# AlgoSoft — All Pending Tasks Reference
*Generated: 28 Apr 2026 | Total: 36 pending tasks*

---

## #3 — Redesign client dashboard — clean trading UI + two-toggle UX + static IP
**Depends on:** #1

The current client dashboard exposes internal strategy details that are the platform's IP. Replaces it with a clean Zerodha/AlgoTest-style interface: broker connection, start/stop trading, live positions, order history. Enforces static-IP per client, two separate toggles (Connect / Allow Trading), and hides all indicator/strategy internals from clients.

**Done looks like:**
- Client sees: broker connection card, two-toggle UX (Connect Broker | Allow New Trades), live P&L, open positions table, today's trade history
- No strategy parameters, no indicator values, no raw logs visible to client
- Static IP shown on settings page with copy button
- All hardcoded strategy details removed from client-facing views

**Key files:** `bot/web/templates/client_dashboard.html`, `bot/web/client_api.py`

---

## #4 — Automate Dhan token renewal so the bot never stops due to an expired token
**Depends on:** #1

Dhan access tokens expire after 30 days. Currently a human must paste a new token manually. This silently breaks the live market feed without warning.

**Done looks like:**
- Warning email/admin notification sent at 5 days before Dhan token expiry
- Admin panel banner showing "Dhan token expires in X days" with a one-click renewal form
- Automatic expiry detection with Telegram alert when token is already expired
- Renewal flow persists new token to DB without requiring a bot restart

**Key files:** `bot/web/admin_api.py`, `bot/web/templates/admin.html`, `bot/brokers/dhan_client.py`

---

## #5 — Wire up live V3 Sell strategy execution so trades actually fire
**Depends on:** #1

The V3 Sell options-selling strategy has no execution path wired to real orders. The DualFeedManager receives live ticks but nothing triggers order placement.

**Done looks like:**
- V3 Sell strategy class reads live ticks from DualFeedManager event bus
- Strategy evaluates entry conditions (delta, premium, time filters)
- On signal: places order via the correct client broker, validates fill, persists trade state
- Exit logic fires on stop-loss / target / EOD square-off
- Paper mode executes the same logic without real orders

**Key files:** `bot/hub/sell_manager_v3.py`, `bot/hub/broker_manager.py`, `bot/hub/engine_manager.py`

---

## #12 — Client UX polish — market status, capital gauge, NSE calendar, mobile, referral
**Depends on:** #3

Quality-of-life features after the core dashboard redesign.

**Done looks like:**
- Live market status bar (pre-market / open / closed) at top of dashboard
- Capital-at-risk gauge sourced from the client's own broker funds API
- NSE holiday calendar awareness — bot shows "Market closed today" on holidays
- Mobile-responsive layout (works cleanly on a phone screen)
- Referral system: client gets a referral link; admin sees referral source on each signup

**Key files:** `bot/web/templates/client_dashboard.html`, `bot/web/client_api.py`

---

## #19 — Show clients a read-only history of admin actions on their account
**Depends on:** #7

The admin audit log tracks all important actions but clients can't see what's been done to their account.

**Done looks like:**
- New "Account Activity" section in client dashboard Settings pane
- Shows events: activated, plan changed, risk params updated, force-closed, bot started/stopped by admin
- Each entry: timestamp, action description (plain English), no internal IDs exposed
- Sourced from existing `audit_log` table filtered to that client's ID

**Key files:** `bot/web/client_api.py`, `bot/web/templates/client_dashboard.html`, `bot/web/db.py`

---

## #23 — Client-configurable risk & money management parameters
**Depends on:** #3

Clients need a self-service panel to configure all P&L-based risk controls for their trading instance. DB schema already has most fields.

**Done looks like:**
- "Risk & Money Management" tab in client dashboard settings
- Fields: max daily loss (₹), max drawdown (₹), per-trade stop-loss (%), position sizing (lots/qty)
- On breach: auto square-off fires and new entries are blocked for the session
- Admin can also override/cap these values from the admin panel

**Key files:** `bot/web/client_api.py`, `bot/web/templates/client_dashboard.html`, `bot/web/db.py`

---

## #25 — Send Telegram alerts when bot starts or stops for a client
**Depends on:** #10

Clients have no visibility when their bot instance starts, stops, or is killed by the admin.

**Done looks like:**
- Bot start → Telegram message: "🟢 Bot started for {instrument} in LIVE/PAPER mode"
- Bot stop → Telegram message: "🔴 Bot stopped ({reason})"
- Admin-forced stop also triggers alert (with "Admin action" as reason)
- Uses the existing per-client Telegram config already in the DB

**Key files:** `bot/hub/instance_manager.py`, `bot/web/admin_api.py`, `bot/utils/telegram_alerts.py`

---

## #28 — Show a positions badge on the tab when trades are active
**Depends on:** #17

The Positions tab has no indicator that trades are running while you're on another tab.

**Done looks like:**
- When CE and/or PE are active, the "Positions" nav tab shows a small pulsing badge with the position count
- Badge disappears when no positions are open
- Sourced from existing status file — no new API endpoint needed

**Key files:** `bot/web/templates/client_dashboard.html`

---

## #30 — Show a Telegram alert notification when any client's bot crashes mid-trade
**Depends on:** #21

The current watchdog only monitors the main AlgoSoft process. Individual client bot crashes go unnoticed.

**Done looks like:**
- When a client bot's status changes from 'running' to anything else unexpectedly (not a manual stop), an admin Telegram alert fires
- Alert includes: client ID, broker, last heartbeat timestamp, last known P&L
- Watchdog checks every 30 seconds against the running process list

**Key files:** `bot/hub/instance_manager.py`, `bot/web/server.py`, `bot/utils/telegram_alerts.py`

---

## #31 — Add a crash history log so admins can review past downtime on the dashboard
**Depends on:** #21

Admins have no way to review how many restarts happened, when crashes occurred, or how long the bot was down.

**Done looks like:**
- New `system_events` table (event_type, message, created_at) records each bot start, crash, and watchdog alert
- Admin overview shows a "System Events" card with last 20 events
- Health watchdog writes events to this table on each restart detection

**Key files:** `bot/web/db.py`, `bot/web/admin_api.py`, `bot/web/templates/admin.html`

---

## #32 — Add a public status page clients can check when the bot is down
**Depends on:** #21

When the bot is down, clients have no way to know if it's a platform issue or their own configuration.

**Done looks like:**
- `/status` renders a simple HTML page (no auth) showing: platform status (green/red), last updated timestamp, active sessions count
- Page auto-refreshes every 30 seconds
- Data sourced from existing `/health` endpoint (already public)

**Key files:** `bot/web/server.py`, new `bot/web/templates/status.html`

---

## #41 — Add a printable one-page quick-start card for new clients
**Depends on:** #40

New clients often need a condensed reference for their first day.

**Done looks like:**
- A short (one A4 page) print-friendly HTML page covering: morning token refresh, start bot, stop bot, square off all, kill-switch recovery
- Accessible at `/admin/onboarding/quickstart`
- Linked from admin client list for each client

**Key files:** `bot/web/admin_api.py`, new `bot/web/templates/onboarding_quickstart.html`

---

## #42 — Show a guided broker setup wizard for first-time clients
**Depends on:** #40

First-time clients often get confused navigating the Settings tab to connect their broker.

**Done looks like:**
- A modal wizard appears after first login if no broker is connected
- Steps: (1) select broker, (2) enter API key/secret, (3) complete login/token flow, (4) confirm connection and start bot
- Wizard state persisted so it doesn't re-appear after completion
- "Skip for now" option available

**Key files:** `bot/web/templates/client_dashboard.html`, `bot/web/client_api.py`

---

## #65 — Apply the admin's chosen default theme to first-time visitors automatically
**Depends on:** #61

The admin can set a default theme but it's not served to fresh browsers — new users always see Dark.

**Done looks like:**
- Public `GET /api/public/theme-default` endpoint returns the stored `default_theme` value
- Login page reads this before any JS runs and applies the theme class immediately
- No flicker on first load — theme is set before rendering

**Key files:** `bot/web/admin_api.py`, `bot/web/templates/login.html`, `bot/web/templates/base.html`

---

## #66 — Fix hardcoded colours in dynamically-built table rows across admin pages
**Depends on:** #61

Several admin pages build table rows in JavaScript with hardcoded hex colours (e.g. `background:#1e293b`) that don't respond to the theme system.

**Done looks like:**
- JS-generated HTML in `admin_clients.html`, `admin_audit_log.html`, and any other affected admin pages uses CSS variable references (`var(--bg-card)`, `var(--text-muted)`) instead of hex values
- All table rows look correct in both Dark and Light themes

**Key files:** `bot/web/templates/admin_clients.html`, `bot/web/templates/admin_audit_log.html`

---

## #73 — Color-code log lines so errors and warnings stand out immediately
**Depends on:** #54

The log viewer shows all lines in a single colour, making it hard to spot critical errors at a glance.

**Done looks like:**
- Log lines containing "ERROR" or "CRITICAL" are highlighted in red
- Lines containing "WARNING" or "WARN" are highlighted in orange/amber
- DEBUG lines are dimmed
- Applies to the inline log viewer on the client detail page and global logs page

**Key files:** `bot/web/templates/admin_client_detail.html`, `bot/web/templates/admin_global_logs.html`

---

## #78 — Show backtest results as a chart so clients can see P&L over time visually
**Depends on:** #77

The backtest results page shows a trade table and summary cards, but there is no visual P&L curve.

**Done looks like:**
- A cumulative P&L line chart is shown below the summary cards once results load
- X-axis: trade number or datetime; Y-axis: cumulative P&L in points
- Drawn with Chart.js (already loaded)
- Drawdown periods shown in a shaded red band

**Key files:** `bot/web/templates/client_dashboard.html` (backtest results section), `bot/web/client_api.py`

---

## #83 — Let admins allocate a brand-new Elastic IP from the dashboard without going to AWS console
**Depends on:** #84

Allocating a new Elastic IP currently requires the admin to log into the AWS console.

**Done looks like:**
- "Allocate New EIP" button in the admin IP management panel
- Platform calls AWS API to allocate a fresh EIP and adds it to the dropdown
- Admin can then assign it to a client in one click
- Requires AWS credentials to be configured (see #84)

**Key files:** `bot/web/admin_api.py`, `bot/web/templates/admin_client_detail.html`

---

## #84 — Prompt admins to set up AWS credentials from the settings page
**Depends on:** #58

When AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are missing, the Elastic IP dropdown silently errors.

**Done looks like:**
- When AWS credentials are missing, the IP card shows a clear inline callout explaining what's needed
- Callout links to a setup guide (or inline instructions for Replit secrets)
- Once credentials are configured, callout disappears automatically

**Key files:** `bot/web/admin_api.py`, `bot/web/templates/admin_client_detail.html`

---

## #85 — Automatically stop the bot at market close (3:30 PM) every day
**Depends on:** #80

The bot starts at 08:00 AM IST via systemd but never stops automatically — it runs idle after market close.

**Done looks like:**
- New `algosoft-bot-stop.timer` fires at 15:30 IST (10:00 UTC) Mon–Fri
- Timer calls a graceful stop: square-off all positions, then `systemctl stop algosoft-bot`
- Timer is enabled alongside the existing start timer
- NSE holiday awareness: timer skips stop on exchange holidays

**Key files:** `bot/scripts/` (new systemd timer units), `bot/setup_service.sh`

---

## #86 — One-command deploy script to update the bot on the EC2 server
**Depends on:** #80

Deploying currently requires multiple manual SSH steps with risk of human error.

**Done looks like:**
- `bot/scripts/deploy.sh` performs: git pull → install requirements → graceful stop → service start → health check (`curl /health`)
- If health check fails after 30 seconds, auto-rolls back to the previous commit and restarts
- Colored output showing each step's pass/fail
- Optional `--no-stop` flag for hot-reload testing

**Key files:** new `bot/scripts/deploy.sh`

---

## #123 — Let operators set RSI period and ROC length per rule in the admin panel
**Depends on:** #122

The V3 admin panel rule-builder has no input fields for `period` (RSI) and `length` (ROC) — operators must edit the JSON file directly.

**Done looks like:**
- Rule editor modal shows "RSI Period" and "ROC Length" number inputs when the relevant indicator is selected
- Values saved to the rule's config object in `strategy_logic.json`
- Existing rules with no explicit period/length show the global default values as placeholder

**Key files:** `bot/web/templates/admin_strategy.html` (or equivalent V3 config page), `bot/web/admin_api.py`

---

## #126 — Log a clear notice when historical data is fetched from further back than yesterday due to a holiday
**Depends on:** #120

After the holiday-gap fix, the fetcher correctly skips empty holiday days but only logs the file saved — not *why* it went back further.

**Done looks like:**
- When the historical fetcher goes back more than 1 day (i.e. skipping a holiday), a WARNING-level log fires: "Holiday gap detected: skipped {n} day(s), using data from {date}"
- Log appears in the startup sequence so it's easy to spot in the admin log viewer

**Key files:** `bot/data/` (historical data fetcher module)

---

## #142 — Add individual Start/Stop buttons per broker so clients can control each broker separately
**Depends on:** #141

The main Start All / Stop All button is implemented. Per-broker controls are still missing.

**Done looks like:**
- Each broker tab panel has its own Start / Stop button
- Clicking Start on a single broker calls `POST /bot/start` with `{ broker: "<name>" }`
- Clicking Stop sends the stop signal for just that broker
- Button state (running/stopped) reflects live status from the poll loop

**Key files:** `bot/web/templates/client_dashboard.html`, `bot/web/client_api.py`

---

## #143 — Make 'Allow New Trades' and 'Square Off' work for all running brokers, not just one
**Depends on:** #141

`/bot/toggle-trading` and `/bot/square-off-all` still only act on a single "active" instance.

**Done looks like:**
- `POST /bot/toggle-trading` writes the signal file for every running instance belonging to the user
- `POST /bot/square-off-all` writes the square-off signal for every running instance
- Response lists which instances were affected
- UI buttons remain single controls (affect all, not per-broker)

**Key files:** `bot/web/client_api.py`

---

## #155 — Show shared feed connection stats in the admin panel
**Depends on:** #152

Admins have no visibility into how many subprocesses are sharing the FeedServer, what the live tick rate is, or whether each subprocess connected successfully.

**Done looks like:**
- Admin data-providers page shows "Shared Feed Server: X clients connected"
- Tick-per-second rate for each active instrument is shown
- Subprocess connection status (connected / reconnecting / never connected) listed per client

**Key files:** `bot/web/admin_api.py`, `bot/web/templates/admin_data_providers.html` (or equivalent), `bot/hub/feed_server.py`

---

## #156 — Auto-resubscribe instruments after FeedServer restarts mid-session
**Depends on:** #152

When FeedClient reconnects after a FeedServer restart, the DualFeedManager has no record of which instruments to subscribe to — subscriptions from subprocesses were lost.

**Done looks like:**
- FeedServer maintains a subscription registry of all symbols currently requested by any connected client
- On reconnect, FeedClient re-sends its symbol list; FeedServer re-subscribes immediately
- No manual intervention needed after a mid-day bot redeploy

**Key files:** `bot/hub/feed_server.py`, `bot/hub/feed_client.py`

---

## #157 — Add FeedServer tick watchdog to alert when market data stops flowing
**Depends on:** #152

If both Upstox and Dhan WebSockets go silent, the FeedServer broadcasts nothing but also raises no alarm.

**Done looks like:**
- FeedServer tracks `_last_broadcast_epoch`
- Watchdog task fires every 60 seconds during market hours (09:15–15:30 IST)
- If no tick in the configured silence window (e.g. 120 seconds), sends a Telegram alert to admin
- Alert clears automatically when ticks resume

**Key files:** `bot/hub/feed_server.py`, `bot/utils/telegram_alerts.py`

---

## #158 — Fix IP-binding failures for AliceBlue, Fyers, and Upstox auth on EC2
**Depends on:** #153

`_scoped_socket_patch()` (used by AliceBlue, Fyers, Upstox auth path) still replaces `socket.create_connection` to bind the EIP, raising `[Errno 99]` on AWS EC2 NAT.

**Done looks like:**
- `_scoped_socket_patch()` applies the same NAT-aware logic as the adapter-based fix in #153
- On AWS EC2 with NAT, the socket patch is a no-op (IP binding skipped gracefully)
- AliceBlue, Fyers, and Upstox auth completes without Errno 99
- Order routing still uses the EIP via the adapter path

**Key files:** `bot/brokers/base_broker.py`, `bot/brokers/aliceblue_client.py`, `bot/brokers/fyers_client.py`, `bot/brokers/upstox_client.py`

---

## #159 — Add automated test coverage so EC2 broker startup is validated in CI
**Depends on:** #153

NAT detection logic from Task #153 has no regression tests — a future change to `base_broker.py` could silently re-introduce Errno 99.

**Done looks like:**
- pytest tests that mock the IMDS endpoint and exercise all broker startup paths (Zerodha, Dhan, AngelOne, AliceBlue) against NAT and non-NAT scenarios
- Tests run cleanly with `pytest bot/tests/test_broker_startup.py`
- CI step added to the project's test suite

**Key files:** new `bot/tests/test_broker_startup.py`, `bot/brokers/base_broker.py`

---

## #162 — Add auto-dismiss and periodic re-check to the IP conflict banner
**Depends on:** #151

The IP conflict banner is dismissed for the entire browser session via `sessionStorage`. Once an admin fixes the issue, the banner stays gone until they hard-refresh.

**Done looks like:**
- A periodic re-poll (every 5 minutes) re-shows the banner if a new conflict appears
- Banner auto-hides when the conflict is resolved (poll returns no conflicts)
- Dismiss button sets a short-lived flag (e.g. 10 minutes) rather than session-wide suppression

**Key files:** `bot/web/templates/admin.html` (overview page)

---

## #163 — Show Global Logs inline on the client detail page (per-broker tab)
**Depends on:** #151

The admin client detail Logs tab currently only offers a file download.

**Done looks like:**
- The Logs tab on the client detail page renders a colour-coded live log tail
- Uses existing `/api/admin/clients/{id}/logs/tail` endpoint
- Same viewer component as the Global Logs page (colour-coding, auto-scroll, pause button)
- Download button remains for full log access

**Key files:** `bot/web/templates/admin_client_detail.html`, `bot/web/admin_api.py`

---

## #164 — Keep the locked package list fresh so security fixes aren't missed
**Depends on:** #71

`bot/requirements.lock` will drift from reality as packages release new security-patched versions.

**Done looks like:**
- `bot/update_lock.sh` automates the full cycle: create clean venv → install → freeze → diff with current lock → write updated lock file
- Script prints a summary of changed packages
- README comment in `requirements.lock` notes when it was last regenerated and how to update

**Key files:** new `bot/update_lock.sh`, `bot/requirements.lock`

---

## #165 — Verify the bot starts cleanly from the lock file on a fresh EC2 instance
**Depends on:** #71

The lock file has never been validated end-to-end on a clean Python 3.11 environment.

**Done looks like:**
- A smoke-test script or runbook: spin up a clean venv with Python 3.11, install only from `requirements.lock`, run the bot's import checks (`python -c "import main"`), confirm no errors
- Any incompatible version pinned in the lock is fixed
- Result documented in a short test report or CI output

**Key files:** `bot/requirements.lock`, new `bot/scripts/smoke_test_lock.sh`

---

## #167 — Show a warning badge when a client bot is running without broker login
**Depends on:** #166

Failed broker logins now produce a clear CRITICAL log but there is no visual indicator in the admin UI.

**Done looks like:**
- Admin client detail page shows an amber "Degraded — broker login failed" badge next to bot status when the bot is running but broker authentication failed
- Admin overview page shows a similar indicator in the client row
- Badge sources from the bot status JSON file (a new `auth_failed` key written by the broker client on failure)

**Key files:** `bot/web/templates/admin_client_detail.html`, `bot/web/templates/admin.html`, `bot/web/admin_api.py`, `bot/brokers/angelone_client.py`

---

## #168 — Apply the broker client-code username fix to AliceBlue and Fyers
**Depends on:** #166

The `broker_user_id` → `client_code` mapping fix was applied to AngelOne (#166). AliceBlue and Fyers also use a client-code style login and may have the same silent bug.

**Done looks like:**
- `handle_aliceblue_login` and `handle_fyers_login` are audited and confirmed to look up `broker_user_id` or the `client_code` alias correctly
- INI-config fallback in `aliceblue_client.py` and `fyers_client.py` is gated on `not self.db_config`, matching the AngelOne fix
- No regression in existing admin-mode (non-client-mode) AliceBlue / Fyers flows

**Key files:** `bot/hub/broker_manager.py`, `bot/utils/auth_manager_aliceblue.py`, `bot/utils/auth_manager_fyers.py`, `bot/brokers/aliceblue_client.py`, `bot/brokers/fyers_client.py`
