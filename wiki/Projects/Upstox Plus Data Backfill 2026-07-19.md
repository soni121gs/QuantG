---
title: Upstox Plus Data Backfill 2026-07-19
topic: Projects
tags:
  - upstox-plus
  - data-foundation
  - hermes
  - edge-rebuild
updated: 2026-07-19
---

# Upstox Plus Data Backfill 2026-07-19

Upstox Plus is founder-approved for QuantG's legal expired-instruments data lane. This unblocks [[Options Alpha Rebuild Strategy Pack 2026-07-05]] intraday option-history expansion and the Phase 1 Edge Rebuild Program data work.

## Source Of Truth

- Broker/account: Upstox V3 only.
- Plus capability: expired instruments APIs, expired F&O historical candles, V3 historical/intraday candles, WebSocket V3 Plus capacity, and D30 subscription mode.
- App endpoint: `GET /api/upstox/data-health`.
- Hermes tool: `get_upstox_data_health`.
- Frontend surfaces: Broker Keys, Market Hub, and Hermes Research Lab.

## Current Data Stores

- `data/bhavcopy_fo`: official F&O EOD bhavcopy store. Phase 1 now supports index and stock F&O rows (`IDO`, `IDF`, `STO`, `STF`).
- `data/options_1m`: Upstox expired-instruments 1-minute option candles, bounded by underlying, date, expiry, ATM window, and option type.
- `data/earnings_dates`: file-backed earnings calendar used by event-conditional research.
- `data/participant_oi`: participant-wise F&O OI store for flow overlays.

## Rules

- Legal sources only: broker account APIs, official exchange/public sources, or clearly licensed data.
- Hermes may read, summarize, and file findings; it does not trade, change code, or wake strategies.
- Coverage claims must come from store files or deterministic endpoints, not model memory.
- `CORE_ENGINE_LIVE_ENABLED=false` remains unchanged.

## Next Use

Hermes should consult `get_upstox_data_health` whenever the user asks about Upstox Plus, data coverage, backfills, options 1-minute readiness, bhavcopy freshness, earnings calendar data, or participant OI.
