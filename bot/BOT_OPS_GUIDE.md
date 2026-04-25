# AlgoSoft — Complete Operations Guide

> **Who this is for:** This guide is for the **platform administrator** and **end-client traders**
> using the AlgoSoft algorithmic trading platform. No programming knowledge is required.

---

## Table of Contents

1. [Platform Overview](#1-platform-overview)
2. [Starting the Bot Server (Admin — EC2)](#2-starting-the-bot-server-admin--ec2)
3. [Admin Workflow — Step by Step](#3-admin-workflow--step-by-step)
4. [Client Workflow — Step by Step](#4-client-workflow--step-by-step)
5. [Broker Connection Guide](#5-broker-connection-guide)
6. [How the Bot Works Automatically](#6-how-the-bot-works-automatically)
7. [Daily Routine Checklist](#7-daily-routine-checklist)
8. [Troubleshooting Common Errors](#8-troubleshooting-common-errors)

---

## 1. Platform Overview

AlgoSoft is a cloud-hosted algorithmic options trading platform. Here is how it is structured:

| Component | Role |
|-----------|------|
| **Admin** | Deploys the server, creates client accounts, sets subscription plans, monitors all bots |
| **Client** | Logs in, connects their broker, starts/stops their personal trading bot |
| **Global Data Feed** | Upstox + Dhan supply live NSE market prices to all bots (admin configures this once) |
| **Execution Broker** | The client's own broker (Zerodha, Dhan, Upstox, etc.) is used only to place orders |
| **Strategy** | V3 Option Selling strategy — the bot automatically finds signals and places/exits trades |

**Key rules:**
- Market data (prices) comes from the global feed — client broker quality does not affect data accuracy.
- Each client's bot runs independently. One client's trades do not affect another's.
- Paper Trading mode simulates trades without placing real orders — safe for testing.
- Live Trading mode places real orders on the client's broker account.

---

## 2. Starting the Bot Server (Admin — EC2)

The bot runs on your AWS EC2 server and is accessed at:
```
http://<your-EC2-IP>:5000
```
*(e.g. http://13.234.185.209:5000)*

### Method A — Quick Restart (Manual, same as before)

SSH into the EC2 server and run:

```bash
cd /home/ubuntu/Option_Selling_May_2026/bot
bash scripts/restart_server.sh
```

This script automatically:
1. Stops any existing bot process
2. Clears port 5000
3. Starts a fresh server
4. Confirms it is running

Logs are saved to `bot/server.log`.

---

### Method B — PM2 (Recommended for Production)

PM2 is a process manager that keeps the bot running 24/7 and restarts it automatically if it crashes.

**First-time setup (run once):**

```bash
cd /home/ubuntu/Option_Selling_May_2026
bash scripts/start-production.sh
```

**After setup, useful PM2 commands:**

| Command | What it does |
|---------|-------------|
| `pm2 status` | Shows if the bot is running |
| `pm2 restart algosoft-bot` | Restarts the bot |
| `pm2 stop algosoft-bot` | Stops the bot |
| `pm2 logs algosoft-bot` | Shows live log output |
| `pm2 monit` | Live CPU and memory monitor |

**Health check (from EC2 terminal):**
```bash
curl http://localhost:5000/health
```
A working bot returns: `{"status": "ok", "uptime": "2h 15m 30s", ...}`

---

## 3. Admin Workflow — Step by Step

### 3.1 First Login

1. Open `http://<EC2-IP>:5000` in a browser
2. Log in with admin credentials:
   - Username: `admin`
   - Password: `Admin@123` *(change this immediately after first login)*
3. You will land on the **Admin Overview** dashboard

---

### 3.2 Step 1 — Configure Global Data Providers

> **Must be done before any client can trade.** These are the platform's own Upstox and Dhan
> accounts that supply live market price data.

1. Go to **Admin → Data Providers** (left sidebar)
2. Click **Configure** next to **Upstox**:
   - Enter the platform Upstox API Key, Secret, Mobile Number, PIN, and TOTP Secret
   - Click **Connect Now** to generate the token automatically in the background
   - If you have not set up automation credentials yet, click **Fallback: Manual Browser Auth** → log in → copy the full redirect URL from the browser → paste it in the **Paste Redirect URL** box → click **Exchange & Save**
   - See **Section 5.2** for detailed Upstox token steps
3. Click **Configure** next to **Dhan**:
   - Enter the Dhan Client ID and Access Token
   - Click **Save**
4. Both should show **Connected / LIVE** status

> **Every morning before 9:15 AM:** Upstox tokens expire daily. Click **Connect Now** on the Upstox provider card to auto-renew it (requires automation credentials saved). Dhan tokens last 30 days and auto-renew.

---

### 3.3 Step 2 — Configure Platform Settings (Telegram + Email)

Go to **Admin → Platform Settings**

**Telegram Setup (for trade alerts):**
1. Open Telegram and search for `@BotFather`
2. Send `/newbot` and follow the prompts to create a bot
3. Copy the token (format: `1234567890:ABCdef...`)
4. Paste it in the **Bot Token** field
5. Enable the **Telegram Alerts** toggle
6. Click **Save Telegram Settings**
7. Click **Send Test Message** to confirm it works

**SMTP Email Setup (for subscription alerts):**
1. Enter your SMTP server details (e.g. Gmail: `smtp.gmail.com`, port `587`)
2. Use a Gmail App Password (not your regular password) — generate at myaccount.google.com → Security → App Passwords
3. Click **Save SMTP Settings**
4. Click **Send Test Email** to confirm

---

### 3.4 Step 3 — Create Subscription Plans

Go to **Admin → Subscription Plans**

Plans control how many broker connections a client can have simultaneously.

1. Click **Add Plan**
2. Set:
   - **Plan Name** (e.g. "Basic", "Pro", "Enterprise")
   - **Max Broker Instances** — how many brokers the client can connect (1 = one broker)
   - **Price** (for your records)
3. Click **Save**

You can create multiple plans and assign them individually to clients.

---

### 3.5 Step 4 — Add and Activate Clients

Clients register themselves at `http://<EC2-IP>:5000/register`, or you can add them directly.

**After a client registers:**
1. Go to **Admin → Clients**
2. Find the client in the **Pending** tab
3. Click **Activate** — the client can now log in and use the platform
4. Click **View** to open the client's detail page where you can:
   - Assign a subscription plan (and set an expiry date)
   - Set the number of broker slots
   - View their trade history
   - Monitor their live bot
   - Set risk parameters (daily loss limit, max trades, etc.)
   - Lock/unlock their kill-switch manually

---

### 3.6 Admin Overview Dashboard

The **Admin → Overview** page shows real-time status:

| Widget | What it shows |
|--------|--------------|
| **Total / Active / Pending** | Client counts |
| **Running Bots** | How many client bots are live right now |
| **Failures (24h)** | Order failures in the last 24 hours |
| **Process Manager Status** | Green/red dot showing if the server is healthy |
| **Live Bot Instances** | Per-client live P&L, broker, heartbeat status |
| **Pending Broker Change Requests** | Clients who have requested to switch brokers |
| **System Health** | CPU usage, memory, data feed status |

The page refreshes automatically every 10 seconds.

---

### 3.7 Client Detail Page (Admin View)

Click any client's **View** button to open their detail page. Three tabs:

- **Info & Trades** — broker instances, trade history, force stop, activate/deactivate, plan assignment
- **Risk & Money** — set capital, daily loss limit, per-trade loss, max drawdown, kill-switch controls
- **Live Monitor** — real-time ATM strike, index level, session P&L, buy/sell side positions, V3 strategy signals

**Force stopping a client's bot:**
Click **Force Stop Bot** on the Info tab. This immediately stops their bot without waiting for trade exit. Use only in emergencies — open positions may remain.

**Locking a client's kill-switch:**
On the Risk & Money tab → Kill Switch section → click 🔒 **Lock**. This prevents the client from starting the bot until you click 🔓 **Unlock**.

---

### 3.8 Audit Log

Go to **Admin → Audit Log** to see a full history of all admin actions: who activated/deactivated clients, changed plans, forced stops, etc.

---

## 4. Client Workflow — Step by Step

### 4.1 First Login

1. Open `http://<EC2-IP>:5000` in a browser
2. Log in with the username and password provided by your admin
3. The **Dashboard** page opens

The dashboard has five tabs at the top:
- **Dashboard** — bot controls, live P&L
- **Positions** — open positions with per-leg square-off
- **Backtest** — test the strategy on past data
- **History** — all past trades
- **Settings** — broker connection, risk config, Telegram, referral

---

### 4.2 Step 1 — Connect Your Broker (Settings Tab)

Click **Settings** in the top navigation.

1. Under the **Broker** section, select your broker (e.g. Zerodha, Dhan, Upstox, etc.)
2. Enter your broker credentials — see **Section 5** below for per-broker instructions
3. Click **Save** or **Connect**
4. The token status pill next to your broker will turn **green** when successfully connected

> **Important:** The token shows **Fresh** (green) or **Stale** (red). A stale token means the bot cannot start — you need to reconnect for today.

---

### 4.3 Step 2 — Set Risk Parameters (Settings Tab)

Still in Settings, scroll to the **Risk Management** section:

| Setting | What it does |
|---------|-------------|
| **Allocated Capital (₹)** | Total capital assigned to the bot account |
| **Daily Loss Limit (₹)** | Bot auto-stops when daily loss hits this amount. Set 0 to disable |
| **Per-Trade Loss Limit (₹)** | Bot exits a trade early if this single-trade loss is hit |
| **Max Drawdown (%)** | Bot stops when loss is this % of capital. Set 0 to disable |
| **Max Trades Per Day** | Hard cap on total trades. Set 0 for unlimited |
| **Max Position Size (lots)** | Maximum lots per single order |

Click **Save Risk Parameters** when done.

---

### 4.4 Step 3 — Set Up Telegram Alerts (Optional but Recommended)

In Settings → Telegram section:

1. Open Telegram and search for `@userinfobot` — start it and it will tell you your **Chat ID**
2. Enter that Chat ID in the Telegram Chat ID field
3. Click **Save**

You will now receive Telegram messages whenever:
- Your bot starts or stops
- A trade is placed or exited
- Daily P&L summary at 3:30 PM
- Subscription is about to expire

---

### 4.5 Step 4 — Start the Bot

Return to the **Dashboard** tab.

1. The **Bot Control** card shows your connected broker and token status
2. Verify the token shows **Fresh** (green) — if red/stale, reconnect your broker first
3. Switch the **BOT toggle** (top-right navbar) to ON — or click the green button
4. The status badge changes from **STOPPED → RUNNING**

The bot is now live. It will:
- Watch the NSE market continuously
- Find option selling signals using the V3 strategy
- Place trades automatically during market hours (9:15 AM – 3:30 PM IST)
- Exit trades based on profit/loss targets
- Square off all positions before 3:30 PM

---

### 4.6 Monitoring Live Trades (Dashboard Tab)

The Dashboard shows real-time information while the bot runs:

**Top bar (Market Status):**
- NSE Market: OPEN (green) or CLOSED (red)
- Current IST time
- Your connected broker name
- Your subscription plan

**Stats cards:**
- **Session P&L (₹)** — today's profit or loss in rupees
- **Open Positions** — how many options legs are currently active
- **Trades Today** — total trades completed this session
- **Mode** — PAPER (simulation) or LIVE (real money)

**Bot Control card:**
- **BOT toggle** — turns the bot process on/off
- **TRADE toggle** — pauses new trade entries without stopping the bot (useful if you want to exit existing positions but take no new ones)
- **Square Off All** button — manually closes all open positions immediately

---

### 4.7 Viewing Open Positions (Positions Tab)

Click **Positions** in the top navigation.

The table shows each open option leg:
- **Instrument** — e.g. NIFTY 24000 CE
- **Type** — CE (Call) or PE (Put)
- **Qty** — number of lots
- **Avg Price** — your entry price
- **LTP** — current market price (live)
- **P&L ₹ / %** — current profit or loss on this leg
- **Entry Time** — when this position was opened

**Squaring off a single leg:**
Click the **Sq Off** button on any row to close just that one leg. A confirmation appears — click Confirm to proceed.

**Summary bar at bottom:**
- Total P&L across all open positions
- Total premium received

---

### 4.8 Stopping the Bot

Switch the **BOT toggle** to OFF (top-right navbar). The bot will:
1. Stop monitoring for new signals
2. **Not** automatically square off open positions — they remain until you manually close them or the next start

> To exit all trades AND stop: click **Square Off All** first, then turn the bot off.

---

### 4.9 Backtesting (Backtest Tab)

Test how the V3 strategy would have performed on a past date:

1. Click **Backtest** tab
2. Select **Instrument** (NIFTY or BANKNIFTY)
3. Select the **Date** (must be a market trading day)
4. Set **Quantity (lots)**
5. Click **Run Backtest**
6. Watch the console log — it simulates the entire day's trading
7. View the generated order book at the bottom (entry price, exit price, P&L per trade)

> Backtest uses historical data and does not place real orders. It is safe to run at any time.

---

### 4.10 Trade History (History Tab)

View all past completed trades:
- Date and time of entry/exit
- Strike price, CE/PE type
- Entry and exit prices
- P&L in points and rupees
- Exit reason (Target Hit, Stop Loss, Manual, Day End, etc.)

---

## 5. Broker Connection Guide

### 5.1 Zerodha (Kite)

Zerodha tokens expire every night. You must reconnect each morning before starting the bot.

**Where to get credentials:**
- Log in at [kite.trade](https://kite.trade) → My Account → Apps → Create App
- **API Key** and **API Secret** are shown on the app page

**Daily connection steps (Settings → Broker → Zerodha):**
1. Enter API Key and API Secret → click **Save**
2. Click **Login with Zerodha** → a Kite login page opens in a new tab
3. Log in with your Zerodha credentials + OTP
4. After login, Kite redirects to a URL — copy the value after `request_token=` in the URL
5. Paste that value in the **Request Token** field
6. Click **Generate Token** → token status turns green ✓

**One-Click Connect (if credentials saved):**
If you have saved your Zerodha **Password** and **TOTP secret** in Settings, the bot will automatically log in each morning when you click **Start Bot** — no manual token step needed.

---

### 5.2 Upstox

Upstox tokens expire daily. There are two ways to refresh the token depending on how your Upstox Developer App is configured.

**Where to get credentials:**
- Log in at [upstox.com/developer/apps](https://upstox.com/developer/apps) → Create App
- Note the **API Key** and **API Secret** shown on the app page

---

**Option A — Fully Automated (Recommended)**

Set the Redirect URI in the Upstox Developer Portal to exactly:
```
http://<EC2-IP>:5000/auth/upstox/callback
```
*(e.g. `http://13.234.185.209:5000/auth/upstox/callback`)*
Then the token is captured automatically after login — no copy-paste needed.

**Steps:**
1. Enter **API Key** and **API Secret** → click **Save Credentials**
2. Enter **Mobile Number**, **PIN**, and **TOTP Secret** in the Automation Credentials section
3. Click **Connect Now** — token is generated in the background automatically ✓

---

**Option B — Manual (If Redirect URI is google.com)**

If your Upstox Developer App has `https://google.com` as the Redirect URI, the bot cannot
capture the token automatically. Use this manual flow instead:

**Steps:**
1. Enter **API Key** and **API Secret** → click **Save Credentials**
2. Click **Fallback: Manual Browser Auth** → Upstox login page opens in a new tab
3. Log in with your Upstox account + OTP
4. After login, your browser is redirected to a google.com URL — do NOT close it
5. Copy the **full URL** from the browser address bar (it starts with `https://google.com?code=...`)
6. Go back to the Admin → Data Providers → Configure Upstox modal
7. Paste the full URL into the **"Paste Redirect URL"** box at the bottom
8. Click **Exchange & Save** — the bot extracts the auth code and saves your token ✓

> **Tip:** To switch to the fully automated Option A, update the Redirect URI in your
> [Upstox Developer Portal](https://upstox.com/developer/apps) to
> `http://<EC2-IP>:5000/auth/upstox/callback`. This is the recommended long-term setup.

---

### 5.3 Dhan

Dhan tokens last 30 days and the platform auto-renews them. No daily manual step needed.

**Where to get credentials:**
- Log in at [dhanhq.co](https://dhanhq.co) → API → Generate Token

**Connection steps (Settings → Broker → Dhan):**
1. Enter **Client ID** and **Access Token** → click **Save**
2. Token status shows Fresh immediately ✓

> Dhan tokens are valid for 30 days. When your subscription expiry check runs (9:15 AM) and your Dhan token is close to expiring, admin will be notified. If you receive a Telegram alert about your Dhan token, re-enter a fresh access token from the Dhan developer portal.

---

### 5.4 Angel One (AngelBroking)

**Where to get credentials:**
- Log in at [angelbroking.com](https://smartapi.angelbroking.com) → API → Create App
- Note your **API Key**, **Client ID**, and set a **MPIN**

**Connection steps (Settings → Broker → Angel One):**
1. Enter **API Key**, **Client ID (Username)**, **Password (MPIN)**, and **TOTP Secret**
2. Click **Connect** → login happens automatically in the background ✓

---

### 5.5 Alice Blue

**Where to get credentials:**
- Contact Alice Blue support or log in at [aliceblueonline.com](https://aliceblueonline.com) → API section

**Connection steps (Settings → Broker → Alice Blue):**
1. Enter **API Key**, **Client ID**, **Password**, and **TOTP Secret**
2. Click **Connect** → login happens automatically ✓

Alice Blue sessions are refreshed automatically when stale.

---

### 5.6 Groww

**Where to get credentials:**
- Log in at [groww.in](https://groww.in) → Settings → API access

**Connection steps (Settings → Broker → Groww):**
1. Enter **API Key**, **Client ID**, **Password**, and **TOTP Secret**
2. Click **Connect** ✓

---

## 6. How the Bot Works Automatically

Understanding what the bot does on its own helps you know when to intervene.

### 6.1 Market Hours Gate

The bot only looks for trade signals between **9:15 AM and 3:30 PM IST on NSE trading days**.

- Outside these hours: the bot stays on but does not place any orders
- On NSE holidays: the bot is aware of the holiday calendar and skips those days automatically
- Market status is shown in the top bar of the client dashboard (green = OPEN, red = CLOSED)

### 6.2 Token Freshness Check (Avoids Unnecessary OTPs)

When you click **Start Bot**, the bot checks if your broker token was already refreshed today (after 6 AM IST).

- **Token is fresh** → Bot starts immediately, no OTP sent
- **Token is stale** → Bot attempts automatic login (if password/TOTP saved) and only then starts
- **No saved credentials and stale token** → Error shown: reconnect in Settings first

This means you will not receive unexpected OTP messages on your phone just because you restarted the bot during the day.

### 6.3 Daily Loss Kill-Switch

If the admin has set a **Daily Loss Limit** for you:

- The bot checks your P&L after every trade
- If your daily loss reaches or exceeds the limit, the bot **automatically stops** and locks trading
- A red banner appears: "Kill-switch active — Daily loss limit hit"
- Trading stays locked until the **next market day at 9:15 AM** (auto-unlock)
- Admin can manually unlock earlier from the client detail page

### 6.4 Dhan Auto-Renewal

Dhan access tokens are valid for 30 days. The platform automatically requests a new token before expiry. You do not need to do anything for Dhan renewals.

### 6.5 PM2 Auto-Restart and Crash Alerts

If the bot server crashes unexpectedly:

1. **PM2** detects the crash and restarts the server within seconds
2. A **Telegram alert** is sent to the admin: "🚨 AlgoSoft Bot is DOWN"
3. Once the server recovers, another alert: "✅ AlgoSoft Bot is back ONLINE"
4. The admin dashboard **Process Manager Status** widget shows a green/red live indicator

### 6.6 Scheduled Automatic Tasks

These run automatically every day without admin intervention:

| Time (IST) | What happens |
|------------|-------------|
| **8:30 AM** | Platform reconnects Upstox and Dhan global data feeds |
| **9:15 AM** | Kill-switch locks are cleared — clients who were locked yesterday can trade again |
| **9:15 AM** | Subscription expiry check — clients and admin are alerted if a plan expires in 7 or 1 day |
| **3:30 PM** | Day-end Telegram P&L summary sent to all active clients who have a Telegram Chat ID |

### 6.7 V3 Strategy Time Gates

The V3 Option Selling strategy has three time boundaries that govern when the bot trades each day. **Only admins can change these** — they are platform-wide risk controls, not per-client settings.

| Time Gate | Config key | Default | What it does |
|-----------|-----------|---------|--------------|
| **Market Open / Start Time** | `sell.start_time` | `09:15` | Bot begins scanning for entry signals after this time. Before this time the bot waits and does not evaluate any entry or exit conditions. |
| **No New Trades After** | `sell.v3.entry_end_time` | `14:00` | Entry cutoff. After this time the bot will not open any new straddle positions. Existing open trades continue to be monitored for exits normally. |
| **Force Square-Off At** | `sell.v3.square_off_time` | `15:15` | Hard EOD deadline. At this time ALL open positions are forcefully closed regardless of P&L. The exit reason recorded is `EOD_SQUAREOFF`. This is the definitive risk control that ensures no client is left with open overnight FO positions. |
| **Bot Close Time** | `sell.v3.close_time` | `17:00` | After this time the bot process winds down for the day. |

**How the gates interact (example for a typical day):**
1. `09:15` — Bot wakes up and starts looking for entry signals
2. `14:00` — Entry gate closes; no new trades are taken
3. `15:15` — **Force square-off fires** — all CE and PE positions are closed immediately
4. `15:20` — Bot shuts down until the next trading day

**To change a time gate:**
1. Log in as admin
2. Go to **Strategy** → **V3 Settings** tab
3. Edit the time fields under **"Time Gates (Admin Only)"**
4. Click **Save** — changes take effect on the next bot startup

> **Important:** The `square_off_time` gate runs independently of all other exit logic (TSL, ratio breach, etc.). It fires unconditionally as a platform-level safety net. Do not set it later than 15:25 to ensure all FO positions clear before exchange close.

---

## 7. Daily Routine Checklist

### Admin — Every Morning Before 9:15 AM

- [ ] SSH into EC2 and confirm server is running: `pm2 status` or check `http://<EC2-IP>:5000/health`
- [ ] Open Admin Dashboard → Data Providers → click **Auth** for Upstox (daily token refresh)
- [ ] Confirm both Upstox and Dhan show **LIVE** status
- [ ] Check Overview dashboard — no failures, feed ticks are recent

### Client — Every Morning Before 9:15 AM

- [ ] Log in to dashboard
- [ ] Check token status (top of Bot Control card) — should show **Fresh** (green)
  - If red/stale: go to Settings → reconnect your broker
- [ ] Verify **Mode** is set correctly: PAPER or LIVE
- [ ] Confirm **Daily Loss Limit** is set to your comfort level
- [ ] Turn on the **BOT toggle** to start the bot

### Client — During Market Hours

- [ ] Monitor the **Dashboard** tab for live P&L
- [ ] Check the **Positions** tab to see open legs
- [ ] If you need to exit a specific leg manually, use **Sq Off** on the Positions tab
- [ ] To pause new trades temporarily: switch **TRADE toggle** to OFF (bot stays on, no new entries)

### Client — After Market Hours

- [ ] Review **History** tab for the day's completed trades
- [ ] The bot can remain ON — it will not trade outside market hours
- [ ] Check your Telegram for the 3:30 PM daily P&L summary

---

## 8. Troubleshooting Common Errors

### "Token expired. Update Password/TOTP or reconnect in Settings"

**Cause:** Your broker access token was last refreshed before today's 6 AM IST cutoff.

**Fix:**
- Go to **Settings → Broker** → click **Reconnect** or complete the login flow for your broker
- For Zerodha: generate a new request token and exchange it (see Section 5.1)
- For Upstox: click Connect with Upstox and complete the OAuth flow

---

### "Daily loss kill-switch is active. Bot is locked until..."

**Cause:** Your daily loss exceeded the limit set by the admin. The bot automatically stopped.

**Fix:**
- Wait until the next trading day (lock auto-clears at 9:15 AM IST)
- Or ask your admin to unlock it early from the Admin → Client Detail → Risk & Money tab → 🔓 Unlock

---

### "Connection failed. Please provide Password/TOTP for One-Click Connect or manual access token"

**Cause:** No access token is saved and no One-Click credentials (password/TOTP) are stored.

**Fix:** Go to Settings → Broker → complete the full credential setup and login for your broker.

---

### Bot not starting — "Invalid API key" or "Authentication failed" error

**Cause:** The API Key or API Secret saved in Settings is incorrect or has been regenerated on the broker's platform since it was last entered.

**Fix:**
1. Log in to your broker's developer portal (e.g. [kite.trade](https://kite.trade) for Zerodha, [dhanhq.co](https://dhanhq.co) for Dhan)
2. Navigate to your API app settings and copy the current API Key
3. In AlgoSoft → Settings → Broker → click **Edit Credentials**
4. Re-enter the correct API Key and API Secret (do not paste extra spaces)
5. Save, then complete the broker login/token flow again
6. Confirm the token pill turns green before starting the bot

> If the error persists, check that the app's Redirect URI in the broker portal exactly matches the URL your admin configured. A mismatch causes authentication to fail even with correct credentials.

---

### Bot shows RUNNING but no trades are happening

**Possible causes:**
1. Market is closed (check the green/red market status bar at the top)
2. Today is a NSE holiday
3. The TRADE toggle is turned OFF — switch it ON
4. Your daily trade cap has been reached (check Settings → Risk)
5. Strategy conditions are not met yet — this is normal; the bot waits for the right signal

---

### Telegram alerts not arriving

**Possible causes:**
1. You have not started a conversation with the bot — search for your bot name in Telegram and send `/start`
2. Chat ID is incorrect in Settings — verify using `@userinfobot`
3. Admin has not configured the platform Telegram token — ask admin to check Platform Settings

---

### Admin: Global feed shows OFFLINE or STALE

**Fix:**
1. Go to Admin → Data Providers
2. Click **Auth** for Upstox (re-do the daily OAuth)
3. Check if the Dhan token is still valid (must be refreshed within 30 days)
4. If still offline, SSH into EC2 and check `pm2 logs algosoft-bot` for error details

---

### Admin: Bot server is down / not accessible

**Fix:**
```bash
# SSH into EC2, then:
pm2 status                    # Check if algosoft-bot is running
pm2 restart algosoft-bot      # Restart it
pm2 logs algosoft-bot --lines 50   # See last 50 log lines for errors
```

If PM2 is not installed yet:
```bash
cd /home/ubuntu/Option_Selling_May_2026/bot
bash scripts/restart_server.sh
```

---

## Appendix — Platform URL Reference

| URL | What it opens |
|-----|--------------|
| `http://<EC2-IP>:5000` | Login page |
| `http://<EC2-IP>:5000/register` | Client self-registration |
| `http://<EC2-IP>:5000/dashboard` | Client trading dashboard |
| `http://<EC2-IP>:5000/admin` | Admin overview |
| `http://<EC2-IP>:5000/admin/clients` | Client management |
| `http://<EC2-IP>:5000/admin/platform-settings` | Telegram + SMTP config |
| `http://<EC2-IP>:5000/health` | Server health check (JSON) |

---

*Guide version: April 2026 — AlgoSoft V4 Architecture*
