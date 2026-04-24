# AlgoSoft Bot Operational Guide (V4 Architecture)

Welcome to the AlgoSoft trading platform. This guide explains how to operate the bot following our major architectural upgrade to a **Global Redundant Data Feed** model.

## 1. Core Concepts
- **Redundant Data Feeds:** Market data (LTP/OHLC) is now sourced globally via **Upstox** and **Dhan** feeds. Individual client brokers (Zerodha, etc.) are used exclusively for **order execution**.
- **Execution Isolation:** Your strategy runs on our servers using the high-fidelity global feed, ensuring that your trades are placed instantly on your selected broker without relying on that broker's potentially unstable data feed.
- **Multi-Broker Support:** You can connect Zerodha, Dhan, AngelOne, or Upstox. However, free accounts are restricted to one active broker at a time.

---

## 2. Platform Setup (Admin Only)

Before any client can trade, the **Global Data Providers** must be configured by an administrator.

1.  Navigate to the **Admin Dashboard** -> **Data Providers**.
2.  Click **Configure** for Dhan and Upstox.
3.  Enter the platform's Master API credentials.
4.  For Upstox, click the **Auth** button to complete the daily OAuth handshake.
5.  Verify that both providers show a "Connected" status.

---

## 3. Client Trading Workflow

### Step 1: Broker Configuration
1.  Go to the **Settings** tab in your dashboard.
2.  Select your **Primary Execution Broker** (e.g., Zerodha).
3.  Enter your API Key and Secret. Click **Save Credentials**.
4.  If using Zerodha, click **Login with Zerodha** to generate your daily access token. For Dhan/Upstox, use the 'One-Click' connect flow.

### Step 2: Trading Parameters
1.  In the **Trading Configuration** card, select your instruments (e.g., NIFTY, BANKNIFTY).
2.  Set your **Quantity (Lots)** and select **Strategy Version V3**.
3.  Choose between **Paper Trading** (Simulated) or **Live Trading** (Real Money).
4.  Click **Save Configuration**.

### Step 3: Start Trading
1.  Switch to the **Trading** tab.
2.  Click the **Start Bot** button.
3.  The bot will initialize using the global feeds and start monitoring for signals.
4.  Monitor real-time stats (ATM Strike, P&L, V3 Strategy Monitor) and logs in the dashboard.

---

## 4. Backtesting
1.  Navigate to the **Backtest** tab.
2.  Select the **Instrument**, **Date**, and **Quantity**.
3.  Click **Run Backtest**.
4.  The engine will download historical data via the global feed and simulate the Strategy V3 logic for that day.
5.  View the detailed console logs and the generated Order Book for analysis.

---

## 5. Troubleshooting
- **"Broker not configured":** Ensure you have saved credentials and completed the login/auth step for your selected broker.
- **"Global feed not active":** Contact your administrator to refresh the platform's redundant data feeds.
- **Bot "Stale" warning:** If the dashboard shows a staleness warning, the bot process may have crashed or lost connection. Click **Restart** to restore operation.
- **Zero Trades in Backtest:** Ensure the market was open on the selected date and that your broker (or global feed) has historical data available for that period.
