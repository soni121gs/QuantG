# QuantG — Algo Trading Platform

## Original Problem Statement
> Can you create ke a algo trading software app. Where i can trade with my broker api key. I want its frontend should like tradetron. And I can make strategies with python code or I can integrate a live ai bot for trading.

## User Choices
- Broker: **Zerodha** (Kite Connect v3 — REST OAuth)
- AI bot: **Claude Sonnet 4.5** via Emergent Universal LLM Key
- Strategy builder: **Both** Python editor + Visual no-code builder
- Auth: **Email/password JWT** (Bearer token in localStorage)
- Live data: Live LTP/quote via Kite REST when connected; mock fallback otherwise
- Live orders: Real Kite execution gated by Profile paper/live switch

## Architecture
- **Backend**: FastAPI + Motor + bcrypt + PyJWT + emergentintegrations (Claude) + kiteconnect 5.2.0
- **Frontend**: React 19 + Tailwind + shadcn/ui + Recharts + react-fast-marquee
- **Background**: asyncio strategy runner ticking every 30s
- **Design**: Dark Control-Room terminal aesthetic (Chivo / IBM Plex Sans / JetBrains Mono)

## What's Implemented
### Iteration 1 (May 14)
- JWT auth, mock 12 NSE symbol watchlist with random-walk live ticks
- Strategies CRUD (Python + Visual), backtest with equity curve + win rate
- Python sandbox executor with `run(data)` contract
- Order placement, positions, portfolio, AI chat (Claude Sonnet 4.5)
- Mobile-responsive UI (drawer + bottom nav + scrollable tables)

### Iteration 2 (May 14)
- **Zerodha Kite Connect REST integration** — per-user OAuth, daily token refresh, live LTP/quote/positions/holdings
- **Profile page** — name, email, password change, default qty/product, risk limits (max daily loss, max position size), Zerodha session status
- **Paper/Live master switch** — flips between simulated and real execution
- **Background strategy runner** — auto-evaluates live strategies every 30s and places orders via the same business logic (so paper/live + risk limits are always honoured)
- **Token-expired banner** + Zerodha session status card with re-connect flow
- **Mode badge in top bar** (PAPER / LIVE indicator)
- **Order safety guards** — qty>0 Pydantic validator, max_position_size enforcement, live-mode requires Zerodha connection
- Backend tests: 34/34 passing (16 new + 18 existing)

## Pages
`/login`, `/signup`, `/dashboard`, `/strategies`, `/python`, `/visual`, `/ai-bot`, `/orders`, `/positions`, `/broker-keys`, `/profile`

## Backlog (Prioritized)
- P0: WebSocket KiteTicker for sub-second ticks (currently REST polling)
- P1: Strategy-runner subprocess sandbox + execution timeout (avoid runaway loops blocking the loop)
- P1: Email / Telegram notifications on trade fills (need integration setup)
- P1: Password confirmation when flipping to LIVE mode
- P2: Holdings page UI, performance analytics (Sharpe, drawdown, monthly returns)
- P2: Refactor server.py into modular routers (~1000 lines now)

## Next Action Items
- **User**: Add the Redirect URL shown on Broker Keys page to your Kite app, save api_key+secret, click "Connect to Zerodha" to start live trading.
- **Engineering**: Wire WebSocket ticks (KiteTicker) + add Telegram/email notifications.
