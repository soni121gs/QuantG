# QuantG Version 8 Personal VPS Runbook

Domain: `www.quantgtrade.com`
Root domain: `quantgtrade.com`
VPS: Contabo `82.180.145.183`, 12 GB RAM, 100 GB NVMe

This setup runs from anywhere through HTTPS. Your laptop/home WiFi is not part of the runtime path.

## 1. DNS

In your domain DNS panel, create:

```text
A  @    82.180.145.183
A  www  82.180.145.183
```

Wait until both resolve:

```bash
dig +short quantgtrade.com
dig +short www.quantgtrade.com
```

Both should show `82.180.145.183`.

## 2. VPS Firewall

Only expose SSH, HTTP, and HTTPS:

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status
```

Do not open `27017`. Do not open `8000`.

## 3. Required Env

On the VPS, edit `backend/.env`:

```env
MONGO_URL=mongodb://mongo:27017
DB_NAME=quantg
JWT_SECRET=replace-with-a-long-random-secret-and-never-change-it
CREDENTIAL_ENCRYPTION_KEY=replace-with-a-second-long-random-secret-and-never-change-it
CORS_ORIGINS=https://www.quantgtrade.com,https://quantgtrade.com,http://82.180.145.183
SIGNAL_CONFIDENCE_MIN=45
OPTION_LEDGER_PATH=/data/runtime_state.sqlite3
LIVE_ORDER_MAX_ATTEMPTS=2
KOTAK_ORDER_MAX_ATTEMPTS=1

# Google AI Studio / Gemini API for QuantBot
GEMINI_API_KEY=your_google_ai_studio_api_key
GEMINI_MODEL=gemini-3.5-flash
GEMINI_TIMEOUT_SEC=20

