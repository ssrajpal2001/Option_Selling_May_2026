# AlgoSoft V3 Connection Guide

This guide explains how to connect **Global Data Providers** (Dhan/Upstox) and **Client Execution Brokers** to the bot.

---

## 🚀 Option 1: 100% Background Automation (Recommended)
This method allows the bot to log in and refresh tokens by itself every morning without you clicking anything.

### Step 1: Get your TOTP Secret
1. Log in to your broker's Web/Mobile app.
2. Go to **Security / 2FA** settings.
3. Look for **"Enable External TOTP"** or **"View TOTP Secret Key"**.
4. Copy the **32-character** code (e.g., `LHSYOVNLP7...`).

### Step 2: Configure the Bot
1. Go to **Admin Panel** -> **Data Feeds**.
2. Click **Configure** for Dhan or Upstox.
3. Fill in the **Automation Credentials** section:
   - **User ID / Mobile**: Your broker login ID.
   - **Password / PIN**: Your broker login password.
   - **TOTP Secret Key**: The 32-character code from Step 1.
4. Click **Save Credentials**.
5. **That's it!** The bot will now handle logins automatically in the background.

---

## 🖱️ Option 2: One-Click Manual Connect
Use this if you don't want to provide your password to the bot.

### For Dhan:
1. Click **Connect Dhan** in the Admin Panel.
2. You will be redirected to the Dhan login page.
3. Log in normally.
4. Once finished, you will be redirected back to the bot, and the status will turn **Connected**.

### For Upstox (Manual Fallback):
*Note: Because we are on a private server, Upstox will redirect you to Google after login.*

1. Click **Connect Upstox** in the Admin Panel.
2. Log in with your mobile number and OTP.
3. You will be redirected to a **Google page** (e.g., `https://www.google.com/?code=XXXX...`).
4. **Copy the ENTIRE URL** from your browser bar.
5. Go back to the Bot Admin Panel.
6. Click **Configure** for Upstox.
7. Paste the URL into the **"Manual Token / Redirect URL Fallback"** field.
8. Click **Save**. The status will turn **Connected**.

---

## 👤 Client Onboarding
When a new user joins:
1. They go to the **Registration Page**.
2. They provide their **Name**, **Phone**, and **Broker of Choice**.
3. Once you (Admin) activate them, they log in to their dashboard.
4. They enter their API credentials in **Settings** and flip the **"Broker Connection"** toggle.
5. Once the bot is connected, they flip **"Start Trading"** to begin taking orders from the Global feeds.
