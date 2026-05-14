# QuantDesk — Algo Trading Platform

## Original Problem Statement
> Can you create ke a algo trading software app. Where i can trade with my broker api key. I want its frontend should like tradetron. And I can make strategies with python code or I can integrate a live ai bot for trading.

## User Choices (locked-in)
- Broker: **Zerodha** (Kite Connect)
- AI bot: **Claude Sonnet 4.5** via Emergent Universal LLM Key
- Strategy builder: **Both** Python editor + Visual no-code builder
- Auth: **Email/password JWT** (Bearer token in localStorage)
- Market data: **Simulated** for MVP demo; structure ready for real Zerodha data

## Architecture
- **Backend**: FastAPI + Motor (MongoDB) + bcrypt + PyJWT + emergentintegrations (Claude Sonnet 4.5)
- **Frontend**: React 19 + React Router + Tailwind + shadcn/ui + Recharts + react-fast-marquee + lucide-react
- **Design**: Dark "Control Room" terminal aesthetic — Chivo / IBM Plex Sans / JetBrains Mono fonts, sharp 1px borders, green/red semantic PnL.

## What's Implemented (May 14, 2026)
- JWT auth (register / login / me) with bcrypt + 7d tokens.
- Mock real-time market data for 12 NSE symbols (RELIANCE, TCS, NIFTY, BANKNIFTY, …) with deterministic random-walk live prices.
- Broker key vault (Zerodha) — encrypted-at-rest pattern, masked display, delete.
- Strategies CRUD (Python + Visual), toggle live/paused, backtest with equity curve + trades + win-rate.
- Python sandbox executor (`run(data)` contract, restricted builtins).
- Order placement (BUY/SELL, MARKET/LIMIT) with positions auto-aggregation + PnL.
- Portfolio API with 30D equity curve.
- AI chat (Claude Sonnet 4.5) — multi-turn, persisted per session.
- Top ticker tape, live market-status indicator, real-time PnL in top bar.
- **Mobile-responsive UI**: hamburger drawer + bottom tab bar (Home/Strats/AI Bot/Orders/Holdings), horizontally-scrollable tables, 16px iOS-safe inputs.
- **Local-friendly**: runs on ~250 MB RAM. Setup guide at `/app/LOCAL_SETUP.md`.

## Pages
`/login`, `/signup`, `/dashboard`, `/strategies`, `/python`, `/visual`, `/ai-bot`, `/orders`, `/positions`, `/broker-keys`

## Backlog (Prioritized)
- P0: Wire real Zerodha Kite Connect SDK (token exchange + live order placement) once user provides API keys.
- P1: Strategy live runner (cron/worker) executing live strategies against real ticks.
- P1: Backtest sandbox hardening (timeout / memory cap / import allowlist).
- P2: Performance analytics page (Sharpe, drawdown, monthly returns).
- P2: Webhook-based trade alerts (Telegram / email).
- P2: Multi-broker support (Upstox, Angel One).

## Next Action Items
- User provides Zerodha API key/secret in `/broker-keys` to flip from paper → live.
- Optional: harden Python sandbox before exposing to untrusted users.