# Kotak Neo V2 session unlock
KOTAK_MOBILE_NUMBER=your_registered_mobile
KOTAK_UCC=your_kotak_client_code
KOTAK_MPIN=your_mpin
KOTAK_TOTP_SECRET_KEY=your_totp_secret
KOTAK_NEO_FIN_KEY=optional_neo_fin_key_if_not_saved_in_broker_keys
```

Never change `JWT_SECRET` unless you accept logging in again.
Never change `CREDENTIAL_ENCRYPTION_KEY` unless you accept saving Zerodha keys again.
Remove `EMERGENT_LLM_KEY` from old `.env` files. QuantBot now uses `GEMINI_API_KEY`.

## 4. Deploy Version 8

From your repo folder on the VPS:

```bash
git pull
docker compose down
docker compose build --no-cache backend frontend
docker compose up -d
docker compose ps
docker compose logs -f caddy
docker compose logs -f backend
```

The SQLite option-engine ledger is persisted in the Docker volume `backend-state`.
Do not delete this volume during normal updates.

Open:

```text
https://www.quantgtrade.com
```

Caddy will issue the SSL certificate automatically once DNS points to the VPS and ports `80/443` are open.

## 5. Zerodha Redirect URL

In Kite Developer Console, set redirect URL to:

```text
https://www.quantgtrade.com/broker-keys?status=success
```

Zerodha access tokens expire daily. Reconnect each trading morning before live trading.

## 6. Safe Runtime Commands

Restart app without deleting data:

```bash
docker compose restart
```

Update after code changes:

```bash
git pull
docker compose build --no-cache backend frontend
docker compose up -d
```

Stop without deleting data:

```bash
docker compose down
```

Never run this unless you intentionally want to delete accounts, broker keys, strategies, orders, and positions:

```bash
docker compose down -v
```

This also deletes the SQLite option-engine runtime ledger because it removes the
`backend-state` volume.

## 7. Backup Mongo

Run before risky deploys:

```bash
docker exec quantg-mongo mongodump --archive=/tmp/quantg.archive --db quantg
docker cp quantg-mongo:/tmp/quantg.archive ./quantg-$(date +%F-%H%M).archive
```

Restore only when the app is stopped:

```bash
docker cp ./quantg.archive quantg-mongo:/tmp/quantg.archive
docker exec quantg-mongo mongorestore --archive=/tmp/quantg.archive --drop
```

## 8. Ops Console

Inside the app, open:

```text
https://www.quantgtrade.com/ops
```

It can:

- show Zerodha/Kotak, ticker, readiness, strategy, order, and error health
- restart the Zerodha ticker websocket
- run Auto Recover for safe order sync, ticker restart, and connected order feeds
- pause all live strategies
- switch to PAPER and pause automation with Emergency Stop
- clear old visible strategy errors after fixing the cause

It cannot fix broker-side rejected orders, expired Zerodha login, wrong Kite redirect URL, blocked DNS/firewall, or invalid strategy logic. Those still need manual correction.

## 9. Tomorrow Morning Checklist

Before live market use:

```bash
cd /root/QuantG
git pull
docker compose build --no-cache backend frontend
docker compose up -d
docker compose ps
docker compose exec backend python -c "from neo_api_client import NeoAPI; print('Kotak SDK OK')"
docker compose logs --tail=80 backend
```

Then in the app:

1. Hard refresh browser with `Ctrl + Shift + R`.
2. Open Broker Keys.
3. Reconnect Zerodha if you use Zerodha data or execution.
4. Click Connect Kotak if you use Kotak execution/order feed.
5. For Kotak commodities, connect Kotak, open Orders, choose `MCX`, search the current Kotak trading symbol, then place the order with `NRML` or `MIS`.

Kotak diagnostic commands after deploy:

```bash
docker compose exec backend python -c "from neo_api_client import NeoAPI; print('Kotak SDK OK')"
```

Inside the app, open Broker Keys and Market Hub to check Kotak status and the latest Neo rejection message.
5. Open Ops Console and run Auto Recover.
6. Confirm Live Recovery Plan is READY or only has non-blocking market-closed info.
7. Open Market Hub and check Ticker Quality plus Signal Stack.
8. Keep PAPER until real ticks are visible and readiness is clean.

## Current App Features

- Email/password login
- Zerodha Kite Connect keys and daily OAuth connection
- Kotak Neo V2 credential storage, TOTP/MPIN session unlock, order feed start, and live order routing scaffold
- Paper/live mode switch
- Watchlist and ticker tape
- Strategy builder and Python strategy editor
- Background strategy runner
- Paper orders and positions
- Live order placement through Zerodha or Kotak Neo depending on execution broker
- Tagged live orders with recovery lookup and retry guard
- Strategy signal confidence filter with trend, RSI, VWAP, ATR, volume, and higher-timeframe checks
- Market Hub ticker comparison and Signal Stack
- Visual Builder indicators: RSI, SMA, EMA, MACD, VWAP, ATR, ATR%, Volume Ratio
- Daily readiness checks
- Ops Console for recovery during market hours

## Limitations And Drawbacks

- Zerodha requires daily reconnect; this is a broker limitation.
- Kotak data needs subscribed Kotak instrument tokens before it can be measured as a live ticker feed.
- Current market analysis is rule-based, not a guaranteed prediction system.
- Strategy code is sandboxed but still meant for personal use, not hostile multi-user SaaS.
- No guaranteed order fill price; live fills depend on broker/exchange conditions.
- No automatic VPS database backup scheduler yet.
- No mobile push/WhatsApp/Telegram alerting yet.
- Multi-broker failover is health-based, but you must verify broker sessions and symbol-token subscriptions before live use.

## Future Scope

- Telegram alerts for token expiry, order rejection, ticker stale, and emergency stop
- Daily pre-market checklist at 9:00 IST
- Per-strategy max trades per day and cooldown
- Trade journal with entry reason, signal confidence, chart snapshot, and exit reason
- Broker order reconciliation job every few minutes
- Real-time P&L from live Zerodha positions
- Automatic Mongo backups to S3/Google Drive
- HTTPS-only production CSP headers after frontend asset audit
- Backtest report with drawdown, expectancy, slippage, brokerage, and walk-forward testing
- Strategy marketplace/private library for your own reusable templates
