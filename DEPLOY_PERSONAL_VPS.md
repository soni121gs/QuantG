# QuantG Version 6 Personal VPS Runbook

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
```

Never change `JWT_SECRET` unless you accept logging in again.
Never change `CREDENTIAL_ENCRYPTION_KEY` unless you accept saving Zerodha keys again.

## 4. Deploy Version 6

From your repo folder on the VPS:

```bash
git pull
docker compose pull
docker compose up -d --build
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
docker compose up -d --build
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

- show Zerodha, ticker, readiness, strategy, order, and error health
- restart the Zerodha ticker websocket
- pause all live strategies
- switch to PAPER and pause automation with Emergency Stop
- clear old visible strategy errors after fixing the cause

It cannot fix broker-side rejected orders, expired Zerodha login, wrong Kite redirect URL, blocked DNS/firewall, or invalid strategy logic. Those still need manual correction.

## Current App Features

- Email/password login
- Zerodha Kite Connect keys and daily OAuth connection
- Paper/live mode switch
- Watchlist and ticker tape
- Strategy builder and Python strategy editor
- Background strategy runner
- Paper orders and positions
- Live order placement through Zerodha
- Strategy signal confidence filter
- Daily readiness checks
- Ops Console for recovery during market hours

## Limitations And Drawbacks

- Zerodha requires daily reconnect; this is a broker limitation.
- Current market analysis is rule-based, not a guaranteed prediction system.
- Strategy code is sandboxed but still meant for personal use, not hostile multi-user SaaS.
- No guaranteed order fill price; live fills depend on broker/exchange conditions.
- No automatic VPS database backup scheduler yet.
- No mobile push/WhatsApp/Telegram alerting yet.
- No multi-broker failover yet.

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
