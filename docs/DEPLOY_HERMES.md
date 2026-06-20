# Hermes Sidecar Deployment Runbook (TASK-H004)

This runbook guides you through creating a Telegram Bot, configuring the credentials, and deploying the isolated Hermes container to the VPS.

---

## Step 1: Create a Telegram Bot

1. Open Telegram and search for the official `@BotFather`.
2. Send the command `/newbot` to start the bot creation wizard.
3. Choose a friendly name for your bot (e.g. `QuantG Hermes Operator`).
4. Choose a username for the bot ending in `bot` (e.g. `quantg_hermes_bot`).
5. BotFather will provide an API Access Token (formatted like `1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ`). Copy this **Bot Token**.
6. Send a message to your new bot (e.g. click "Start" or send "hi") so that Telegram initializes a chat session between you and the bot.

---

## Step 2: Get Your Personal Chat ID

To make sure Hermes only alerts *your* account, get your Telegram Chat ID:
1. Search for `@userinfobot` on Telegram.
2. Send `/start`.
3. The bot will reply with your personal numeric **Id** (e.g. `987654321`). Copy this ID.

---

## Step 3: Configure Environment Variables on the VPS

1. Connect to the VPS via SSH:
   ```bash
   ssh -i C:\Users\MG\.ssh\codex_quantg_vps root@82.180.145.183
   ```
2. Navigate to the project folder:
   ```bash
   cd /opt/QuantG
   ```
3. Copy the template `.env.hermes.example` to `.env.hermes`:
   ```bash
   cp .env.hermes.example .env.hermes
   ```
4. Edit the file:
   ```bash
   nano .env.hermes
   ```
5. Replace the template values with your actual Telegram credentials:
   ```ini
    TELEGRAM_BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ
    TELEGRAM_CHAT_ID=987654321

    # Proactive behavioral alerts configuration (planned thresholds)
    DROUGHT_CUTOFF_IST=12:00
    DRAWDOWN_ALERT_FRAC=0.8
    LOSS_STREAK_N=3
    ```
6. Keep the defaults for `QUANTG_BACKEND_URL` and `QUANTG_OPERATOR_EMAIL`/`QUANTG_OPERATOR_PASSWORD` (which login to the local container via `http://backend:8000/api`).
7. Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

---

## Step 4: Build and Start the Hermes Sidecar

Run the following command on the VPS to build the Hermes image and start the container:
```bash
docker-compose up -d --build hermes
```

---

## Step 5: Verify Deployment & Logs

To check that the container started successfully and successfully sent the startup notification to Telegram:
```bash
docker-compose logs hermes --tail=50 -f
```

You should see log output similar to:
```
[AUTH] Logged in successfully as demo@quantdesk.io
[TELEGRAM] Message sent successfully
[AGENT] Hermes Sidecar Agent started successfully.
[WATCHDOG] Checking feed and session health...
```
And you should receive a message in Telegram:
`🚀 Hermes Sidecar Agent initialized and connected successfully on the VPS.`

---

## Two-Way Telegram Interface & Security

Hermes operates as a **two-way operator bridge**. Beyond the scheduled and triggered push notifications, you can query Hermes directly from Telegram.

### Security and Authorization

To protect your trading platform from unauthorized access, Hermes enforces a strict **Single-User Authorization Policy**:
- Hermes only processes messages originating from the configured `TELEGRAM_CHAT_ID`.
- Incoming messages from any other chat ID are **silently ignored** and logged.
- The command bridge is **strictly read-only**; no write or mutation calls can ever be triggered from Telegram.
- Any reply proposing a system action (`PROPOSED_ACTION`) is stripped of its JSON metadata, and Hermes appends a notice: `(Action proposed: Please approve in-app)`. All system updates must still be approved inside the QuantG web UI.

### Supported Commands

You can send the following commands to the bot from your authorized Telegram chat:
1. **/status** - Calls the backend feed status and live readiness checks, returning a consolidated system readiness report.
2. **/pnl** - Fetches the daily report for today (IST) and formats it like the standard End-of-Day report.
3. **/why [question]** or **[free text]** - Queries the Hermes AI analyst agent. For example, `/why why didn't Nifty trade today?` or just `how are my positions doing?`. The reply will include a `sources` footer listing the read-only tools used to answer your question.
