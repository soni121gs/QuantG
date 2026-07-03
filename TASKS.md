# TASKS.md — QuantG Agent Work Queue

**Read AGENTS.md before starting any task.**
**Pick the first open task `[ ]` that matches your model tier. Mark `[~]` when starting, `[x]` when done.**

Legend: `[ ]` open · `[~]` in progress · `[x]` done · ⛔ blocked (prerequisite not done)

---

## CURRENT STATE & ACTIVE QUEUE (updated 2026-07-04)

**⭐ HEADLINE (2026-07-04): the data wall is broken, and the OOS verdict is in — the current book has NO EDGE.**
2 years of real NSE option prices are ingested (`backend/scripts/bhavcopy_ingest.py`, 494 days, ~2.5M rows) and the EOD OOS validator (`backend/core/eod_options_backtest.py`) graded the whole book: **0 of 11 option strategies are positive out-of-sample**; a **72-config sweep found 0 winners**. Corroborates live ~−₹86/trade. This UNBLOCKS old `WR-71` (real options-chain backtest) and completes the data layer for HSI Stage 4. See CLAUDE.md §13.

**🚫 NEW LAW (supersedes daily tweaking):** do NOT tune the existing strategies. Every strategy change / new strategy must PASS the OOS validator first. Discipline: hypothesis → OOS backtest → forward-paper → live. Grade ideas on OOS expectancy, not daily paper P&L.

**▶ NEW ACTIVE PROGRAM — Edge Discovery & Book Rebuild (EDR):**
- `[ ]` **EDR-01** Base-rate studies on the bhavcopy data (short-vol vs long-vol; ATM straddle-to-expiry; iron-condor-in-RANGE; underlying daily trend) — find WHERE edge could live before building. Tier 3.
- `[ ]` **EDR-02** Design NEW option strategies from EDR-01 findings; each must pass `run_oos_validation.py` (CANDIDATE_EDGE, 30+ trades, positive OOS). Tier 3.
- `[ ]` **EDR-03** Archive + de-template the 11 dead option strategies + 10 equity (touch-points in CLAUDE.md §13). Off-hours, one commit + rebuild, PAIRED with EDR-02 replacements so the book isn't emptied. Update catalog tests. Tier 3.
- `[ ]` **EDR-04** Bake the research modules (`bhavcopy_store`, `eod_options_backtest`, `run_*`) into the backend image on next off-hours rebuild (currently `docker cp`'d for ad-hoc runs); ship `/app/data/bhavcopy_fo`. Tier 2.
- `[ ]` **EDR-05** (data gap) BSE SENSEX/BANKEX bhavcopy is Akamai-gated — needs a browser/manual fetch; and equity needs NSE_EQ stock EOD data before equity can be backtested/rebuilt. Tier 2.

**App reality now:** Paper mode (`CORE_ENGINE_LIVE_ENABLED=false`). ~24 strategies live but **none proven** — index options (single-leg + credit/debit spreads) + equity (10 NSE_EQ). Truth-bearing P&L source is `db.trade_fills`; the paper wallet self-heals to that ledger at EOD. Profit-lock + daily-loss kill-switch + directional-exposure cap all live.

**Shipped 2026-06-30 (this session — all deployed to VPS):**
- `[x]` **FIX-01** Phantom-wallet ROOT CAUSE fixed: equity exits were sized as fresh capital-based SELL orders (e.g. 29 sh sold vs 17 held) → wallet over-credited ~₹160k of fake "profit" while real P&L was negative. Equity exits now route through `close_strategy_fn` (reduce-only, sells `open_quantity`). `ff4fd58` + wallet reset to clean ₹500k.
- `[x]` **FIX-02** Self-healing wallet reconciliation (`PaperWallet.reconcile_if_flat`, wired into `position_monitor` EOD): when the book is flat, balance is snapped to `initial + Σ realized_pnl`; any drift logged CRITICAL + auto-healed. Permanently kills the recurring phantom-money class. `0c3d1b2`.
- `[x]` **FIX-03** Converted the 3 worst directional debit spreads → theta **credit spreads** (NIFTY Quick EMA Scalper / BANKNIFTY HFT Momentum Scalper / BANKNIFTY Breakout Buyer — all lost every trade in chop) via idempotent `_migrate_credit_spread_structure`. `873117a`.
- `[x]` **WR-53** Directional-exposure cap `MAX_DIRECTIONAL_EXPOSURE_PER_UNDERLYING=3`: blocks a new entry when N strategies already hold the same BULLISH/BEARISH bias on one underlying (per-position bias via `strategy_runner._position_exposure_bias`). The decorrelation lever. `873117a`.
- `[x]` **FIX-04** Hermes session-memory wiki notes now titled `Session Memory <date> HH:MM:SS IST` + the approve path auto-dedupes title/slug → every memory is approvable (was "A document with this title already exists"). `b42acaf`.
- `[x]` **DOC-01** Planned the 5-stage **Hermes Self-Improvement Loop** (HSI-11..54 — bottom of this file): the concrete path to a self-improving trading brain.

**Shipped 2026-06-24 (live since — folded to history):**
- `[x]` **CUR-01..06** NIFTY Quick EMA Scalper de-scalp; spreads ignore reverse signals; equity counter-trend gate; equity capital tiers; 15:25 spread squareoff; BHARTIARTL anti-churn. `5555589`/`1f03c5d`/`b274aac`.

**KEY MECHANIC (still true — read before editing any strategy):** DB risk edits on the ~19 default strategies do NOT stick — a startup template re-sync (`migrate_user_to_v12_upstox`→`_risk_update_fields`) rewrites cooldown/SL/TP/maxTrades/category from the in-code template. Only `required_capital` and `visual_config.options.structure` survive a DB-only edit (or add a dedicated idempotent startup migration, as `_migrate_credit_spread_structure` does). **To durably change risk on a default strategy, edit the template in `server.py` (`DEFAULT_OPTION_STRATEGIES` + `UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME`); note there is no `equity_trend` preset → all equity silently uses the `momentum` preset.**

---

### ▶ OPEN TASKS INDEX — pick from here (full detail in the sections below)

> Status: `[ ]` open · `[~]` in progress (add your model name) · `[x]` done. IDs use a domain prefix (`AR-`, `HSI-`, `WR-`, `HSB-`, `CUR-`) — not all `TASK-###`. Start with the highest item your tier can handle; read the task's "Files / Acceptance" before coding.

**🩹 Alpha Repair Campaign (2026-07-02 full-book analysis) — START HERE (§ below)**
- **DONE 2026-07-02:** `AR-01`+`AR-02` risk/config epoch, `AR-03` equity ATR brackets, `AR-04` equity economics/cutoff, `AR-05` attribution inputs, `AR-06` BANKNIFTY theta expiry guard.
- **NEXT → `AR-07` is data-gated** on the new AR-05 stamped attribution window; `AR-08` measurement checkpoint is due after ~8 trading sessions from the AR-01..06 deploy window.

**🧠 Hermes Self-Improvement Loop — the headline initiative (§ bottom of file)**
- **Stages 1–4 SHIPPED** ✅ `HSI-11..15` Stage 1 attribution · `HSI-21..23` Stage 2 grounded EOD · `HSI-31..34` Stage 3 scored lesson store (`fb486ef`, 2026-07-01) · `HSI-41..44` Stage 4 OOS validator (`d1553fc`, 2026-07-03 — judge-first, returns `INSUFFICIENT_DATA` until held-out samples mature).
- **NEXT → `HSI-51..54`** Stage 5 gated advisor — founder-gated and must require `oos_passed=true`.

**📈 Win-Rate / Expectancy (open remainder — deduped 2026-07-02)**
- Open: `WR-33` deferred (re-open only per AR-08 evidence) · `WR-45` correlation matrix · `WR-51` risk sizing *(after AR-08)* · `WR-54` auto-pause *(⛔ gated on AR-01/AR-08)*. **Folded 07-02:** `WR-42`/`WR-43` → AR-07/AR-08 · `WR-44`/`WR-72` → HSI-41..44.
- Bigger builds: `WR-71` real options-chain backtest **✅ UNBLOCKED + DONE 2026-07-04** (free NSE bhavcopy replaced the Upstox expired-option 404 wall; EOD OOS validator shipped — see EDR program above) · `WR-73` enable live on 2–3 proven *(founder gate — now also requires an OOS `CANDIDATE_EDGE`, which NO current strategy has)*

**🏗 Backlog programs — do NOT start unless the founder directs:** Architecture redesign Stages 0–1 (event catalog / publish-only bus — see CLAUDE.md §11) · Hermes integration `HSB-11..17` (AutoResearch ratchet — overlaps HSI Stages 4–5, reuse not fork) · Phase-2 UI polish · capital allocator.

**🔧 OPS hygiene (from 2026-07-02 live audit — do AFTER market close, § below):** `OPS-01` portfolio-stream 401 storm (P1) · `OPS-02` 429 rate-limiting · `OPS-03` RELIANCE Trend Rider orphaned-paused · `OPS-04` Hermes Telegram 404 · `OPS-05` verify wallet reconcile.

---

## OPS HYGIENE — from live full-system audit (2026-07-02, market open, no changes made)

**Audit verdict: system HEALTHY and trading correctly.** Real feed (0 mock fallbacks), 57 fills today, MTM fresh (2–3s), no stuck EXITING/CIRCUIT_BREAKER positions, HSI brain intact (attribution 06-30/07-01, 10 candidate lessons scored 07-01, daily_reports through 07-01), self-healing wallet ledger working. Below are the non-urgent cleanups found — **none affect trading correctness or money integrity; do after 15:30 IST.**

- `[x]` **OPS-01 (P1) — Upstox portfolio-stream 401 storm, no backoff.** DONE 2026-07-02: backend now only starts the Upstox portfolio stream when `CORE_ENGINE_LIVE_ENABLED=true`; paper mode keeps REST reconciliation but skips the portfolio WS handshake entirely. Commit: `18f70fd`. Acceptance after deploy: 401 log rate → ~0, backend CPU baseline should drop. Recheck OPS-02 after this because it may remove most of the contention.
- `[ ]` **OPS-02 (P2) — 429 rate-limiting on `/v2/market-quote/ltp`** (~15/min, 231 in 15m). Tolerated today (MTM stays fresh, 0 mock fallbacks) but wasteful; partly caused by OPS-01 contention. Recheck after OPS-01 lands; if still present, add quote batching/throttle on the monitor+guardian quote path. **Early recheck 2026-07-03 02:06 IST:** current rebuilt backend container has 0 HTTP 429 matches; only "rate-limited" matches are the gateway's own duplicate-warning suppression for missing broker timestamps. Keep open for one market-hours load recheck because the current sample is overnight.
- `[x]` **OPS-03 (P3) — RELIANCE Trend Rider orphaned-paused.** DONE 2026-07-03: RELIANCE was exactly orphaned (`status=paused`, `manual_paused=false`, `schedule_paused=false`) with 0 open positions, 0 pending signals, 0 open orders. After the equity ATR/deadline fixes, production Mongo now has `status=paused`, `manual_paused=false`, `schedule_paused=true`, so the 9AM scheduler will adopt/reactivate it with the rest of the book instead of leaving it idle overnight. No code commit; DB-only ops fix.
- `[ ]` **OPS-04 (P3) — Hermes Telegram alerts 404.** `.env.hermes` bot token/chat_id is still a placeholder → every `[TELEGRAM] Send failed 404`. Alerts undelivered (doesn't affect trading). Known ([[ops_hermes_creds_and_core_status_bug]]). Fix: real bot token + chat_id, then force-recreate hermes (restart won't reload env_file).
- `[x]` **OPS-05 (P3, verify only) — wallet vs realized-P&L reconcile.** DONE 2026-07-02: exact wallet reset timestamp is `2026-06-30T15:03:34.386Z`; wallet balance ₹498,889.87 implies Δ −₹1,110.13 from ₹500k, and `trade_fills` realized since reset is exactly −₹1,110.13 across 84 fills. No open reserved positions. Close as benign epoch mismatch, not residual phantom-credit. Commit: `b47f28d`.

---

## PRIORITY 0 — Alpha Repair Campaign (2026-07-02)

**Source**: Full-book strategy analysis 2026-07-02 — 118 closed trades since the clean 2026-06-25 baseline
(VPS Mongo: `strategy_positions` + `trade_attribution` + `hermes_lessons` + live configs; memory
`strategy-book-analysis-07-02`). Book since baseline: +₹13.0k (06-25) → −₹9.0k (06-29) → −₹9.4k (06-30) →
−₹0.5k (07-01) → −₹2.4k (07-02) ≈ **−₹8.2k net**.

**Verdict: the book produces alpha and then interrupts it.** Every exit that lets a trade complete its thesis
is green — `spread-tp` +₹4,239 @ 100% WR · `profit-lock-book-trail` +₹6,938 @ 100% · `squareoff-1525` +₹4,139
@ 62.5% — and every exit that interrupts is red — **`daily-loss-killswitch-strat` −₹21,177 across 28 trades**
(more than the entire net loss) · `time-exit-22m` −₹2,406 @ 3.8% · signal-flips −₹926 @ 7.7%. The winning
cluster is **credit spread + RANGE regime + held 2h+ to theta TP or EOD** (SENSEX Theta Credit Spread +₹2,664
@ 80% WR, n=10, is the template). Leaks ranked: (1) killswitch geometry, (2) equity cost structure + dead ATR
brackets, (3) regime blindness (79% of attribution UNKNOWN), (4) afternoon entries (−₹9,411; hour-13 IST alone
−₹7,964 vs morning +₹2,176), (5) debit spreads (−₹8,260 @ 18% WR), (6) scalper configs on theta trades,
(7) one-sided book (44 BULLISH vs 8 BEARISH entries).

**Evidence-strength note**: AR-01 and AR-04's cost math are *arithmetic* (a ₹700 limit under a ₹5k designed
risk is incoherent at any sample size), not statistics. The regime/time-of-day findings are directional on a
small sample — ship them env-gated with skip-reason codes so attribution confirms or kills them.

**Sequencing**: AR-01+AR-02 ship together (same `server.py` template edit — one deploy, one measurement
epoch; note the deploy date in `db.app_config`). AR-03 is an independent P1 bug, can land the same day.
AR-04/AR-05 next. AR-06 is verify-then-config. AR-07 is ⛔ data-gated on AR-05 + ~2–3 weeks of stamped
attribution (same clock as HSI-41..44). AR-08 closes the loop. **Do NOT pile unrelated tuning into the
measurement window** (WR-33 stays deferred).

---

### AR-01 — Risk geometry: make the per-trade stop the risk unit, daily limit a multiple of it
- **Status**: `[x]` DONE 2026-07-02, commit `18f70fd`
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2.5 hours
- **Prerequisite**: None. **Ships WITH AR-02** (same template edit, one deploy).
- **Targets**: the −₹21,177 `daily-loss-killswitch-strat` bucket (28 trades since 06-25); 20 strategy-day
  locks in 5 sessions; ALL of 06-29's −₹9.0k day was killswitch force-closes

**Problem**: every options strategy is designed to risk ~₹4.4k–7.5k per spread (`planned_risk` on live fills)
but carries a ₹600–1,200 `daily_loss_limit` → one normal mid-trade drawdown force-closes at the worst mark and
locks the day (`LOSS_LOCKED_DAY` suppressed 173 signals since 06-25). Short premium routinely marks against you
intraday and then decays back — our own `squareoff-1525` exits are +₹4,139 @ 62.5% WR. WR-52 already flagged
this ("floors inherit the small momentum-preset value"). Live example 07-02: `strat:-762<=-700` locked SENSEX
Swing RSI Pullback after ONE designed-size trade. Theta diagnosis 07-01 called the same lead.

**Invariant to enforce: `daily_loss_limit ≥ 2 × designed per-trade stop`, for every strategy, forever.**

**Files to touch**: `backend/server.py` (`DEFAULT_OPTION_STRATEGIES` risk template + `_risk_update_fields` —
DB-only edits get re-synced away, see KEY MECHANIC at top of this file), `backend/position_monitor.py` (spread
SL knob), `docker-compose.yml` (env), `backend/core/loss_killswitch.py` (verify only — which field it reads).

**Exact steps**:
1. Locate the spread stop mechanism (grep `spread-sl` in `position_monitor.py`) and its level knob. Evidence:
   it fired once at −₹2,014 on a ₹4,473-planned-risk spread → current stop ≈ 45% of max loss. Set the spread
   stop to **1.5–2× credit received** (≈ ₹1.5–2k at current sizing) if it isn't already; make it env-tunable.
2. In the in-code template, raise options `daily_loss_limit` to **2–3× that stop (₹3,000–4,500)** for all 13
   options strategies. Leave equity dll alone (equity's problem is costs, see AR-04).
3. Keep `PORTFOLIO_DAILY_LOSS_LIMIT` (₹20k whole-book kill) unchanged — that's the real catastrophe brake.
4. After restart, verify the re-sync propagated: `strategies.visual_config.risk.daily_loss_limit` on VPS shows
   the new values, and `loss_killswitch.py` reads that same field.
5. Record the deploy date here + a note in `db.app_config` (measurement epoch marker).

**Acceptance**: no live strategy with `daily_loss_limit < 2× its per-trade stop`; over the next 5 sessions,
`daily-loss-killswitch-strat` exits drop to ~0 on single-loss days (the killswitch should only fire on genuine
multi-loss days); strategy-day locks fall materially from 20-per-5-sessions.

---

### AR-02 — Structure–config coherence: strip scalper DNA off the 9 credit-spread strategies + entry window
- **Status**: `[x]` DONE 2026-07-02, commit `18f70fd`
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2.5 hours
- **Prerequisite**: Ships with AR-01 (same template edit)
- **Targets**: the 15–30m hold bucket (−₹6,256 @ 8.3% WR vs 2h+ holds +₹2,935 @ 62.5%); afternoon entries
  (−₹9,411; hour-13 IST −₹7,964)

**Problem**: 9 strategies now fire credit spreads (4 original theta + 5 converted ex-scalpers) but still carry
scalper/momentum configs — `time_exit` 10–30m, cooldowns down to 1m, `max_trades_day` up to 20, scalper freq
class. A credit spread with an 18-minute time exit fights its own theta thesis. Separately, credit entered
after ~13:00 IST has no decay runway before the 15:25 squareoff — and afternoon is exactly where the book
bleeds.

**Files to touch**: `backend/server.py` (template, same pass as AR-01; also the `_debit_names`/`_credit_names`
startup-migration lists), `backend/strategy_runner.py` or `backend/signal_manager.py` (entry-window gate),
`backend/trade_frequency.py` (class reassignment), `docker-compose.yml` (env).

**Exact steps**:
1. Verify which time clock spreads actually honor: SENSEX Theta's median hold is 81m against a configured
   `time_exit_minutes=20` → the per-strategy value appears NOT applied to spreads. Confirm in
   `position_monitor._process_spread_position`, then neutralize/remove the misleading value for credit
   strategies so nobody later "fixes" it in the wrong direction.
2. Normalize the 9 credit strategies in the template: cooldown ≥ 15m, `max_trades_day` ≤ 8, no sub-hour time
   exit; the designed exits stay `spread-tp` (SPREAD_TP_FRAC=0.5, validated WR-32), spread SL (AR-01), and
   squareoff-1525.
3. Add an env-gated **entry window for `structure=credit_spread`: new entries 09:45–13:00 IST only**
   (`CREDIT_ENTRY_WINDOW=0945-1300`, default on). Emit a distinct skip code (`ENTRY_WINDOW`) so attribution
   can measure the gate. Existing positions manage to TP/EOD unchanged.
4. Reclass converted strategies in `trade_frequency._CLASS_CAPS` lookup (still classed scalper/momentum) so
   freq caps match their new structure.
5. Migrate the 2 worst remaining debit spreads → credit via the `_debit_names`/`_credit_names` lists:
   **NIFTY Micro-Lot Trend Follower** (0% WR, −₹1,755 since baseline) and **NIFTY Momentum Buyer** (lifetime
   −₹14,520, worst in book). Keep **NIFTY VWAP Trend Breakout** (only debit with positive lifetime, +₹1,626)
   and NIFTY HFT Quick Scalper as the TREND-gated debit probes for AR-07.

**Acceptance**: all 9+2 credit strategies share theta-coherent configs post-resync; `ENTRY_WINDOW` skips
visible in `signals`; credit entries after 13:00 IST → ~0; hold-bucket distribution shifts toward 1h+.

---

### AR-03 — BUG: equity ATR brackets never land — every position gets the dead 7.05%/10.94%
- **Status**: `[x]` DONE 2026-07-02, commit `18f70fd`
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: None (independent P1 bug — can land same day as AR-01/02)
- **Targets**: equity book −₹5,160 @ ~3% WR since baseline; 26 `time-exit-22m` exits @ 3.8% WR (now disabled)
  left signal-flips @ 7.7% WR as the de-facto exit

**Problem**: the 06-23 equity rebuild (`a94f5ab`) was supposed to replace the 7%/11% SL/TP with ATR(14)
brackets — but EVERY equity position through 07-01 carries exactly slPct 7.05 / tpPct 10.94. Those are
unreachable intraday → equity has NO functioning risk brackets; with `EQUITY_TIME_EXIT_MINUTES=0` (7abb5e1,
verified landed — zero 22m exits on 07-02) the only exits left are signal flips and squareoff. Related oddity,
same subsystem: RELIANCE Trend Rider closed `R_TARGET_HIT` at a LOSS (−₹61) 23 seconds after entry.

**Files to touch**: `backend/server.py` (fill handler that stamps `tp_sl_tsl_config`; KEY MECHANIC: there is
no `equity_trend` preset → equity silently uses the `momentum` preset — 7.05/10.94 smells like preset
percents), `backend/strategy_runner.py` (do the equity python_code v2.0 signals actually emit ATR levels —
check signal fields `initial_stop_R`/`target_R`/`exit_policy`), `backend/core/position_lifecycle.py`.

**Exact steps**:
1. Pull one 07-01 equity signal + its position doc side by side: does the signal carry ATR-based levels that
   the fill path ignores, or does the python_code not emit them at all? Fix at whichever end is broken.
2. Wire real ATR(14) levels into `tp_sl_tsl_config` at fill time: SL ≈ 1×ATR, TP ≈ 1.5–2×ATR, trailing on.
3. Root-cause the RELIANCE `R_TARGET_HIT`-at-a-loss (target below entry? stale entry price?) and fix.

**Acceptance**: new equity positions show ATR-derived brackets that vary per name/day (NOT 7.05/10.94); no
`R_TARGET_HIT` exit with negative P&L; real `stop-loss`/`take-profit` exit reasons reappear in the equity mix.

---

### AR-04 — Equity economics: clear the cost bar + 14:30 entry cutoff + wake the dead names
- **Status**: `[x]` DONE 2026-07-02, commit `b47f28d`
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: AR-03 (brackets must work before judging equity edge)
- **Targets**: ~HALF of the equity loss is transaction charges (avg win ₹13 vs ₹30+ round-trip on ₹13k
  notionals — arithmetic, not signal quality); 3 entries at 15:05 went straight into the 15:10 squareoff

**Problem**: at current sizing no equity signal can be profitable: a 0.2% favorable move on ₹13k is ₹26,
below round-trip cost. Founder rule applies: FIX, don't pause. Also: SBIN Short Seller has NEVER fired a
single trade (the book's only bearish equity leg is dead) and INFY VWAP Pullback has 0 trades since baseline —
silent strategies produce no data.

**Files to touch**: `backend/server.py` (equity capital tiers from CUR-04; entry-cutoff gate),
`backend/strategy_runner.py` (cutoff + SBIN/INFY diagnosis).

**Exact steps**:
1. Raise per-name notional so expected gross at TP clears **≥3× round-trip charges** (use the CUR-04 capital
   tiers; concentrate capital in fewer names only if sizing-up all 10 overshoots book risk).
2. Entry cutoff: **no NEW equity entries after 14:30 IST** (env-gated, distinct skip code like AR-02's).
3. Diagnose SBIN (short-side path likely blocked — CNC product can't short? signal never emitted?) and INFY
   (evaluations run but nothing fires) — fix or document why.

**Acceptance**: no equity entry after 14:30; median equity notional ≥ ₹50k (or a written tier decision);
SBIN/INFY either trade or have a root-caused reason recorded here.

**Done 2026-07-02**: added env-gated `EQUITY_ENTRY_CUTOFF=1430` with skip code `EQUITY_ENTRY_CUTOFF`; enforced a minimum ₹50k equity risk-capital tier while preserving existing higher 75k tiers; startup migration syncs top-level `required_capital` with `visual_config.risk.required_capital`. Dead-name diagnosis: 60-day real Upstox 5-minute OHLC backtest produced 0 trades for RELIANCE and SBIN; INFY produced 1 losing trade. This is signal scarcity/low edge on the current code, not an execution/router failure.

---

### AR-05 — Attribution inputs: equity regime stamping + planned_risk everywhere
- **Status**: `[x]` DONE 2026-07-02, commit `b47f28d`
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: None. **Gates AR-07 and HSI Stage 4** — the sooner this lands, the sooner the data clock runs.
- **Targets**: 41/52 attribution rows are regime UNKNOWN; RANGE = 83% WR / TREND_UP = 0% WR is the strongest
  gate we have and it is currently unactionable

**Problem**: options regime stamping only works since 07-01 (8d2be82); equity has NEVER stamped regime
(`market_regime` covers indices only; the equity fill path doesn't thread `trend_context` — known gap, memory
`project_regime_instrumentation_status`). Also `planned_risk=0` on NIFTY Micro-Lot + NIFTY Range Credit Spread
positions → their `R_multiple` attribution is broken, which mis-feeds the Hermes lesson scorer.

**Files to touch**: `backend/server.py` (equity fill path — thread `trend_context`→`regime_at_entry`),
`backend/strategy_runner.py`, wherever `planned_risk` is stamped for spread paths (grep `planned_risk`).

**Exact steps**:
1. Thread the equity signal's `trend_context` into `regime_at_entry` on the position doc at fill time.
2. Fix `planned_risk` stamping on the spread paths that miss it (Micro-Lot debit + Range credit evidence).
3. Do NOT backfill old rows — attribution is cumulative (HSI gotcha: `attribution_rollup(since=date)`).

**Acceptance**: new trades <10% UNKNOWN regime across all asset types; every new position has
`planned_risk > 0`.

**Done 2026-07-02**: equity signals now store `trend_context`, a regime-like `regime_snapshot`, and `regime`; the reservation/activation path writes `regime_at_entry` onto new equity/single-leg position docs. New reserved/activated positions also stamp `sl_price`, `tp_price`, and `planned_risk` from the final risk config when a stop exists. Spread lifecycle already stamps `planned_risk`; old zero/UNKNOWN rows are intentionally not backfilled.

---

### AR-06 — BANKNIFTY theta expiry mismatch (weeklies died Nov 2024)
- **Status**: `[x]` DONE 2026-07-02, commit `b47f28d`
- **Tier**: 1–2
- **Session size**: ~1 hour (investigation + config)
- **Prerequisite**: None
- **Targets**: BANKNIFTY Theta Credit Spread's inverted asymmetry — 67% WR but avgLoss −₹1,601 vs avgWin
  +₹366, including the book's single worst stop (−₹2,014)

**Problem**: NSE discontinued BANKNIFTY weekly options in Nov 2024 — it only has monthlies now, while
NIFTY (NSE) and SENSEX (BSE) kept weeklies. An intraday theta-harvest strategy on a monthly option sells
slow-decay premium while keeping fast-market risk. The data is consistent: SENSEX Theta (weekly) +₹2,664 @
80% WR vs BANKNIFTY Theta bleeding. The selector uses `expiry_offset: 0` = nearest available expiry.

**Exact steps**:
1. Verify from `orders`/`strategy_positions` trading symbols what expiry BANKNIFTY spreads actually trade
   (the verbose symbol carries the date).
2. If monthly: either (a) restrict BANKNIFTY theta entries to **expiry week only** (env-gated), or (b)
   re-point that strategy's premium-selling to a weekly underlying (NIFTY/SENSEX) and leave BANKNIFTY to the
   directional/breakout book. Prefer (a) first — smaller change, keeps the underlying diversity.

**Acceptance**: expiry evidence documented here; one of the two configs applied; BANKNIFTY theta's win/loss
asymmetry normalizes over the following 2 weeks (avgLoss no longer 4× avgWin).

**Done 2026-07-02**: live DB has `BANKNIFTY Theta Credit Spread` configured as paused/schedule-paused credit spread with no recent position/order expiry rows to inspect; config fix applied anyway as the smaller-risk option. Added env-gated `BANKNIFTY_THETA_EXPIRY_WEEK_ONLY=true`: after option resolution, BANKNIFTY theta credit-spread entries are blocked unless the resolved contract expiry is within 6 IST calendar days. Startup migration annotates the DB strategy with `expiry_policy=expiry_week_only`.

---

### AR-07 — ⛔ Portfolio layer: regime gates + two-sided selling + net-delta cap (DATA-GATED)
- **Status**: `[ ]` ⛔ blocked until AR-05 lands + ~2–3 weeks of stamped attribution (same clock as HSI-41..44
  — reuse that OOS validation, don't fork it)
- **Tier**: 2–3
- **Session size**: ~1 day, split

**Scope when unblocked**:
1. Enforce regime entry gates from *measured* lessons (the Hermes lesson store already scores
   regime=RANGE/TREND_UP claims): credit spreads require RANGE/weak-ADX; the two debit probes require TREND.
2. In RANGE, sell BOTH sides (put spread + call spread) instead of the bias-picked single side — a ranging
   market is the iron-condor case; one-sided selling wastes half the edge.
3. Book-level net-delta cap via the existing Greeks proxy (44 BULLISH vs 8 BEARISH entries = one big long
   bet; 06-29/30 was the cascade this causes). WR-53's per-underlying cap stays; this is the book-level lid.
4. Optional: VIX-scaled spread lots (shrink size continuously as vol rises — arXiv 2508.16598 pattern), using
   the VIX data already collected since 06-16.

---

### AR-08 — Measurement checkpoint (~2026-07-16)
- **Status**: `[ ]`
- **Tier**: 1 (any model — read-only queries + doc update)
- **Session size**: ~1 hour
- **Prerequisite**: AR-01..03 deployed ≥ 8 trading sessions

**Exact steps**: re-run the 07-02 analysis queries (memory `strategy-book-analysis-07-02` documents them) and
compare against this baseline: killswitch exits (was 28 / −₹21,177), exit-reason economics, morning-vs-afternoon
P&L split, hold-bucket distribution, equity gross-vs-charges, per-strategy expectancy. Feed keep/kill through
the scorecard verdict (WR-41 KEEP/WATCH/KILL, respects the thin-sample floor). Update all AR statuses; re-open
WR-33 only if attribution now shows winners being cut early.

**Acceptance**: a written before/after table in this section; explicit KEEP/WATCH/KILL verdict per strategy.

**Early checkpoint 2026-07-03 02:06 IST (NOT FINAL — prerequisite not met):** AR-01..06 have not yet had 8
trading sessions; July 2 data still includes old equity deadline/static-bracket positions opened before the
latest `EQUITY_TIME_EXIT_MINUTES` / ATR warmup deploys. Read this as a smoke-check, not a keep/kill decision.

| Metric | 07-02 baseline | Current query | Early read |
|---|---:|---:|---|
| Closed attributed trades since 2026-06-25 | 118 in original analysis window | 82 in current `trade_attribution` window | Different compiler/window; do not compare counts directly |
| Net P&L since 2026-06-25 | about -₹8.2k | -₹10,439 | Still negative; not enough post-fix data |
| July 2 attributed P&L | n/a | -₹653 across 30 trades | Much closer to flat than the pre-fix bleed, but mixed old/new behavior |
| `daily-loss-killswitch-strat` since 2026-06-25 | 28 / -₹21,177 | 18 / -₹12,145 | Improved vs baseline but still the largest loss bucket |
| `daily-loss-killswitch-strat` on/after 2026-07-02 | n/a | 5 / -₹2,877 | Watch next sessions after AR-01 geometry fully propagates |
| `time-exit-22m` | 26 / bad equity bucket | 17 / -₹1,271 | Should decay to 0 after latest equity deadline fix |
| `time-exit-deadline` | should vanish for equity | 8 / -₹658 | These are old pre-deadline-fix positions; recheck after next market session |
| `spread-tp` | +₹4,239 @ 100% WR | +₹3,502 @ 100% WR | Still the cleanest winning exit |
| `intraday-squareoff-1525` | +₹4,139 @ 62.5% WR | +₹3,340 @ 61.5% WR | EOD/theta holds remain productive |
| Time of day | afternoon was worst | afternoon -₹9,361; morning +₹1,877 | Supports keeping afternoon entry gates |
| Hold bucket | 2h+ winners strongest | 2h+ +₹3,416; 15-30m -₹7,046 | Supports letting theta trades mature |
| Structure | credit/range strongest | credit spreads -₹364, debit spreads -₹6,953, single-leg -₹3,123 | Credit spreads still least bad; debit probes need regime proof |
| Regime | RANGE best | RANGE +₹2,205; UNKNOWN -₹11,574 | AR-05 regime stamping is important; UNKNOWN remains the loss bucket |
| Equity book | cost/bracket leak | 61 closed equity positions, net -₹5,161, 3.3% WR | Old equity behavior still dominates; recheck after ATR warmup fix has live trades |

**Early KEEP/WATCH/KILL:** no hard KILL yet because every strategy is below the WR-41 thin-sample floor
(`n < 15`). `SENSEX Theta Credit Spread` remains the best WATCH/KEEP candidate (+₹2,305, 85.7% WR, n=7).
Worst WATCH/rework names by current P&L: `BANKNIFTY Volatility Breakout` (-₹2,404, n=3),
`SENSEX Swing RSI Pullback` (-₹2,233, n=3), `NIFTY Quick EMA Scalper` (-₹2,129, n=3),
and `KOTAKBANK RSI Rebound` (-₹880, n=13). Do not reopen WR-33 yet: the winners are still mainly
`spread-tp` / EOD theta holds, not winners being cut early.

---

## PRIORITY 0 — Win-Rate & Expectancy Campaign (2026-06-22)

**Source**: Full session 2026-06-22. Day was −₹24,044 realized (24% win rate, avg loss −696 = 3.4× avg win +202).
Root-caused to BUGS + wrong metric, NOT bad strategy logic. Research-grounded (see Sources). **Core thesis:
win rate is the wrong target — optimize EXPECTANCY + Sharpe, weight the book toward measured-positive edge
(equity momentum A/B, theta-selling 60–75%) and away from measured-negative (ATM option buying, grade F).**

Loss attribution (today): equity phantom-quote fake stop-losses −12,975 (54%) · spread stale-exit −6,266 (26%) ·
spreads sold into trends ~−8k · single-leg buyers ~−3k (buckets overlap). ~80% was bug-driven, not trading.

Key facts established this session:
- Credit spreads are correctly built (0.30Δ short, 2-wide, 50% TP) → were bug-destroyed to 11–18% win (should be ~70%).
- Momentum buyers at 22–40% win is CORRECT for trend-following (research: 35–45%); fix EXPECTANCY not win rate.
- Signal plumbing is HEALTHY: no duplicates, symbol-group dup-guard works (4 blocks), no stale-signal starvation,
  cooldown not biting. Only inefficiency = spread over-emission (52–63 sig → 11 ord; ADX gate now reduces it).
- NIFTY EMA Scalper = 60% win / −5,695 → proof win rate is a vanity metric (its stop lets losers run).

**Sequencing**: Phase 1 (validate clean) BLOCKS tuning — don't pile changes that muddy measurement.
Critical path: WR-1x (validate) → WR-21 + WR-22 (EMA stop + ITM strikes) → WR-31 (spread delta) →
WR-42 (weight to proven edge) → WR-51/52 (risk sizing + kill-switch).

Sources: apexvol.com/strategies/credit-spread · robottraders.io/blog/trend-following-vs-mean-reversion ·
journalplus.co/learn/guides/win-rate-vs-risk-reward · einvestingforbeginners.com/theta-gang-strategies

---

### Phase 0 — Done & deployed this session (baseline)
- `[x]` **WR-00a** Spread stale-exit bug fixed (3-layer: refresh last_fresh_tick_at + exclude spreads from staleness predicate + ignore stale reason in close path + debit_spread close routing) · `bc73d37` · 2026-06-22
- `[x]` **WR-00b** ADX(14) regime gate on 3 Theta credit spreads (stand down ADX≥25) · DB python_code v1.1 · 2026-06-22
- `[x]` **WR-00c** Feed-aware volume gate (range-expansion fallback) on 6 index-option strategies · DB · 2026-06-22
- `[x]` **WR-00d** Equity phantom-quote guards: runner outlier-candle entry skip + monitor/guardian >35%-from-entry LTP reject · `ec09c7a` · 2026-06-22
- `[x]` **WR-00e** Mongo healthcheck flap permanently fixed (timeout 5s→20s + start_period 60s) · `cbc4944` · 2026-06-22
- `[x]` **WR-00f** Verified stops/trailing are correctly configured and DO fire (clean trades booked sane P&L)

---

### Phase 1 — Validate on clean data (P0 — BLOCKS all tuning below)
- `[x]` **WR-11** Phantom-equity validation clean on 2026-06-23 (`a94f5ab` VPS): backend log grep for `PHANTOM_CANDLE` / `PHANTOM_LTP_REJECTED` returned no hits, and today's paper fills had no single-leg/equity loss over ₹1,000. No 1-second half-entry equity stop-loss pattern observed.
- `[x]` **WR-12** VALIDATED 2026-06-24 session: 9 credit-spread closes, +₹1,646 net; **2 `spread-tp` closes (both wins +683/+883)** prove the 50%-profit close fires. **Zero fast churn** — only 2 `strategy-sell-signal` flip exits and NONE under 5 min (both landed at ~20.3 min, right at the `1ed9def` 1200s debounce boundary). The residual boundary flips are now STRUCTURALLY eliminated by `1f03c5d` (2026-06-24): spreads ignore reverse signals entirely and exit only on TP/SL/time/squareoff. Closed.
- `[x]` **WR-13** VALIDATED 2026-06-24: the 3 theta credit spreads fired 2/1/3 signals, **0 filtered**, on a low-ADX/range midday → correctly ALLOWED (theta wants range) and net +₹1,646. ADX warmup hotfix (`len>=60`, skip `i<42`/`adx<=0`/`adx>=25`) deployed and producing sane behavior (no spurious blocks on a range day, no over-firing). Full high-ADX *block* proof still wants a trending day, but the gate is behaving correctly. Closed.
- `[x]` **WR-14** ASSESSED 2026-06-24, no tuning warranted: filter histogram healthy — `LOSS_STREAK_BLOCKED` 11 (mostly LT counter-trend buys, now also gated), `STRATEGY_SIGNAL_SPAM` 5, `SYMBOL_GROUP_ACTIVE_POSITION_EXISTS` 2; 42 processed vs 18 filtered. No threshold over/under-firing. Defaults left as-is; revisit only if a future session shows pathological filtering.

---

### Phase 2 — Strategy logic fixes (P1 — no market wait needed)
- `[x]` **WR-21** NIFTY EMA Scalper (2f7ce983): stop is NOT broken — verified 2026-06-22. The −5,695 lifetime / "60% win but big loss" was ONE pre-fix oversized trade (qty=650 = 10 lots, 2026-06-12, −5,350) that lost only −6.3%/unit × 10× size. The 1-lot trade lost exactly −5.66% = the 5.5% stop firing correctly. `max_lot=1` now prevents recurrence. No stop fix needed; contamination tracked in WR-63. Real lever = WR-22 (ATM→ITM).
- `[x]` **WR-22** ATM→ITM strikes for directional buyers — DEPLOYED `15d3c73` 2026-06-22. ROOT CAUSE: `server.py:16141` derived `strike_rule = OTM1-or-ATM` — could NEVER produce ITM (the unlanded "Fix 1"); resolver already supported ITM1. Fix: strike_mode containing "ITM"→strike_rule ITM1. Applied ITM1_BUY to 4 holders (time_exit≥15m: SENSEX Swing, NIFTY Micro-Lot, BANKNIFTY HFT, NIFTY VWAP Trend); 2 scalpers (≤10m) kept ATM for bid-ask liquidity. DEBIT_SPREAD_LONG_DELTA 0.50→0.65 (ITM long leg) for the 3 debit-spread buyers. **Validate at open**: confirm ITM1 strikes resolve (check selected_strike_mode / last_traded_symbol) and win rate lifts.
- `[x]` **WR-23** No churn risk — verified 2026-06-22. All 10 equity strategies are clean state machines (enter at signal, exit at opposite signal/stop); the re-entry patch referenced in memory is NOT in current migrated code. No change. *Opposite mild risk noted: persistent trends trigger only once (under-participation), not a loss source — possible later follow-up.*
- `[x]` **WR-24** Resolved via ADX gate (WR-00b) — over-emission already cut, and the spam-filter + symbol-group guard absorb the rest with no losses. The "emit every candle to beat the staleness guard" workaround is now moot (spread stale-exit fixed in WR-00a). No separate change needed.

---

### Phase 3 — Economics / strike tuning (P1 — AFTER Phase 1 confirms)
- `[x]` **WR-31** DONE 2026-07-01 (`9521e14`): `CREDIT_SPREAD_SHORT_DELTA` default 0.30→0.20 in docker-compose.yml (verified 0.20 in container). POP ~70%→80%, less premium collected. Reversible via env. NOTE: entangles measurement with the same-day rung-2 credit migration (both push the book toward theta) — read the two effects together.
- `[x]` **WR-32** 50%-profit close (`SPREAD_TP_FRAC=0.5`) CONFIRMED firing: 2026-06-23 had 4 `spread-tp` closes (+₹2,086, ~32m); 2026-06-24 had 2 more (+₹683/+₹883). No signal-churn contamination after `1f03c5d`. Closed.
- `[ ]` **WR-33** Let momentum winners run: raise `target_R`, enable trailing — fixes expectancy. **DEFERRED 2026-07-01 (evidence-based):** the premise (winners cut tight) came from the 06-20 single-leg-buyer-heavy book. The CURRENT book's winners are spreads held to theta-TP (`spread-tp`, tastytrade-optimal 50%) / EOD square-off — i.e. NOT cut tight. Trailing is already enabled by default (`trailing_sl_enabled=True` suppresses the fixed TP). Raising `target_R`/loosening trail now = low-evidence change that adds give-back risk AND muddies the rung-1 (equity exit) + rung-2 (migration) measurement. RE-OPEN when attribution shows a real "winner cut early" pattern (e.g. a cluster of `take-profit`/`trailing-sl` exits closing well below `target_R` on trending days).

---

### Phase 4 — Portfolio construction & measurement (P1 — the real strategy)
- `[x]` **WR-41** DONE 2026-06-24 (`874521e`) — `grade()` was already pure expectancy/Sharpe/PF (win rate unused); added explicit `keep_kill_verdict` (KEEP/WATCH/KILL + reason) and `summarize_verdicts` roll-up on `GET /ops/risk-scorecard`. Never KILLs on a thin sample (`SCORECARD_KILL_MIN_TRADES`, default 15) per the don't-kill-on-1-2-days rule. 5 new pure tests. Auto-pause action is WR-54.
- `[x]` **WR-42** FOLDED into the Alpha Repair campaign 2026-07-02 (dedupe — not independently actionable): the concrete weighting moves are AR-02 step 5 (last debit→credit conversions), AR-07 (regime-gated sizing) and AR-08 (KEEP/WATCH/KILL verdicts). *Partial progress 06-30 (`873117a`/FIX-03).*
- `[x]` **WR-43** FOLDED into AR-07 2026-07-02 (dedupe): archetype decorrelation is delivered concretely as two-sided selling in RANGE + book net-delta cap + the TREND-gated debit probes. WR-45 (correlation matrix) stays open as the measurement tool.
- `[x]` **WR-44** FOLDED 2026-07-02 (dedupe): the OOS ratchet IS HSI Stage 4 (HSI-41..44) + the AR-08 checkpoint verdicts — one judge, built once. (HSB-12..16 superseded the same way, see Phase E note there.)
- `[ ]` **WR-45** Track per-strategy correlation matrix to confirm edges are genuinely independent.

---

### Phase 5 — Risk management (P1 — the actual product)
- `[ ]` **WR-51** Size each bet by risk (fraction-of-Kelly / fixed-fractional), not fixed lots. **Sequenced 2026-07-02: do AFTER AR-08 — changing sizing inside the AR-01/02 measurement window would make the checkpoint unreadable.**
- `[x]` **WR-52** DONE 2026-06-24 (`874521e`) — `core/loss_killswitch.py` (sibling of profit_lock), actively enforced from the monitor tick (fires even with no new order, unlike the entry-only preflight guard). Per-strategy: day P&L ≤ −`daily_loss_limit` → square off + stand down for the IST day (`day_loss_locked`, read by the signal_manager gate). Whole-book: aggregate ≤ −`PORTFOLIO_DAILY_LOSS_LIMIT` (env, default ₹20k) → square off entire book + stand down. ⚠️ Per-strategy floors currently inherit the small momentum-preset value (₹650 equity / ₹650–1200 options) — may want raising vs the new equity sizing.
- `[x]` **WR-53** DONE 2026-06-30 (`873117a`): directional-exposure cap `MAX_DIRECTIONAL_EXPOSURE_PER_UNDERLYING` (default 3) in `strategy_runner.py` — blocks a new entry when N strategies already hold the same BULLISH/BEARISH bias on one underlying. Per-position bias via `_position_exposure_bias` (equity long / credit PE=bullish CE=bearish / debit CE=bullish PE=bearish / single-leg by option_type+side). Per-underlying so equity (1 strat/stock) is never throttled.
- `[ ]` **WR-54** Auto-pause a strategy on max-drawdown breach. **⛔ GATED on AR-01 + AR-08 (2026-07-02): the killswitch analysis showed auto-locks tighter than designed risk destroyed −₹21k of edge in 5 sessions. Any drawdown auto-pause must respect the AR-01 invariant (threshold ≥ 2–3 designed per-trade losses) and only ships after AR-08 proves the new geometry — otherwise this recreates the exact bug class AR-01 removes.**

---

### Phase 6 — Operational / data hygiene (P2)
- `[x]` **WR-61** Root-cause the equity phantom-quote SOURCE (why the open emits 1/2x/2x ticks) — added source diagnostics on both equity phantom guards plus `/api/ops/equity-quote-diagnostics` for source/instrument/deviation review.
- `[x]` **WR-62** Add index on `strategies.id` (currently COLLSCAN every eval; minor) — startup now creates a non-unique `strategies.id` index.
- `[x]` **WR-63** Clean contaminated lifetime stats (pre-2026-06-18 oversizing/phantom era still pollutes win/loss counts) — `/api/ops/risk-scorecard` now defaults to clean stats since `2026-06-17T18:30:00+00:00`; pass `clean=false` for lifetime.
- `[x]` **WR-64** Pre-open readiness check (token valid + feed live + strategies live) so mornings don't silently fail — added `/api/ops/pre-open-readiness` with token/feed/strategy/position/order/kill-switch/phantom checks.

---

### Phase 7 — Roadmap-aligned, bigger build (P2/P3)
- `[ ]` **WR-71** Strategy backtesting on real options-chain history (blocked by Upstox expired-option API — needs a data source).
- `[x]` **WR-72** FOLDED into HSI-41..44 2026-07-02 (dedupe): the walk-forward/OOS harness is HSI Stage 4's `core/hermes_validator.py` (+ HSB-11's historical_chains data audit as precursor). One OOS judge for the whole platform — don't build a parallel one.
- `[ ]` **WR-73** Enable `CORE_ENGINE_LIVE_ENABLED` on 2–3 proven strategies (founder gate; roadmap Phase 1). **Prerequisites added 2026-07-02: AR-08 checkpoint green (killswitch leak closed, equity brackets real) + OOS verdicts from HSI Stage 4.**
- `[x]` **WR-74** DONE 2026-07-03 (`d1553fc`) — improved the existing `/analytics` page with decision-grade panels: KEEP/WATCH/KILL counts, clean-window marker, best/worst expectancy, Hermes OOS judge status, and a verdict column with per-strategy reasons. Verified with `$env:CI='false'; npm run build`.

---

## PRIORITY 0 — Profitability Campaign (2026-06-20) — ✅ SUPERSEDED / FOLDED INTO WIN-RATE CAMPAIGN

> **Reconciliation 2026-06-24:** This campaign's code all shipped (`6d598b3`) and has been live through ~4 sessions since. P-EX01 (SL floor), P-EX02 (stale-quote exit), P-EX03 (trailing) are validated and additionally hardened by later work — the `strategy-sell-signal` over-loss bucket was attacked again on 06-24 (`1f03c5d` spreads ignore reverse signal). P-EX04 (rebalance to spreads) is DONE — the book now runs credit+debit spreads + equity. P-EX05 accounts were purged. Treat the items below as DONE; the live exit-economics work continues under the Win-Rate campaign (WR-31/33). Statuses updated to `[x]` below.



**Source**: Full strategy audit 2026-06-20. Evidence: `trade_fills` realized P&L = −₹40,802 over 106 closes;
clean epoch (post 06-18) = 9 trades / −₹2,644 / 11% win. Diagnosis: entry logic is NOT the main problem —
**exit logic + buy-only theta structure are**. Losses average 3–5× wins (avgWin +239 vs avgLoss −688 to −1,066),
so even a 40% win rate bleeds.

Key evidence (exit-reason breakdown of realized P&L):
| total P&L | trades | avg | exit reason |
|---|---|---|---|
| −19,959 | 29 | −688 | stop-loss (works ~7%) |
| −18,117 | 17 | −1,066 | **strategy-sell-signal (bypasses SL)** |
| −6,814 | 21 | −324 | time-exit-22m |
| −4,908 | 1 | −4,908 | **eod-square-off (SL never fired → staleness gap)** |
| +4,771 | 20 | +239 | take-profit (winners cut tiny) |
| +4,706 | 8 | +588 | trailing-sl (best exit we have) |

Sequencing: P-EX01 + P-EX02 are real bugs (≈ −₹23k of the −₹40k) and come first. P-EX03/04 are strategy-design
changes. P-EX05 is cleanup. After all five: run a clean paper-forward window → rank → keep only the 2–3 survivors.

---

### TASK-P-EX01 — Make the hard stop-loss an un-bypassable floor
- **Status**: `[x]` code shipped `6d598b3` (SL floor) · validated live over subsequent sessions; further hardened 06-24 (`1f03c5d`)
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: None
- **Targets**: the −₹18,117 `strategy-sell-signal` bucket (avg −1,066)

**Problem**: A strategy's own python_code can emit an opposite SELL that closes the whole position at market,
regardless of the configured 7% SL. The momentum indicator flips only *after* the option premium has already
collapsed, so `strategy-sell-signal` exits realize ~1.5× the loss the SL would have. The SL check in
`backend/core/position_lifecycle.py:284` (`exit_reason`) is correct and fires at a clean ~7% (−688 avg) — but
the signal-driven exit path in the strategy/signal evaluation loop never consults it.

**Files to touch**: `backend/core/position_lifecycle.py`, `backend/signal_manager.py` / `backend/strategy_runner.py`
(whichever path turns a strategy SELL into a position close), `backend/position_guardian.py`.

**Exact steps**:
1. Trace where `strategy-sell-signal` exits originate (grep `strategy-sell-signal`).
2. Before honoring a signal-driven exit on a LONG option, compute the SL price via `position_risk_prices`.
   If current LTP is already at/through the SL, tag the exit `stop-loss` (not the signal) — behaviour is the same
   but ensures the SL is the *first* gate and the guardian's 5s loop catches it before the slower signal path.
3. Confirm the guardian (5s) evaluates `exit_reason()` SL independently every tick, so no position can sit past
   its SL waiting for a signal flip.

**Acceptance**: no future CLOSE fill has `exit_reason="strategy-sell-signal"` with a loss larger than the
configured `stop_loss_pct` would allow.

---

### TASK-P-EX02 — Close the staleness → EOD square-off gap
- **Status**: `[x]` code shipped `6d598b3` (stale-quote exit) · validated; equity feed now real (Equity Campaign) so the staleness gap is closed at source
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: None
- **Targets**: the −₹4,908 single `eod-square-off` trade + staleness-driven losers

**Problem**: One position bled to the 15:20 square-off for −4,908 because its SL never evaluated intraday — the
monitor had no fresh LTP (WS_CACHE / feed-staleness bug class, see memory `project_trade_drought`,
`project_equity_ws_cache_phantom`). When LTP is stale the SL check is silently skipped and the position rides
to EOD.

**Files to touch**: `backend/position_monitor.py`, `backend/position_guardian.py`, `backend/core/quote_service.py`.

**Exact steps**:
1. Identify where the monitor/guardian skips an open position when LTP is stale/unavailable.
2. Add a protective rule: if an OPEN option has no fresh LTP for > N seconds (config-gated), attempt REST/last-good
   fallback; if still none, force a protective exit rather than letting it ride to square-off.
3. Ensure this does NOT reintroduce the WS_CACHE phantom-price bug (no pricing exits off unsubscribed NSE_EQ keys).

**Acceptance**: no CLOSE fill reaches `eod-square-off` with a loss exceeding the configured SL because of a
stale-quote skip.

---

### TASK-P-EX03 — Stop capping winners (let trailing run)
- **Status**: `[x]` code shipped `6d598b3` (trailing TP suppression) · validated; trailing-sl is the dominant winning exit. Further tuning tracked as WR-33.
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~1.5 hours
- **Prerequisite**: P-EX01
- **Targets**: realized TP only captures +239 vs configured 11%; trailing-sl is the best exit (+588)

**Problem**: Winners are cut tiny by a fixed take-profit while losers run. Trailing-sl already outperforms fixed
TP. The R:R is inverted in *realized* terms even though configured TP > SL.

**Exact steps**:
1. Widen `trail_trigger_pct` / loosen `trail_step_pct` so winners trail instead of hitting the fixed TP early.
2. Consider replacing the fixed `take_profit_pct` with a trailing-only exit above the trigger.
3. Backtest/paper-forward the change before promoting; do not raise risk per trade (keep 1-lot cap).

**Acceptance**: realized avg-win rises toward configured target; trailing-sl share of profitable exits increases.

---

### TASK-P-EX04 — Rebalance from ATM-buying toward theta-positive spreads
- **Status**: `[x]` DONE — book now runs 4 credit spreads + multiple debit spreads + equity; buyers/sellers mix achieved and trading live (paper).
  - 4 credit-spread strategies confirmed armed (`schedule_paused=true` → auto-live Mon 9 AM): NIFTY Theta, NIFTY Range, BANKNIFTY Theta, SENSEX Theta. `CREDIT_SPREADS_ENABLED=true` verified on VPS.
  - Worst ATM buyer (BANKNIFTY ATM Breakout Buyer, −15,181) converted `single_leg`→`debit_spread`. NIFTY ATM Momentum Buyer was already `debit_spread`. `DEBIT_SPREADS_ENABLED` defaults true.
  - Monday-armed set is now balanced: 4 credit spreads + 2 debit spreads + 6 directional single-legs.
  - REMAINING (optional): trim the two heavy single-leg losers still armed — BANKNIFTY HFT Momentum Scalper (−6,275), NIFTY Quick EMA Scalper (−5,741) — before the ranking window, or let them re-test under the new exit logic and rank them out.
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: None
- **Targets**: every large loser is an ATM option *buyer* (BANKNIFTY avgL −2,234, NIFTY −1,586)

**Problem**: ATM buying = max theta, needs a big directional move every trade; bleeds on range/quiet days. The 4
credit-spread (theta-positive) strategies are all paused — i.e. the book has no edge on exactly the days buyers
can't win. See memory `project_credit_spread_diversification`.

**Exact steps**:
1. Re-activate the 4 paused credit-spread strategies (NIFTY Theta, NIFTY Range, BANKNIFTY Theta, SENSEX Theta)
   for paper, verify they fire on biased candles (beat the staleness guard).
2. Convert 1–2 worst ATM buyers to `debit_spread` (structure support exists, TASK-048) to cut theta cost.
3. Track buyers vs sellers P&L split over the clean window.

**Acceptance**: live(paper) set is a balanced mix of buyers + theta-positive sellers; spreads firing verified.

---

### TASK-P-EX05 — Dedupe strategies (DONE — accounts) + canonicalize
- **Status**: `[x]` accounts purged 2026-06-20 (76 dup drafts deleted with 4 non-owner users); single owner account with ~24 canonical strategies remains.
- **Tier**: 1 (Haiku) for remaining cleanup
- **Session size**: ~30 min
- **Prerequisite**: None

**Done**: deleted 4 non-owner accounts (drgaurav, agent, test+1, test+2) and their 76 duplicate draft strategies,
1 paper_wallet, 14 ai_chats. Only owner `soni121.gs@gmail.com` remains (24 strategies, all real).

**Remaining**: within the owner's 24, collapse any leftover duplicate/draft definitions so the strategy list is a
clean canonical set ready for ranking. Then run the clean paper-forward window → rank → keep 2–3 survivors.

---

## PRIORITY 0 — Equity Phase Campaign (2026-06-21) — ✅ LARGELY DONE (live on real feed)

> **Reconciliation 2026-06-24:** Equity is LIVE on the real Upstox V3 feed (paper), 10 NSE_EQ strategies trading. The phantom/mock-price bug class is FIXED (real V3 signal candles `372751b`/`7e57536`, real REST exit pricing). So **EQ-01 (feed), EQ-02 (exec sanity), EQ-03 (sizing) are DONE**, and **EQ-06 (re-enable) is DONE**. Equity sizing was further tuned 06-24 (capital tiers, see CUR-04) and equity entries now have a counter-trend gate (CUR-03). Remaining: EQ-04 (rank the universe on real OHLC — operational, do on a clean window) and EQ-05 (auto-reactivation root-cause — hygiene). Statuses updated below.



**Founder decision 2026-06-21**: start the equity phase. Footprint target = the **full equity universe**
(the 12 NSE_EQ names mapped in `server.py:7316` — RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, SBIN, AXISBANK,
BHARTIARTL, KOTAKBANK, ITC, LT, MARUTI). Token will be connected on a trading day.

**Why now**: the only A/B-graded strategies in the whole book are equity (LT, AXISBANK) — see memory
`project_options_backtester_analytics`. They were paused twice (06-16, 06-18) not for lack of edge but for a
**phantom-price bug**: equity NSE_EQ keys are never subscribed to the Upstox V3 feed, so the WS cache returns
garbage LTP and exits book impossible fills (TCS exit ₹2189 vs real ~₹4200, etc.). Both fixes to date were
**band-aids** (`_EQ| → skip WS cache → fall back to a ±0.5% mock price`), not a real feed. See memory
`project_equity_ws_cache_phantom_2026_06_16` and `project_equity_exit_005_bug`.

**One-sentence thesis**: equity has the best measured edge but no real price feed — build the feed first, then
everything downstream (exec, sizing, backtest, re-enable) becomes safe.

Sequencing is strict dependency order: EQ-01 (feed) gates EQ-02/EQ-03; EQ-04 needs a connected token;
EQ-05 must land before EQ-06 (don't re-enable until we know why they auto-reactivated). After all six:
run a clean paper-forward window on the equity universe → rank → promote survivors alongside the options book.

Note: equity is officially roadmap Phase 3 (CLAUDE.md §10) — this campaign is the founder pulling it forward
because the edge evidence is already here. Live equity stays `CORE_ENGINE_LIVE_ENABLED=false` gated like options.

---

### TASK-EQ-01 — Real NSE_EQ price feed (kill the phantom at the source)
- **Status**: `[x]` DONE — equity trades on the real Upstox V3 feed; signal candles `372751b`/`7e57536`, exits priced via REST. Validated (e.g. RELIANCE real ₹1335). Phantom/mock band-aids removed.
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~3 hours
- **Prerequisite**: None (pure wiring — buildable with no token, zero trading risk)
- **Targets**: the entire equity phantom-loss bug class (06-16 −₹2,730, 06-18 inverted exits)

**Problem**: equity instrument keys (`NSE_EQ|<ISIN>`) are never subscribed to the Upstox V3 WS feed. The
startup re-subscribe (`_subscribe_open_position_tokens_on_startup`, `server.py:16381`) and the entry-time
`start_market_data_ws` calls cover options/index keys but equity positions ride on a stale/garbage WS cache.
The two existing fixes (`_EQ| → skip WS cache`) only stop reading the garbage — they fall through to a **mock**
SYMBOL_LTP (±0.5% of base), which is still not a real price.

**Files to touch**: `backend/brokers/upstox_gateway.py` (confirm V3 sub accepts NSE_EQ), `backend/server.py`
(entry subscribe path + `_subscribe_open_position_tokens_on_startup`), `backend/position_monitor.py` +
`backend/position_guardian.py` (remove the `_EQ|` WS-cache band-aid ONCE the real feed is confirmed live).

**Exact steps**:
1. Verify the V3 feed accepts `NSE_EQ|<ISIN>` subscriptions and returns real LTP (capture a live frame, same
   way `project_trade_drought` proved the index feed — watch for the `mode="full"` vs `"full_d5"` trap).
2. Subscribe equity tokens at position entry AND on startup re-subscribe, exactly like option tokens.
3. Only after a real equity LTP is flowing, REMOVE the `is_cash_equity = "_EQ|" in ikey` skip in BOTH
   `position_monitor._resolve_ltp` and `position_guardian._resolve_ltp_guardian` so equity prices off the
   real feed, not the mock. Keep a staleness guard (no fresh tick → protective exit, never a garbage price).
4. Confirm paper-fill entry pricing (`_get_paper_ltp`) and exit pricing both resolve to the real equity LTP.

**Acceptance**: a live equity position shows `ltp_source=WS_CACHE` (or a new real source) with a price within
~±2% of the true market price; no exit fills at impossible prices; capture proof during a trading session.

---

### TASK-EQ-02 — Equity execution sanity (fills, exits, exit-reasons)
- **Status**: `[x]` DONE — real REST exit pricing replaced the ₹0.05 / mock fallback; entry and exit price sources match. (Whipsaw churn on BHARTIARTL addressed separately via cooldown, CUR-06.)
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2.5 hours
- **Prerequisite**: EQ-01
- **Targets**: the inverted-exit / 2–4s churn pattern (06-18) once it's on real prices

**Problem**: with garbage prices gone, the equity exit path still needs an end-to-end audit — the ₹0.05
nominal-fill bug (`project_equity_exit_005_bug`) lived in the equity exit branch, and exit-reason inversion
(`take-profit` exits losing) came from deciding on one price and filling at another.

**Files to touch**: `backend/server.py` (equity SL/TP exit branch ~`server.py:7176`), `backend/core/paper_broker.py`,
`backend/position_lifecycle.py`.

**Exact steps**:
1. Trace the equity exit branch; confirm the price used to DECIDE the exit == the price used to FILL it.
2. Remove the ₹0.05 nominal fallback for equity exits; require a real LTP (or protective-exit, per EQ-01 §3).
3. Verify exit reasons are consistent (a `take-profit` exit must be a gain at fill, a `stop-loss` a loss).

**Acceptance**: no equity CLOSE fill with an inverted reason; no ₹0.05 / nominal-price fills; entry vs exit
price sources match.

---

### TASK-EQ-03 — Equity sizing model (shares, not option lots)
- **Status**: `[x]` DONE — `risk_manager.py` sizes equity by `required_capital / price` (shares, lot_size=1), Greeks/delta cap bypassed for cash equity. Per-strategy capital tiers set 06-24 (CUR-04).
- **Tier**: 2 (Sonnet / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: EQ-01
- **Targets**: correct notional/qty for cash equity

**Problem**: cash equity trades in **shares** (lot size 1), not option lots. The risk manager, position sizing,
and Greeks/delta proxy are all options-shaped. `market_domains.get_lot_size` already returns 1 for equity (good),
but the sizing + risk path must size by ₹ notional / capital-% on shares, and the delta/Greeks cap must be
bypassed for cash equity (no Greeks on a stock).

**Files to touch**: `backend/core/risk_manager.py`, `backend/core/market_domains.py` (confirm equity domain),
position-sizing in `backend/server.py` / `backend/strategy_runner.py`.

**Exact steps**:
1. Confirm equity resolves to lot_size=1 everywhere (no hardcoded option lot leakage — already fixed in the
   equity backtester `backtest_engine.py`).
2. Size equity positions by capital-% / ₹ notional, not by lots.
3. Bypass the Greeks/delta exposure cap for cash equity (it's an options-only check).

**Acceptance**: an equity entry sizes to a sensible share quantity for the configured capital allocation;
no Greeks/delta rejection on a cash-equity order.

---

### TASK-EQ-04 — Backtest + rank the full equity universe (real OHLC)
- **Status**: `[x]` DONE 2026-07-02, commit `b47f28d` — full-universe run completed on real Upstox 5-minute OHLC as input to AR-04. Result is a **thin-sample frequency diagnosis**, not a reliable Sharpe ranking: most strategies emitted 0–1 trades over 60 days, so no name clears a true Sharpe > 1 evidence bar yet.
- **Tier**: 1–2
- **Session size**: ~1.5 hours
- **Prerequisite**: token connected on a trading day (EQ-01 not strictly required — backtest is read-only)
- **Targets**: rank all 12 equity names before any go live

**Problem**: the equity backtester is now wired to real Upstox OHLC (`POST /core/backtests/run` equity path,
commit 3905ae2) but has never been run on the full universe with a live token. LT/AXISBANK graded A/B on the
old `trade_fills` sample; the rest are unranked.

**Files to touch**: none (operational) — run the endpoint per strategy, record results.

**Exact steps**:
1. With the token connected, run `POST /core/backtests/run` for each of the 12 equity strategies.
2. Record win-rate / Sharpe / Sortino / max-DD / data_quality per name.
3. Produce a ranking; flag which names clear a Sharpe > 1 bar on a real sample.

**Acceptance**: a ranked table of all 12 equity strategies on real OHLC, with the A/B graders confirmed and
the negative-edge names identified.

**Result 2026-07-02 (60-day Upstox 5m OHLC, 1350–1351 bars/name)**:

| Strategy | Trades | Return | Win rate | Verdict |
|---|---:|---:|---:|---|
| LT Momentum Rider | 1 | +0.15% | 100% | WATCH — best thin-sample positive |
| HDFCBANK Range Rebound | 1 | +0.06% | 100% | WATCH — thin positive |
| BHARTIARTL Intraday Trend | 1 | +0.06% | 100% | WATCH — thin positive |
| AXISBANK Trend Follower | 1 | +0.04% | 100% | WATCH — thin positive |
| KOTAKBANK RSI Rebound | 1 | +0.02% | 100% | WATCH — thin positive |
| RELIANCE Trend Rider | 0 | 0.00% | 0% | DEAD/NO-SIGNAL |
| SBIN Short Seller | 0 | 0.00% | 0% | DEAD/NO-SIGNAL |
| TCS Swing Accumulator | 1 | −0.10% | 0% | WATCH/WEAK |
| INFY VWAP Pullback | 1 | −0.11% | 0% | WATCH/WEAK |
| ICICIBANK Volatility Breakout | 1 | −0.15% | 0% | WATCH/WEAK |

Capital rule from this run: do not use the OHLC sample to up-weight aggressively yet; AR-04 only raises the floor to cost-clearing size and preserves existing higher tiers.

---

### TASK-EQ-05 — Find the paused→live auto-reactivation root cause
- **Status**: `[x]` DONE 2026-07-01 (`9521e14`). Root cause: two pause flags. The active toggle (`routes/strategies.py`) already sets `manual_paused=True`+`schedule_paused=False` on manual pause; `enable-all` (ops.py) already skips `manual_paused=True`; but the 9AM + startup **auto-restore** paths keyed off `schedule_paused` only. Added `manual_paused:{$ne:True}` to both restore filters so an explicit manual pause can never be auto-woken even with a stale `schedule_paused`. No current behavior change (cycling strategies have `manual_paused` unset). ORPHAN FOUND (not auto-fixed): `RELIANCE Trend Rider` = `status=paused, schedule_paused=false, manual_paused=false` → won't auto-restore; founder to decide reactivate-or-leave.
- **Tier**: 2
- **Session size**: ~1.5 hours
- **Prerequisite**: None
- **Targets**: the unexplained reactivation that un-paused equity twice (06-16 → live by 06-18)

**Problem**: equity strategies were set `status=paused` on 06-16 but were `live` again by 06-18, root cause
NOT confirmed. The enable-all op (`server.py:8083`) flips anything NOT in `["live","paused"]` to live — the
assumption "paused is safe from enable-all" may be false, or a daily scheduler / manual action re-enabled them.
This MUST be understood before re-enabling the full universe, or they'll bleed again on any feed gap.

**Files to touch**: `backend/server.py` (enable-all `~:8083`), `backend/routes/ops.py`, the daily scheduler
loop, `backend/strategy_runner.py`.

**Exact steps**:
1. Audit every code path that can set a strategy `status=live` and check whether it respects `paused`.
2. Reproduce/confirm which path reactivated the equity set.
3. Ensure `paused` (with `pause_reason`) is durably honored — an explicitly-paused strategy must not be
   auto-reactivated by enable-all or the scheduler.

**Acceptance**: a paused strategy stays paused across an enable-all and a daily-scheduler cycle; the
reactivation vector is identified and closed.

---

### TASK-EQ-06 — Re-enable the full equity universe (paper) + paper-forward rank
- **Status**: `[x]` DONE — 10 NSE_EQ strategies live (paper) on the real feed, accumulating clean fills. Ranking/keep-kill rolls up into WR-44. (Universe is 10 live names, not the original 12 — ITC/MARUTI not in the live set.)
- **Tier**: 1
- **Session size**: ~1 hour + monitoring
- **Prerequisite**: EQ-01, EQ-02, EQ-03, EQ-04, EQ-05 all green
- **Targets**: full equity universe trading paper on a real feed

**Problem**: only safe once the feed is real (EQ-01), exec is sound (EQ-02), sizing is correct (EQ-03), the
universe is ranked (EQ-04), and they can't silently auto-reactivate (EQ-05).

**Exact steps**:
1. Re-enable all 12 equity strategies to `status=live` (paper) with the token connected.
2. Run a clean paper-forward window; track equity vs options P&L split (use `strategies.last_filter_reason`
   + `trade_fills` like the Profitability Campaign).
3. Rank on real fills → keep the survivors live alongside the options book.

**Acceptance**: full equity universe trading paper on the real feed with no phantom fills; a clean
paper-forward P&L sample accumulating for ranking.

---

## PRIORITY 1 — Bug Fixes (Ship These First)

---

### TASK-001 — Fix strategy limits enforcement
- **Status**: `[x]` commit 37c25dc · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**:
`validate_strategy_limits()` in `backend/signal_manager.py` returns success after finding the strategy but does NOT actually enforce:
- Per-strategy `max_trades_per_day` limit
- Per-strategy `cooldown_minutes` between trades
- Per-strategy `daily_stop_loss` threshold
- Global daily trade count cap

**Files to touch**: `backend/signal_manager.py` only

**Exact steps**:
1. Open `backend/signal_manager.py`, find `validate_strategy_limits` (or the equivalent validation function).
2. Read the full function and the strategy document schema — find where `max_trades_per_day`, `cooldown_minutes` fields live.
3. Add real enforcement:
   - Query `orders` collection: count today's filled orders for this strategy. If >= `max_trades_per_day`, return BLOCKED with reason.
   - Check the timestamp of the last order for this strategy. If within `cooldown_minutes`, return BLOCKED with reason.
   - Check today's realized P&L for the strategy. If below `daily_stop_loss` threshold, return BLOCKED with reason.
4. Log each block with: `logger.info(f"[LIMITS] strategy={strategy_id} blocked: {reason}")`

**How to verify**:
```bash
# Start backend locally or check VPS logs after deploy
# Set a strategy max_trades_per_day=1, fire two signals — second must be blocked
# Look for: [LIMITS] strategy=... blocked: ...
docker-compose logs backend --tail=50 | grep LIMITS
```

**Commit format**:
```
fix: enforce strategy cooldown, max_trades_per_day, daily_stop_loss in signal validation

Task: TASK-001
Tier: 2
Files changed: backend/signal_manager.py
```

---

### TASK-002 — Block duplicate exit orders before order creation
- **Status**: `[x]` commit 6fd78cc · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~45 min
- **Prerequisite**: None

**Problem**:
Currently duplicate exit orders are rejected by the ledger AFTER an order row is already created in the `orders` collection. In live trading this creates phantom order rows and risks duplicate fills. The idempotency check must happen BEFORE `order_manager.create_order()` is called.

**Files to touch**: `backend/core/execution_router.py` or wherever exit orders are dispatched (grep for `exit` + `create_order`).

**Exact steps**:
1. Grep: `grep -n "exit\|create_order" backend/core/execution_router.py`
2. Find the exit order dispatch path.
3. Before calling `create_order`, query the `orders` collection: check if an order with the same exit idempotency key (`"exit:{pos_id}:{reason[:20]}"`) already exists with status PENDING or FILLED.
4. If yes: log and return early — do NOT call `create_order`.
5. Add test in `backend/tests/test_audit_fixes.py` for this guard.

**How to verify**:
```bash
python -m pytest backend/tests/test_audit_fixes.py -v
# Also check: no duplicate rows in orders collection for same position exit
```

**Commit format**:
```
fix: check exit idempotency key before order creation, not after ledger rejection

Task: TASK-002
Tier: 2
Files changed: backend/core/execution_router.py, backend/tests/test_audit_fixes.py
```

---

### TASK-003 — Unify option quality gate into single entry check
- **Status**: `[x]` commit 6415dba · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~1.5 hours
- **Prerequisite**: None

**Problem**:
Two separate systems are evaluating option quality — one marks contracts as selected, another marks them as `quality_readiness: BLOCK`. Paper trades are going through despite the BLOCK signal. There is no single authoritative gate that says: "this contract is tradeable right now."

**Files to touch**: `backend/core/option_selector_v2.py`, `backend/signal_manager.py` (entry gate call site)

**Exact steps**:
1. Read `backend/core/option_selector_v2.py` fully — find the quality scoring logic.
2. Find where `quality_readiness` is set and where it is currently checked (or NOT checked) before order placement.
3. Create or enforce a single function `is_contract_tradeable(symbol, ltp, quote_age_seconds, spread_pct, volume) -> (bool, str)` that returns (tradeable, reason).
4. Enforce this check in `signal_manager.py` at the point of entry — before order is dispatched. If not tradeable, log and return FILTERED with reason.
5. Thresholds (use existing values or these defaults): `quote_age < 30s`, `spread_pct < 2%`, `volume > 500 contracts`.

**How to verify**:
```bash
python -m pytest backend/tests/test_option_selector_v2.py -v
# After deploy: grep logs for FILTERED with quality reason
docker-compose logs backend --tail=100 | grep "quality\|FILTERED"
```

**Commit format**:
```
fix: enforce single option quality gate before order dispatch; filter stale/wide/low-volume contracts

Task: TASK-003
Tier: 2
Files changed: backend/core/option_selector_v2.py, backend/signal_manager.py
```

---

## PRIORITY 2 — Architecture Foundation (Enables All Future Agent Work)

---

### TASK-004 — Create backend/config.py with all tunable constants
- **Status**: `[x]` commit 6415dba · 2026-06-11
- **Tier**: 1 (Any model — Haiku, Codex, GPT-4o mini)
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**:
Tunable constants (lot sizes, cooldown defaults, risk limits, quote age thresholds, spread limits) are scattered through `server.py`, `risk_manager.py`, `market_domains.py`, `signal_manager.py`. Tier 1 agents cannot change a config value without reading 15k lines.

**Files to touch**: Create `backend/config.py`. Grep for constants in existing files and reference them — do NOT remove them from their source files yet (that is a future refactor). This task is additive only.

**Exact steps**:
1. Create `backend/config.py`.
2. Grep for magic numbers and constants across backend:
   ```bash
   grep -n "= 65\|= 30\|cooldown\|max_trades\|daily_loss\|spread_pct\|quote_age\|500000\|lot_size" backend/server.py backend/core/*.py backend/signal_manager.py
   ```
3. Write a clean config module with sections: `MARKET`, `RISK`, `PAPER_TRADING`, `OPTION_QUALITY`, `STRATEGY_DEFAULTS`.
4. Each constant must have a one-line comment with its unit and meaning.
5. Import `os` at the top — any constant that could be an env var should fall back to env: `int(os.getenv("MAX_TRADES_PER_DAY", "5"))`.
6. Do NOT change any other file. This is documentation + future reference only for now.

**Template structure**:
```python
# backend/config.py
import os

class MARKET:
    NIFTY_LOT_SIZE = 65
    BANKNIFTY_LOT_SIZE = 30

class RISK:
    DEFAULT_DAILY_LOSS_LIMIT = int(os.getenv("DEFAULT_DAILY_LOSS_LIMIT", "5000"))  # INR
    MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
    DEFAULT_COOLDOWN_MINUTES = 15

class OPTION_QUALITY:
    MAX_QUOTE_AGE_SECONDS = 30
    MAX_SPREAD_PCT = 2.0
    MIN_VOLUME = 500

class PAPER_TRADING:
    STARTING_BALANCE = 500_000  # INR
    SLIPPAGE_PCT = 0.05         # 5 bps
```

**How to verify**:
```bash
python -c "from backend.config import MARKET, RISK; print(MARKET.NIFTY_LOT_SIZE, RISK.MAX_TRADES_PER_DAY)"
# Should print: 65 5
```

**Commit format**:
```
feat: add backend/config.py centralizing all tunable constants for agent-readable config

Task: TASK-004
Tier: 1
Files changed: backend/config.py
```

---

### TASK-005 — Write AGENT_ROUTER.md — symptom-to-file decision tree
- **Status**: `[x]` commit 6f61247 · 2026-06-11
- **Tier**: 1 (Any model)
- **Session size**: ~45 min
- **Prerequisite**: TASK-004 (so config.py file path is accurate)

**Problem**:
Agents waste time grepping the entire codebase to find where a bug lives. A routing document mapping symptoms → exact files and functions eliminates this cold-start cost for every future session.

**Files to touch**: Create `AGENT_ROUTER.md` at repo root.

**Exact steps**:
Build a decision-tree document with this structure for each symptom:

```markdown
## Symptom: "P&L is wrong / always 0"
- Primary file: backend/core/portfolio_ledger.py
- Secondary file: backend/server.py (search: "today_pnl")
- Collection: strategy_positions, trade_fills
- Common causes: fill not closing position, duplicate fill processed, wrong lot_size
- Test to run: python -m pytest tests/test_dashboard_truthfulness.py -v
- Tier: 2

## Symptom: "Order not placed / signal blocked"
- Primary file: backend/signal_manager.py → validate_strategy_limits()
- Secondary file: backend/core/risk_manager.py → _check_greeks_exposure()
- Look for: [LIMITS] or [RISK] in logs
- Test to run: python -m pytest tests/test_risk_controls.py -v
- Tier: 2
```

Build entries for ALL common symptoms. At minimum cover:
P&L wrong, order not placed, signal blocked, position stuck, paper fill not processing, strategy not activating, WebSocket disconnected, frontend not updating, option not selected, wrong strike selected, lot size wrong, wallet balance wrong, duplicate position, kill switch not triggering.

**How to verify**: Have another agent read the document and confirm they can find the right file for 3 different symptoms without opening any other file.

**Commit format**:
```
docs: add AGENT_ROUTER.md symptom-to-file decision tree for AI agent routing

Task: TASK-005
Tier: 1
Files changed: AGENT_ROUTER.md
```

---

### TASK-006 — Add 20 broker-free unit tests for core logic
- **Status**: `[x]` commit 6f61247 · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: None (tests must run without broker or live DB)

**Problem**:
The existing test suite (`backend/tests/`) has many integration tests that require a running server or broker. Agents cannot self-verify changes without tests that run locally in isolation.

**Files to touch**: `backend/tests/test_core_logic.py` (create new file)

**Exact steps**:
Write 20 tests in `backend/tests/test_core_logic.py` that cover pure functions only (no DB, no broker, no HTTP calls). Each test must pass with `pytest tests/test_core_logic.py -v` on a fresh clone.

Required test coverage:
1. `test_nifty_lot_size()` — market_domains returns 65 for NIFTY
2. `test_banknifty_lot_size()` — market_domains returns 30 for BANKNIFTY
3. `test_ce_symbol_check()` — `"CE" in "NIFTY 23200 CE 09 JUN 26"` is True
4. `test_ce_endswith_wrong()` — `"NIFTY 23200 CE 09 JUN 26".endswith("CE")` is False (document the pitfall)
5. `test_exit_idempotency_key_format()` — key = `f"exit:{pos_id}:{reason[:20]}"`, assert length <= 40
6. `test_entry_idempotency_key_format()` — sha256 key is 32 chars
7. `test_delta_proxy_ce_long()` — CE long qty=65 → delta = +0.5*65 = +32.5
8. `test_delta_proxy_pe_long()` — PE long qty=65 → delta = -0.5*65 = -32.5
9. `test_delta_proxy_ce_short()` — CE short qty=65 → delta = -32.5
10. `test_delta_proxy_pe_short()` — PE short qty=65 → delta = +32.5
11. `test_position_side_long()` — position_side="LONG" check pattern
12. `test_position_side_short()` — position_side="SHORT" check pattern
13. `test_pnl_long_position()` — long exit_price > entry_price → positive P&L
14. `test_pnl_short_position()` — short exit_price < entry_price → positive P&L
15. `test_paper_wallet_starting_balance()` — 500000 INR
16. `test_quote_age_threshold()` — age > 30s should flag as stale
17. `test_spread_pct_threshold()` — spread > 2% should flag as wide
18. `test_option_symbol_contains_space()` — NSE verbose format has spaces
19. `test_instrument_key_format()` — Upstox format: `"NSE_FO|<numeric_token>"`
20. `test_exit_qty_uses_open_quantity()` — exit_qty must come from open_quantity, not original qty

**How to verify**:
```bash
cd backend
python -m pytest tests/test_core_logic.py -v
# All 20 must pass. Zero imports from server.py.
```

**Commit format**:
```
test: add 20 broker-free unit tests for core trading logic

Task: TASK-006
Tier: 2
Files changed: backend/tests/test_core_logic.py
```

---

### TASK-007 — Create canonical P&L source function
- **Status**: `[x]` commit 6f61247 · 2026-06-11
- **Tier**: 3 (Opus / Claude — cross-module)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-006 (tests must exist to verify this)

**Problem**:
P&L is computed in at least 4 places: `server.py` (dashboard endpoint), `portfolio_ledger.py` (fill processing), `strategy_runner.py` (today_pnl update), and the `positions` collection mirror. They disagree. The calendar system, capital allocator, and leaderboard all need one truth.

**Files to touch**: `backend/core/portfolio_ledger.py` (add canonical function), `backend/server.py` (update dashboard endpoint to call it)

**Exact steps**:
1. Read `backend/core/portfolio_ledger.py` fully.
2. Read all places in `server.py` that compute or return P&L (grep: `pnl\|today_pnl\|realized`).
3. Write a single async function in `portfolio_ledger.py`:
   ```python
   async def get_strategy_pnl_today(db, strategy_id: str, user_id: str) -> dict:
       """Returns: {realized_pnl, unrealized_pnl, total_pnl, trade_count, last_updated}"""
   ```
   Source of truth: `trade_fills` collection (not orders, not positions mirror).
4. Update the dashboard endpoint in `server.py` to call this function instead of computing inline.
5. Update `strategy_runner.py` `today_pnl` update to call this function.
6. Add tests in `backend/tests/test_core_logic.py` (or new file) that mock `trade_fills` data and assert the canonical function returns correct values.

**How to verify**:
```bash
python -m pytest tests/ -k "pnl" -v
# Deploy and check: dashboard P&L matches strategy cards P&L matches wallet change
```

**Commit format**:
```
feat: add canonical get_strategy_pnl_today() in portfolio_ledger; remove inline P&L duplication

Task: TASK-007
Tier: 3
Files changed: backend/core/portfolio_ledger.py, backend/server.py, backend/strategy_runner.py
```

---

## PRIORITY 3 — Server.py Route Extraction (Enables Tier 1/2 Work at Scale)

---

### TASK-008 — Extract strategy routes from server.py → routes/strategies.py
- **Status**: `[x]` commit 4d1bf01 · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-006 (tests), TASK-007 (canonical P&L)

**Problem**:
All strategy CRUD and control endpoints live inside `server.py`. Any agent touching strategy logic must load 15k lines of context.

**Files to touch**: Create `backend/routes/strategies.py`. Edit `backend/server.py` (include new router, remove extracted routes).

**Exact steps**:
1. Grep: `grep -n "@api.get\|@api.post\|@api.put\|@api.delete\|@api.patch" backend/server.py | grep -i strat`
2. List every strategy-related endpoint. Typical candidates: GET /strategies, POST /strategies, PUT /strategies/{id}, POST /strategies/{id}/activate, POST /strategies/{id}/pause, DELETE /strategies/{id}.
3. Create `backend/routes/strategies.py` with an `APIRouter(prefix="/strategies")`.
4. Move each endpoint function into the new file. Bring all required imports.
5. In `server.py`, add: `from backend.routes.strategies import router as strategies_router` and `api.include_router(strategies_router)`. Remove the moved endpoints.
6. Verify all tests pass and the server starts clean.

**Critical**: Do NOT change any endpoint URL, request schema, or response schema. This is a pure move — zero behavior change.

**How to verify**:
```bash
python -m pytest tests/ -v
# Start backend locally, hit GET /strategies — same response as before
# grep server.py for old route decorators — they must be gone
```

**Commit format**:
```
refactor: extract strategy routes from server.py into routes/strategies.py

Task: TASK-008
Tier: 3
Files changed: backend/routes/strategies.py (new), backend/server.py
```

---

### TASK-009 — Extract signal routes from server.py → routes/signals.py
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-008 done

**Problem**: Same as TASK-008 but for signal-related endpoints.

**Files to touch**: Create `backend/routes/signals.py`. Edit `backend/server.py`.

**Exact steps**:
1. Grep: `grep -n "@api" backend/server.py | grep -i signal`
2. Move all signal endpoints to `backend/routes/signals.py` with `APIRouter(prefix="/signals")`.
3. Register in `server.py`, remove originals.
4. Run tests, verify server starts clean.

**How to verify**: Same pattern as TASK-008. Grep confirms endpoints gone from server.py.

**Commit format**:
```
refactor: extract signal routes from server.py into routes/signals.py

Task: TASK-009
Tier: 3
Files changed: backend/routes/signals.py (new), backend/server.py
```

---

### TASK-010 — Extract orders + positions routes → routes/orders.py + routes/positions.py
- **Status**: `[x]` commit 68033c8 · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-009 done

**Files to touch**: Create `backend/routes/orders.py`, `backend/routes/positions.py`. Edit `backend/server.py`.

**Exact steps**: Same pattern. Grep for order-related and position-related endpoints. Move them. Register routers. Verify.

**Commit format**:
```
refactor: extract order and position routes from server.py into dedicated route files

Task: TASK-010
Tier: 3
Files changed: backend/routes/orders.py (new), backend/routes/positions.py (new), backend/server.py
```

---

### TASK-011 — Extract dashboard/P&L routes → routes/dashboard.py
- **Status**: `[x]` commit c6999de · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-007 (canonical P&L), TASK-010 done

**Files to touch**: Create `backend/routes/dashboard.py`. Edit `backend/server.py`.

**Commit format**:
```
refactor: extract dashboard and P&L routes into routes/dashboard.py

Task: TASK-011
Tier: 3
Files changed: backend/routes/dashboard.py (new), backend/server.py
```

---

## PRIORITY 4 — Calendar & Daily Report System

---

### TASK-012 — Backend: EOD aggregation job + daily_reports collection
- **Status**: `[x]`
- **Tier**: 2 (Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-007 (canonical P&L must exist)

**Problem**: There is no per-day summary of trading activity. The calendar UI (TASK-013) needs a backend data source.

**Files to touch**: Create `backend/routes/reports.py`. Edit `backend/position_monitor.py` (add EOD job).

**Exact steps**:
1. Create `backend/routes/reports.py` with these endpoints:
   - `GET /reports/daily/{date}` — returns one day's summary for the authenticated user
   - `GET /reports/daily` — returns last 30 days of summaries (for calendar month view)
2. MongoDB collection: `daily_reports`. Document schema:
   ```json
   {
     "user_id": "...",
     "date": "2026-06-11",
     "total_realized_pnl": 1250.0,
     "total_unrealized_pnl": -200.0,
     "trades_taken": 4,
     "signals_fired": 12,
     "signals_filtered": 8,
     "market_regime": "TRENDING",
     "best_strategy": {"name": "...", "pnl": 800.0},
     "worst_strategy": {"name": "...", "pnl": -200.0},
     "strategies": [...per-strategy breakdown...],
     "generated_at": "2026-06-11T15:35:00+05:30"
   }
   ```
3. In `backend/position_monitor.py`, add an `_run_eod_aggregation(db)` async function that:
   - Runs at 15:35 IST on market days
   - Calls `get_strategy_pnl_today()` for each active strategy
   - Writes/upserts a `daily_reports` document for today
   - Already in the monitor loop — add alongside the existing 30s check

**How to verify**:
```bash
# Hit GET /reports/daily/2026-06-11 — should return today's summary (or empty if no trades)
# Check MongoDB: db.daily_reports.find({date: "2026-06-11"}).pretty()
```

**Commit format**:
```
feat: add daily_reports collection, EOD aggregation job, and GET /reports/daily endpoints

Task: TASK-012
Tier: 2
Files changed: backend/routes/reports.py (new), backend/position_monitor.py
```

---

### TASK-013 — Frontend: Calendar page with daily P&L heatmap
- **Status**: `[x]`
- **Tier**: 2 (Sonnet / GPT-4o — frontend)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-012 (backend reports API must exist)

**Problem**: No UI to see historical trading performance at a glance.

**Files to touch**: Create `frontend/src/pages/Calendar.jsx`. Edit `frontend/src/App.js` (route), edit sidebar nav component.

**Exact steps**:
1. Create `frontend/src/pages/Calendar.jsx`.
2. Month-view grid (7 columns, weeks as rows). Each day cell:
   - Green if realized P&L > 0, red if < 0, grey if no trades, white if future/weekend.
   - Show the P&L amount inside the cell if it fits.
   - Click on a day → slide-out panel on the right showing: trade count, per-strategy breakdown, best/worst strategy, market regime badge.
3. Navigation: prev/next month arrows. Default to current month.
4. Data: call `GET /reports/daily?month=2026-06` → array of day summaries.
5. Add route in `App.js`: `<Route path="/calendar" element={<Calendar />} />`.
6. Add "Calendar" link in the sidebar nav (wherever other nav items are).
7. Use existing Tailwind classes — do not introduce a new CSS framework.
8. No new npm packages — use only what is already installed.

**How to verify**:
```bash
# Rebuild frontend and open /calendar in browser
# Verify: month grid renders, days with trades are coloured, clicking a day shows detail
docker-compose build frontend && docker-compose up -d frontend
```

**Commit format**:
```
feat: add Calendar page with daily P&L heatmap and per-day trade drill-down

Task: TASK-013
Tier: 2
Files changed: frontend/src/pages/Calendar.jsx (new), frontend/src/App.js, frontend/src/components/Sidebar.jsx (or wherever nav lives)
```

---

## PRIORITY 5 — Capital Allocator & Regime Weighting

---

### TASK-014 — Adaptive capital allocator across strategies
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~4 hours
- **Prerequisite**: TASK-007 (canonical P&L), TASK-001 (strategy limits enforced)

**Problem**: All strategies receive equal capital regardless of recent performance. A strategy on a 3-trade win streak should get more capital than one that lost 3 times today.

**Files to touch**: Create `backend/core/capital_allocator.py`. Edit `backend/signal_manager.py` (use allocator at signal entry). Edit `backend/core/risk_manager.py` (read allocation before sizing).

**Exact steps**:
1. Create `backend/core/capital_allocator.py` with:
   ```python
   async def get_strategy_allocation_multiplier(db, strategy_id, user_id) -> float:
       """Returns a multiplier 0.5–2.0 based on recent performance.
       Hot (3+ wins today): 1.5x. Cold (2+ losses today): 0.5x. Neutral: 1.0x."""
   ```
2. Multiplier logic:
   - Fetch last 5 trades for the strategy today from `trade_fills`.
   - Win streak >= 3: return 1.5
   - Loss streak >= 2: return 0.5
   - Otherwise: return 1.0
   - Hard cap: never exceed 2.0x, never go below 0.25x.
3. In `risk_manager.py`, multiply the computed position size by `get_strategy_allocation_multiplier()` result before order dispatch.
4. Log: `logger.info(f"[ALLOC] strategy={strategy_id} multiplier={mult:.2f}")`

**How to verify**:
```bash
# Seed DB with wins/losses for a strategy, call the function, assert correct multiplier
python -m pytest tests/ -k "alloc" -v
docker-compose logs backend --tail=50 | grep ALLOC
```

**Commit format**:
```
feat: add adaptive capital allocator — hot strategies get 1.5x, cold get 0.5x

Task: TASK-014
Tier: 3
Files changed: backend/core/capital_allocator.py (new), backend/signal_manager.py, backend/core/risk_manager.py
```

---

### TASK-015 — Regime → capital weighting (not only blocking)
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-014 (allocator must exist)

**Problem**: Market regime currently only blocks strategies (binary ALLOW/BLOCK). A trending day should give trending strategies MORE capital, not just allow them — and give range strategies LESS, not block them outright.

**Files to touch**: `backend/market_regime.py`, `backend/core/capital_allocator.py`

**Exact steps**:
1. Read `backend/market_regime.py` — find how regime is determined and returned.
2. Add a `get_regime_multiplier(strategy_type, current_regime) -> float` function:
   - TRENDING regime + trend-following strategy: 1.3x
   - TRENDING regime + range strategy: 0.5x
   - RANGE regime + range strategy: 1.3x
   - RANGE regime + trend strategy: 0.5x
   - VOLATILE regime + breakout strategy: 1.5x
   - Neutral/unknown: 1.0x
3. Multiply this into the final allocation in `capital_allocator.py`.
4. Keep the existing hard BLOCK for extreme regime mismatches (e.g., trying to go long in a strong downtrend) — do not remove that safety.

**Commit format**:
```
feat: convert regime gate from binary block to capital weight multiplier

Task: TASK-015
Tier: 3
Files changed: backend/market_regime.py, backend/core/capital_allocator.py
```

---

## PRIORITY 6 — Typed Module Contracts (Ongoing Quality)

---

### TASK-016 — Add TypedDict contracts for position and order data shapes
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-010 (route extraction) should be done first

**Problem**: Functions pass raw `dict` objects between modules. Agents reading `risk_manager.py` cannot know what fields are guaranteed without tracing callers.

**Files to touch**: `backend/core/models.py` (add TypedDicts). No other file changes in this task — just define the types.

**Exact steps**:
1. Open `backend/core/models.py` — check what already exists.
2. Add TypedDict definitions for the most-passed data shapes:
   - `PositionDoc` — all fields a position document can have
   - `OrderDoc` — all fields an order document can have
   - `FillDoc` — fields in a trade_fill record
   - `StrategyDoc` — fields in a strategy document
   - `SignalEvent` — what signal_manager passes downstream
3. Use `TypedDict` with `total=False` for optional fields.
4. Add a docstring to each TypedDict listing which collection it maps to.

**How to verify**:
```bash
python -c "from backend.core.models import PositionDoc, OrderDoc, FillDoc; print('OK')"
```

**Commit format**:
```
feat: add TypedDict contracts for position, order, fill, strategy, and signal shapes

Task: TASK-016
Tier: 2
Files changed: backend/core/models.py
```

---

### TASK-017 — Loss-streak throttling: wire strategy_loss_streaks into signal gate
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o)
- **Session size**: ~1.5 hours
- **Prerequisite**: TASK-001 (strategy limits), TASK-014 (allocator)

**Problem**: `strategy_loss_streaks` collection exists but is not being read at signal entry time. Bad strategies keep firing.

**Files to touch**: `backend/signal_manager.py`

**Exact steps**:
1. In `validate_strategy_limits()` (after TASK-001 changes), add:
   - Query `strategy_loss_streaks` for this strategy's current streak.
   - If streak >= 3: set `allocation_multiplier = 0.25` and log `[THROTTLE]`.
   - If streak >= 5: return BLOCKED entirely (cooldown until next day).
2. Update `strategy_loss_streaks` on each confirmed losing trade close (in `portfolio_ledger.py` fill processing).

**Commit format**:
```
feat: wire loss-streak throttling into signal gate — 3 losses = 0.25x, 5 losses = blocked

Task: TASK-017
Tier: 2
Files changed: backend/signal_manager.py, backend/core/portfolio_ledger.py
```

---

## FUTURE / BACKLOG (Do Not Start Until Priority 1–4 Complete)

---

### TASK-018 — Typed contracts rollout across core/ modules
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2
- **Prerequisite**: TASK-016

Replace `dict` parameter types with TypedDict references in `risk_manager.py`, `portfolio_ledger.py`, `execution_router.py`. Fold into future fixes — no dedicated session needed.

---

### TASK-019 — SENSEX regime detection
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2
- **Prerequisite**: TASK-015

Extend `market_regime.py` to support SENSEX in addition to NIFTY/BANKNIFTY. Same regime logic, different index token.

---

### TASK-020 — Paper fill realism: slippage + partial fill model
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2
- **Prerequisite**: None (independent)

Improve `backend/core/paper_broker.py`: add configurable slippage (default 5 bps from config.py), add partial fill simulation for low-volume strikes, add quote timestamp validation before fill price is accepted.

---

## PRIORITY — Phase 2 UI Polish (surface the options-alpha features)

Context: Phase 2 (theta-aware exits, delta strikes, IV-rank gate, order-flow gate, credit spreads) is deployed; config + strategy/positions UI are done. These tasks make the engine's behaviour legible in the UI. All are frontend-leaning, low engine risk, gated/additive. Do in order; deploy + verify each. Frontend changes require `docker-compose build frontend && docker-compose up -d frontend` (a restart does NOT pick up JSX).

### TASK-023 — Friendly labels for Phase 2 exit/skip reasons
- **Status**: `[x]` commit 86705ba · 2026-06-16
- **Tier**: 2
- **Session size**: ~45 min
- **Prerequisite**: None

**Problem**: New reasons render as raw codes (`theta-decay-10m`, `theta-no-progress-8m`, `spread-tp`, `spread-sl`, `IV_RANK_GATE`, `IV_RANK_SHADOW`, `ORDERFLOW_GATE`, `ORDERFLOW_SHADOW`, `CREDIT_SPREADS_DISABLED`). `Strategies.jsx` already has a `noticeFor()` mapper for older reasons; the Phase 2 ones aren't covered, and Orders/Positions show reasons raw.

**Files to touch**: `frontend/src/lib/` (new shared `reasonLabels.js` helper), `frontend/src/pages/Strategies.jsx`, `frontend/src/pages/Orders.jsx`, `frontend/src/pages/Positions.jsx`.

**Steps**: add a `reasonLabel(code)` → `{ label, tone, hint }` map (covering Phase 2 + existing codes); render exit_reason / last_filter_reason / rejection_reason through it with a colored chip + tooltip. Keep raw code in a title attr for debugging.

**Verify**: a position closed `theta-decay-12m` shows "Closed early — theta decay (12m)"; a skipped signal `IV_RANK_GATE` shows "Blocked — IV too rich for buying".

### TASK-024 — "Why isn't it trading?" readiness banner
- **Status**: `[x]` commit ff7f411 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**: Token expires nightly + strategies sit paused → screens look silently empty. No in-app signal of the blocking condition.

**Files to touch**: `frontend/src/components/` (new `ReadinessBanner.jsx`), mount in `Layout.jsx` (or Dashboard/Strategies). Reuse existing `/upstox/status` and `/strategies` data.

**Steps**: top strip that shows, when blocking: "Upstox token expired — reconnect" (link to ApiKeys) and "0 of N strategies armed — arm to trade". Hide when token live AND ≥1 strategy live. Subtle, dismissible.

**Verify**: with token disconnected / all paused, banner shows both messages; after reconnect + arming, it disappears.

### TASK-025 — Greeks (δ/θ/IV) on positions & signals
- **Status**: `[x]` commit b8f7f70 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**: `greeks_at_signal`/`greeks_at_entry` now populate but are shown nowhere. Delta-selection picks strikes by δ — users can't see it.

**Files to touch**: `backend/execution_state.py` (carry `delta/theta/iv` + `target_delta` onto the position snapshot), `frontend/src/pages/Positions.jsx` (show δ/θ/IV, ideally a detail popover).

**Steps**: surface entry greeks on the position row/popover; show "δ 0.46 (target 0.45)" when `target_delta` present.

**Verify**: an option position shows its δ/θ/IV; a delta-selected one shows target vs actual.

### TASK-026 — Spread leg detail (expandable row / popover)
- **Status**: `[x]` commit 362f731 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: TASK-025 (shares the popover pattern)

**Problem**: Spread positions show a badge + credit/max-loss but not the two legs.

**Files to touch**: `frontend/src/pages/Positions.jsx` (legs already in snapshot from `execution_state.py`).

**Steps**: expandable row/tooltip showing short & long leg (strike/type/premium), net δ/θ, current spread value vs TP(50%)/SL(2×) levels; small progress bar from credit→max-loss.

**Verify**: a credit_spread position expands to show both legs and where value sits between TP and SL.

### TASK-027 — Phase 2 feature-status panel (read-only)
- **Status**: `[x]` commit f872522 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**: Gate flags are env-driven and invisible in-app; you must SSH to know what's on.

**Files to touch**: `backend/server.py` or `backend/routes/ops.py` (new `GET /ops/feature-flags` returning each Phase 2 flag's state), `frontend/src/pages/OpsConsole.jsx` (render a status panel).

**Steps**: endpoint returns theta-exit/delta/IV-rank/order-flow/credit-spreads as ON/OFF/shadow (read env). UI shows colored chips. Read-only (no toggles).

**Verify**: panel matches `docker exec quantg-backend printenv | grep` for the flags.

### TASK-028 — Structure badge on strategy cards
- **Status**: `[x]` commit 63b9829 · 2026-06-16
- **Tier**: 3 (small)
- **Session size**: ~30 min
- **Prerequisite**: None

**Problem**: Can't tell at a glance which option strategies are credit-spread vs single-leg.

**Files to touch**: `frontend/src/pages/Strategies.jsx`.

**Steps**: show a "Spread" / "Single-leg" chip on option strategies from `visual_config.options.structure`.

**Verify**: a strategy set to `credit_spread` shows the Spread chip.

### TASK-029 — IV-Regime card → visual gauge
- **Status**: `[x]` commit 6c15010 · 2026-06-16
- **Tier**: 3 (small)
- **Session size**: ~30 min
- **Prerequisite**: None

**Problem**: MarketHub IV-Regime card is text rows; no visual sense of where IV rank sits.

**Files to touch**: `frontend/src/pages/MarketHub.jsx`.

**Steps**: add a horizontal bar showing IV rank within the 52w min–max, plus a cheap/rich colored chip from `would_block_buys`.

**Verify**: card shows a bar with the marker at the current rank.

### TASK-030 — Per-strategy trade-cap indicator
- **Status**: `[x]` commit 63b9829 · 2026-06-16
- **Tier**: 3 (small)
- **Session size**: ~30 min
- **Prerequisite**: None

**Problem**: New caps (now 8/day) and throttling aren't visible as they fill.

**Files to touch**: `frontend/src/pages/Strategies.jsx` (uses `order_count_today` + `visual_config.risk.max_trades_day`).

**Steps**: small "N / 8 today" counter per strategy; muted when 0, warn-tone when at cap.

**Verify**: a strategy with 3 fills shows "3 / 8".

---

## PRIORITY 7 — Architecture Redesign Stage 0 (Event Catalog + Ownership Map)

Context: CLAUDE.md §11 is now the active architecture program. Stage 0 is documentation only: no runtime code, no fill/P&L/wallet math changes, no new infra, no deployment. The goal is to make the event-bus redesign concrete enough for founder approval before Stage 1.

### TASK-031 — Stage 0A: Create event catalog draft
- **Status**: `[x]` commit this session
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: Founder approval to start Stage 0 task breakdown

**Problem**:
The architecture map identifies the need for an event bus, but the actual events are not named or cataloged. Without a catalog, agents may invent incompatible event names and payloads.

**Files to touch**: Create `docs/architecture/EVENT_CATALOG.md` only.

**Exact steps**:
1. Read AGENTS.md, TASKS.md, and CLAUDE.md §11 first.
2. Inspect the current trade lifecycle only enough to document existing transitions; do not edit code.
3. Draft event families for the current monolith:
   - Strategy lifecycle events
   - Signal lifecycle events
   - Order lifecycle events
   - Fill lifecycle events
   - Position lifecycle events
   - Risk/readiness events
   - Broker/feed events
   - P&L/reporting events
4. For each event, document:
   - Event name
   - Producer today
   - Consumers today
   - Current collection writes caused by the transition
   - Proposed owner module
   - Idempotency key or natural dedupe key
   - Required correlation/causation fields, marked TBD where founder decision is needed
5. Add a "Founder decisions required" section for event naming style, payload schema style, and correlation id format.

**How to verify**:
```bash
git diff -- docs/architecture/EVENT_CATALOG.md
```
Confirm the file is documentation only and contains no implementation instructions that change fill, P&L, wallet, or live-trading behavior.

**Commit format**:
```
docs: draft Stage 0 event catalog for architecture redesign

Task: TASK-031
Tier: 2
Files changed: docs/architecture/EVENT_CATALOG.md
```

---

### TASK-032 — Stage 0B: Create collection ownership map
- **Status**: `[x]` commit this session
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-031

**Problem**:
CLAUDE.md §11 identifies `strategy_positions` and `strategies.today_pnl` as the highest-risk multi-writer zones. The app needs an explicit ownership map before any single-writer refactor starts.

**Files to touch**: Create `docs/architecture/COLLECTION_OWNERSHIP.md` only.

**Exact steps**:
1. Read CLAUDE.md §11.4 and §11.8.
2. Document every core collection currently named in CLAUDE.md §7 and §11:
   - Current purpose
   - Current runtime writers
   - Current readers, where obvious
   - Risk level
   - Proposed single owner
   - Allowed readers
   - Open founder decision, if ownership is contested
3. Mark these as contested until founder approval:
   - `strategy_positions`
   - `strategies.today_pnl`
   - `positions` UI mirror
   - SQLite `option_state_ledger`
4. Explicitly document that `trade_fills` and `paper_wallets` are the safe templates for single ownership.

**How to verify**:
```bash
git diff -- docs/architecture/COLLECTION_OWNERSHIP.md
```
Confirm this task does not change runtime code or database schema.

**Commit format**:
```
docs: map collection ownership for Stage 0 architecture redesign

Task: TASK-032
Tier: 2
Files changed: docs/architecture/COLLECTION_OWNERSHIP.md
```

---

### TASK-033 — Stage 0C: Founder approval memo for Stage 1
- **Status**: `[x]` commit this session
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~1 hour
- **Prerequisite**: TASK-031, TASK-032

**Problem**:
Stage 1 introduces the first in-process event bus and correlation ids. AGENTS.md says event names, payload schemas, correlation id format, and contested owners are stop-and-ask decisions.

**Files to touch**: Create `docs/architecture/STAGE_1_APPROVAL_MEMO.md` only.

**Exact steps**:
1. Summarize the Stage 0 event catalog and ownership map.
2. List the exact founder decisions required before Stage 1:
   - Event naming convention
   - Payload schema convention
   - Correlation id and causation id format
   - First loop to convert
   - Single owner for `strategy_positions`
   - Single owner for `strategies.today_pnl`
   - Deprecate vs delete stance for legacy fill path, `_mongo_position_monitor_loop`, and SQLite `option_state_ledger`
3. Recommend the lowest-risk Stage 1 slice, but do not implement it.
4. Include a clear "Do not deploy" note: Stage 0 is docs-only and does not require VPS deployment.

**How to verify**:
```bash
git diff -- docs/architecture/STAGE_1_APPROVAL_MEMO.md
```
Confirm the memo is readable by the founder without opening code.

**Commit format**:
```
docs: add Stage 1 approval memo after architecture Stage 0

Task: TASK-033
Tier: 2
Files changed: docs/architecture/STAGE_1_APPROVAL_MEMO.md
```

---

## PRIORITY 8 — Architecture Redesign Stage 1 (Publish-Only Event Bus)

Context: Founder approved Stage 1A defaults on 2026-06-18: UPPER_SNAKE_CASE event names, Pydantic payload contracts, `corr:<signal_id>` correlation ids, causation id as previous event id or source record id, existing Mongo `core_events` storage, and signal manager as the first publish-only slice.

### TASK-034 — Stage 1A: Publish-only signal lifecycle events
- **Status**: `[x]` Completed by Codex
- **Commit**: `cebefca`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-033 and founder approval

**Problem**:
Signals move through `PENDING`, `FILTERED`, `SKIPPED_SIGNAL`, and `PROCESSED`, but there is no structured event trail tying the transitions together. Debugging "why didn't it trade?" still depends on mutable signal rows and logs.

**Files to touch**: `backend/core/event_store.py`, `backend/signal_manager.py`, tests under `backend/tests/` if needed.

**Exact steps**:
1. Add a small Pydantic-backed signal event publishing helper using existing Mongo `core_events`.
2. Publish events without changing existing signal/order behavior:
   - `SIGNAL_QUEUED` when the signal manager observes a pending signal for processing
   - `SIGNAL_VALIDATION_FAILED` when validation/limits/quality filtering sets `FILTERED`
   - `SIGNAL_PRIORITY_SKIPPED` when conflict resolution sets `SKIPPED_SIGNAL`
   - `SIGNAL_PROCESSED` when dispatch succeeds and links an order id
   - `SIGNAL_EXECUTION_SKIPPED` when dispatch returns a skipped signal or raises at the execution boundary
3. Use correlation id `corr:<signal_id>`.
4. Use causation id `record:signals:<signal_id>` unless a previous event id is available.
5. Make event persistence best-effort: failure to write `core_events` must never block signal processing.
6. Do not change order creation, fills, wallet, P&L, positions, broker behavior, or live flags.

**How to verify**:
```bash
cd backend
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m py_compile core/event_store.py signal_manager.py
```

**Commit format**:
```
feat: publish signal lifecycle audit events without changing trading behavior

Task: TASK-034
Tier: 2
Files changed: backend/core/event_store.py, backend/signal_manager.py, backend/tests/<test-file-if-added>
```

---

### TASK-035 — Extract market routes from server.py into routes/market.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `116d7d8`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-034

**Problem**:
`server.py` still owns market-data and option-preview HTTP endpoints even after earlier route extraction. This keeps market UI/API work tied to the giant startup file.

**Files to touch**: Create `backend/routes/market.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only market/query endpoints into `backend/routes/market.py`:
   - `/market/watchlist`
   - `/market/iv-rank`
   - `/market/candles/{instrument_key:path}`
   - `/market/analytics/option-chain`
   - `/market/analytics/expiry-dates`
   - `/market/commodities`
   - `/market/quote/{symbol}`
   - `/market/feed-comparison`
   - `/market/auto-data-broker`
   - `/market/indicators/{symbol}`
   - `/market/session-status`
   - `/market/session`
   - `/market/regime`
   - `/option-chain/{underlying}`
   - `/options/preview`
2. Register the new market router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, trading execution, broker execution, wallet, P&L, positions, or live flags.
4. Keep `server.py` as the startup authority.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/market.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract market routes from server.py

Task: TASK-035
Tier: 2
Files changed: backend/server.py, backend/routes/market.py, TASKS.md
```

---

### TASK-036 — Extract broker and Upstox routes from server.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `5287022`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-035

**Problem**:
`server.py` still owns broker key, Upstox OAuth/status/control, Upstox quality, gateway, webhook, and legacy Zerodha HTTP routes. This keeps broker UI/API work tied to the giant startup file.

**Files to touch**: Create `backend/routes/broker.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only broker/upstox/gateway HTTP endpoints into `backend/routes/broker.py`:
   - `/broker/keys`
   - `/broker/keys/{key_id}`
   - `/upstox/data-health`
   - `/upstox/instruments/sync`
   - `/upstox/quality-system/migrate`
   - `/upstox/option-chain`
   - `/upstox/webhook`
   - `/upstox/reconciliation`
   - `/upstox/exit-all`
   - `/zerodha/login-url`
   - `/zerodha/exchange`
   - `/zerodha/status`
   - `/zerodha/disconnect`
   - `/broker/upstox/config`
   - `/broker/upstox/login`
   - `/broker/upstox/callback`
   - `/broker/upstox/order/test`
   - `/broker/upstox/positions`
   - `/broker/upstox/orders`
   - `/broker/upstox/quote`
   - `/broker/upstox/market-data/start`
   - `/upstox/status`
   - `/broker/upstox/status`
   - `/brokers/status`
   - `/diagnostics/health`
   - `/broker/health`
   - `/gateway/check-all`
   - `/gateway/status`
   - `/webhook/upstox/token/{user_id}`
2. Register the new broker router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, broker execution behavior, wallet, P&L, positions, or live flags.
4. Keep `server.py` as the startup authority and keep Upstox feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/broker.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract broker and upstox routes from server.py

Task: TASK-036
Tier: 2
Files changed: backend/server.py, backend/routes/broker.py, TASKS.md
```

---

### TASK-037 — Extract profile, portfolio, funds, and paper wallet routes
- **Status**: `[x]` Completed by Codex
- **Commit**: `60f0678`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-036

**Problem**:
`server.py` still owns profile/account, portfolio/funds, paper-wallet, and paper recovery/reset HTTP routes. These are user/account surfaces and should not stay coupled to the giant startup file.

**Files to touch**: Create `backend/routes/profile.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only account/profile and wallet query/recovery endpoints into `backend/routes/profile.py`:
   - `/portfolio/holdings`
   - `/portfolio`
   - `/funds`
   - `/profile/paper-trading-stats`
   - `/profile`
   - `/profile/reset-paper`
   - `/profile/recover-paper-contract-halts`
   - `/paper-wallet`
   - `/profile/change-password`
2. Register the new profile router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, wallet math, P&L, positions, broker behavior, or live flags.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/profile.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract profile and wallet routes from server.py

Task: TASK-037
Tier: 2
Files changed: backend/server.py, backend/routes/profile.py, TASKS.md
```

---

### TASK-038 — Extract readiness and core health routes from server.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `457a43f`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-037

**Problem**:
`server.py` still owns readiness and health endpoints even though they are mostly read-only API surfaces. Keeping them inline makes status/readiness work depend on the startup file.

**Files to touch**: Create `backend/routes/readiness.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only readiness/status endpoints into `backend/routes/readiness.py`:
   - `/strategy-readiness`
   - `/paper-readiness`
   - `/live/readiness`
   - `/trading/live-readiness`
   - `/core/live/readiness`
   - `/core/health`
   - `/core/market-status`
   - `/core/feed-status`
2. Register the new readiness router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, readiness logic, live flags, broker behavior, wallet, P&L, or positions.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/readiness.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract readiness routes from server.py

Task: TASK-038
Tier: 2
Files changed: backend/server.py, backend/routes/readiness.py, TASKS.md
```

---

### TASK-039 — Extract remaining ops runtime routes from server.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `943cd05`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-038

**Problem**:
`server.py` still owns a few operational HTTP routes. Moving them into a small ops-runtime module reduces route coupling without touching startup loops.

**Files to touch**: Create `backend/routes/ops_runtime.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only these ops endpoints into `backend/routes/ops_runtime.py`:
   - `/ops/v12/upstox-retailer/activate`
   - `/ops/squareoff-all`
   - `/ops/trading-ready`
2. Register the new ops-runtime router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, order behavior, live flags, wallet, P&L, or positions.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/ops_runtime.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract ops runtime routes from server.py

Task: TASK-039
Tier: 2
Files changed: backend/server.py, backend/routes/ops_runtime.py, TASKS.md
```

---

### TASK-040 — Extract core data and backtest routes from server.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `62466ef`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-039

**Problem**:
`server.py` still owns core read-only data routes and the core backtest runner endpoint. These should live beside the other route modules so the startup file can keep shrinking.

**Files to touch**: Create `backend/routes/core_status.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only these core data/backtest endpoints into `backend/routes/core_status.py`:
   - `/core/strategies`
   - `/core/orders`
   - `/core/positions`
   - `/core/performance`
   - `/core/backtests`
   - `/core/backtests/run`
   - `/core/live/arm`
   - `/core/live/disarm`
   - `/core/kill-switch`
2. Register the new core-status router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, order behavior, live flags, wallet, P&L, or positions.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/core_status.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract core status routes from server.py

Task: TASK-040
Tier: 2
Files changed: backend/server.py, backend/routes/core_status.py, TASKS.md
```

---

### TASK-041 — Extract diagnostics route from server.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `437bc1f`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~1 hour
- **Prerequisite**: TASK-040

**Problem**:
`server.py` still owns the position-integrity diagnostic endpoint. Moving it into a diagnostics route module keeps operational troubleshooting APIs separate from startup/runtime wiring.

**Files to touch**: Create `backend/routes/diagnostics.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only `/debug/position-integrity` into `backend/routes/diagnostics.py`.
2. Register the diagnostics router in `server.py`.
3. Do not change endpoint URL, auth, response shape, broker position fetching, strategy position checks, wallet, P&L, or order behavior.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/diagnostics.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract diagnostics route from server.py

Task: TASK-041
Tier: 2
Files changed: backend/server.py, backend/routes/diagnostics.py, TASKS.md
```

---

### TASK-042 — Move system routes from server.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `158ff6c`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~1 hour
- **Prerequisite**: TASK-041

**Problem**:
`server.py` still owns the root, health, and version probes. Moving these into a system route module removes the final simple HTTP route definitions from the startup file without changing public checks.

**Files to touch**: Create `backend/routes/system.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only these endpoints into `backend/routes/system.py`:
   - `/`
   - `/health`
   - `/version`
2. Register the system router in `server.py`.
3. Do not change endpoint URLs, response shapes, version fields, git metadata logic, startup time, live flags, wallet, P&L, or order behavior.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/system.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract system routes from server.py

Task: TASK-042
Tier: 2
Files changed: backend/server.py, backend/routes/system.py, TASKS.md
```

---

### TASK-043 — Reconcile daily reports with canonical fill ledger
- **Status**: `[x]` Completed by Codex
- **Commit**: `1822d8a`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-042

**Problem**:
The 2026-06-18 daily report and raw closed spread positions do not fully agree. Some historical credit-spread exits were closed before spread closes wrote canonical `trade_fills` rows, so daily report generation can miss P&L. The P&L helper also only reads the current trading day, which is unsafe for rebuilding or late-running reports.

**Files to touch**: Edit `backend/core/portfolio_ledger.py`, `backend/position_monitor.py`, and `TASKS.md`. Create `backend/migration/backfill_spread_trade_fills.py`. Add focused tests under `backend/tests/`.

**Exact steps**:
1. Make `get_strategy_pnl_today()` accept an optional `trading_date` and compute the IST day window for that date.
2. Make EOD aggregation pass the actual report date into the P&L helper.
3. Add a dry-run/apply migration that backfills missing canonical `trade_fills` rows for closed credit-spread positions and rebuilds the selected daily report from `trade_fills`.
4. Do not change order creation, live flags, wallet behavior, broker behavior, or position close math.

**How to verify**:
```bash
cd backend
python -m py_compile core/portfolio_ledger.py position_monitor.py migration/backfill_spread_trade_fills.py
python -m pytest tests/test_daily_report_reconciliation.py -v
python -m pytest tests/test_spread_lifecycle.py -v
python -m pytest tests/test_core_logic.py -v
```

**Commit format**:
```
fix: reconcile daily reports with canonical spread fill ledger

Task: TASK-043
Tier: 2
Files changed: backend/core/portfolio_ledger.py, backend/position_monitor.py, backend/migration/backfill_spread_trade_fills.py, backend/tests/test_daily_report_reconciliation.py, TASKS.md
```

---

## PRIORITY 9 — Live-Data Unlock & Strategy Diversification (2026-06-19)

Context: 2026-06-19 session. Root-caused why the book barely traded for days — the Upstox V3 WS feed silently streamed ZERO ticks (wrong subscription mode), so candles ran on lagging REST and the staleness guard blocked nearly every signal. Fixed + verified live. Also diagnosed that all 8 prior live strategies were the same ATM-buy single-leg bet, and added theta-positive credit spreads. Remaining open items below are the "what to do next".

### TASK-044 — Fix Upstox V3 feed subscription mode (full_d5 → full)
- **Status**: `[x]` commit 5b026bb · 2026-06-19
- **Tier**: 3 (Opus / Claude)

**Problem**: `build_subscription_payload` converted feed mode `"full"` → `"full_d5"` before sending the WS subscription. Upstox does not accept `full_d5` as a subscribe mode string — it silently dropped every subscription (connection + `market_info` handshake still succeed, but ZERO data frames stream). Result: `latest_tick()` always empty → `first tick received = 0` for the whole session → candles/ATR fell back to lagging historical REST → staleness guard blocked signals. The ENTIRE live tick feed had been dead for days.

**Fix**: send the documented mode string verbatim (delete the full→full_d5 conversion). Proven by live frame capture (`backend/scratch/capture_feed_frames.py`): `mode="full"` → 8 data frames decode; old `full_d5` → 0. Deployed + verified in prod (`first tick received` fired, `tick cache empty` stopped, strategies now `last_data_reason="websocket tick"`). The decoder was always fine.

**Files changed**: `backend/brokers/upstox_market_data_v3.py`

---

### TASK-045 — Lower theta entry-guard floor 0.08 → 0.05
- **Status**: `[x]` commit 5f6089d · 2026-06-19
- **Tier**: 2

**Problem**: theta entry-guard blocked ~40+ signals on a quiet day (NIFTY/BANKNIFTY ATR% ran 0.054–0.079, just under the 0.08 floor) → only one trade all session.

**Fix**: added `EXIT_THETA_GUARD_ATR_PCT: "${EXIT_THETA_GUARD_ATR_PCT:-0.05}"` to docker-compose.yml. Revert to 0.08 to restore the stricter floor. Trade-off: 0.05 buys options into lower-movement conditions.

**Files changed**: `docker-compose.yml`

---

### TASK-046 — Add theta-positive credit-spread strategies + eval baseline + scheduled rank
- **Status**: `[x]` · 2026-06-19 (DB ops + scheduled agent; no code commit)
- **Tier**: 3

**Problem**: all 8 prior live strategies were `ATM_BUY` + `single_leg` directional BUYING — max theta, low probability-of-profit, 100% correlated, and they sit out range days.

**Done**: created (live, paper) **NIFTY Range Credit Spread** (mean-reversion, EMA-flatness gated so it only fades ranges, not trends), **BANKNIFTY Theta Credit Spread**, **SENSEX Theta Credit Spread**; re-activated **NIFTY Theta Credit Spread**. Each `python_code` validated through the real `safe_run_strategy` sandbox before insert. Stamped `app_config.credit_spread_eval_baseline`; scheduled agent `quantg-credit-spread-weekly-rank` fires **2026-06-26 09:00 IST** to rank sellers vs buyers.

**Artifacts**: `backend/scratch/create_credit_spread_strategies.py`, `backend/scratch/fix_range_strategy.py`

---

### TASK-047 — Feed + token health watchdog/alert
- **Status**: `[x]` · 2026-06-19
- **Tier**: 2 (Sonnet / Codex / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: None

**Problem**: both failure modes that caused this week's outage are SILENT — (a) feed connected-but-no-ticks (the full_d5 bug, hidden for days) and (b) the daily Upstox token expiry. Nothing alerts; you only find out by noticing "no trades".

**Exact steps**:
1. Add a health check (extend a health loop, e.g. `backend/position_guardian.py` or the strategy-health loop) that during market hours flags: `latest_tick()` stale > 3 min for index keys, OR `first tick received` never fired since connect, OR Upstox token invalid.
2. Raise a notification (`notifications` collection) and expose the state via `routes/readiness.py`.
3. Wire it into the existing `ReadinessBanner` (TASK-024) so the UI shows "Live feed stalled — 0 ticks" / "Upstox token expired — reconnect".

**How to verify**: during market hours, force a stale feed (or expired token) → alert fires within a few minutes and the banner shows it.

---

### TASK-048 — Build `debit_spread` structure support, then convert one ATM buyer
- **Status**: `[x]` Completed by Antigravity · 2026-06-19
- **Tier**: 3 (Opus / Claude — touches execution path)
- **Session size**: ~4 hours
- **Prerequisite**: None (do with tests, off-hours)

**Problem**: the engine only supports `single_leg` + `credit_spread`. A debit spread (buy ATM + sell further-OTM) cuts cost basis ~40% and theta bleed while keeping the directional view — but it isn't built, so you can't do the "convert an ATM buyer → debit spread, compare before/after" experiment.

**Exact steps**:
1. Mirror the credit-spread machinery: add `build_debit_spread()` in `core/spread_builder.py`, `value/open/close_debit_spread()` in `core/spread_lifecycle.py`, and `structure == "debit_spread"` handling in `execution_state.py` + `position_monitor.py`.
2. Add `structure: "debit_spread"` support in the strategy `visual_config` path (server.py ~447).
3. Add tests under `backend/tests/` (replay-style, no broker).
4. Convert one ATM buyer (e.g. UPSTOX NIFTY ATM Momentum) to a debit spread and run side-by-side on paper.

**How to verify**: a `debit_spread` strategy opens two legs, values + exits correctly in paper; tests pass.

---

### TASK-049 — Verify live-tick trade frequency + declare clean measurement epoch
- **Status**: `[x]` Completed by Antigravity · 2026-06-19
- **Tier**: 2
- **Session size**: ~1 hour (over 1–2 sessions)
- **Prerequisite**: TASK-044 (done)

**Problem**: all prior P&L is corrupted by the dead feed + old bugs. Now that ticks stream, establish the first trustworthy paper-forward window so ranking/alpha decisions use clean data.

**Exact steps**:
1. Over the next 1–2 full sessions, confirm orders/day rises materially above the pre-fix ~1/day and that the 4 credit spreads actually fire and fill.
2. Set an `app_config` `research_baseline` marker dated to the first full live-tick session (same pattern as the clean-epoch reset).
3. Ensure the 2026-06-26 ranking (TASK-046) measures from this clean window.

**How to verify**: orders/day clearly > pre-fix; baseline recorded; no staleness-guard rejections from feed lag.

---

### TASK-050 — Post-ranking: trim losers, promote winners
- **Status**: `[x]` Completed by Antigravity · 2026-06-19
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: TASK-046 (scheduled rank), TASK-049

**Problem**: the live set is buyer-heavy and several buyers are deep losers (cumulative, pre-clean-epoch): UPSTOX NIFTY ATM Momentum −9.7k, NIFTY Quick EMA −5.7k, UPSTOX BANKNIFTY ATM Breakout −4.1k.

**Exact steps**: after the ranking runs, pause/trim the worst single-leg buyers if the credit spreads outperform on clean-window realized P&L + win-rate; keep the live set small and focused; do NOT enable real-money live (`CORE_ENGINE_LIVE_ENABLED`) until ≥1 strategy shows out-of-sample edge.

---

## PRIORITY 10 — Prove Alpha (measure edge with data)

Context: the bottleneck is no longer engineering — it's proving a strategy actually has edge. Built the toolkit to measure it; remaining items feed it better data and surface it.

### TASK-051 — Option-priced backtester + risk-adjusted scorecard
- **Status**: `[x]` commit (this session) · 2026-06-19
- **Tier**: 3 (Opus / Claude)

**Problem**: the old `core/backtest_engine.py` prices trades on the UNDERLYING index — it ignores option premium, theta, and spreads, overstating option P&L by ~1/delta. And there was no risk-adjusted view (Sharpe/Sortino/expectancy) of realized trades. So "does this strategy have edge?" was unanswerable.

**Done**: `core/metrics.py` (pure metrics, 8 unit tests pass), `core/options_backtest.py` (option-PRICED backtest over `db.historical_chains`, real CE/PE bid/ask incl. theta + spread cost, supports single_leg/credit_spread/debit_spread), `core/strategy_scorecard.py` (risk-adjusted ranking from real `db.trades`). Endpoints `GET /ops/risk-scorecard`, `POST /ops/options-backtest`. Runner `backend/scratch/analyze_strategies.py`. **Finding**: options buyers are negative-edge grade F; only equity momentum (LT, AXISBANK) score A/B (small samples); no robust positive edge yet.

**Files**: backend/core/metrics.py, backend/core/options_backtest.py, backend/core/strategy_scorecard.py, backend/routes/ops.py, backend/tests/test_metrics.py

---

### TASK-052 — Feed real underlying OHLC to the options backtester
- **Status**: `[x]` DONE (this session) · 2026-06-19
- **Tier**: 2
- **Prerequisite**: TASK-051

**Problem**: the backtester derives its candle series from chain-snapshot `spot` (flat o=h=l=c, ~5min), so breakout/range strategies under-fire (0 trades) and signal fidelity is low.

**Done**: new `core/candle_store.py` backfills REAL 5-min underlying OHLC (Upstox V3 historical + intraday, index keys) into `db.candles` keyed by IST minute; `OptionsBacktestEngine.run` now loads `db.candles` for SIGNALS and prices legs from the nearest `historical_chains` snapshot (`_nearest_snap`, ≤10-min, no look-ahead), falling back to flat chain-spot when no real OHLC. SENSEX added to the chain collector + daily 16:00 IST candle backfill in the scheduler. `POST /ops/backfill-candles` route. Backfilled NIFTY/BANKNIFTY/SENSEX 30d (1650 bars each).

**Verify (passed)**: breakout fires **10 trades** (was 0 on flat candles); `candle_source=real_ohlc`; signals_in_window reported. **Data constraint**: option pricing is still bounded by live chain history = 3 days (06-17..06-19); the Upstox expired-instrument historical-candle API is unavailable on this account (all 404, `expired_instrument_key` null), so no real option premiums beyond the live snapshots.

**Search result (next-step ask)**: with real OHLC, EMA-cross and breakout momentum on NIFTY/BANKNIFTY grade **A** on the real chain-priced sample — most credible: NIFTY breakout SL11/7 te30 (10 tr, PF 3.07, +₹3.5k) and the EXISTING live **NIFTY Quick EMA Scalper** (11 tr, A, PF 2.75). Caveat: only ~3 priceable days → promising, NOT proven; option SELLERS (theta credit spreads) graded F over this trending window. The binding constraint is chain history, which the daily collector now accumulates.

**Files**: backend/core/candle_store.py, backend/core/options_backtest.py, backend/routes/ops.py, backend/server.py, backend/scratch/search_strategies.py

---

### TASK-053 — Analytics dashboard UI (scorecard + equity curves)
- **Status**: `[x]` DONE (this session) · 2026-06-19
- **Tier**: 2 (frontend)
- **Prerequisite**: TASK-051

**Problem**: the risk-adjusted scorecard + backtest results are API-only; no UI. The founder can't see Sharpe/Sortino/expectancy/equity-curve per strategy at a glance.

**Done**: new `frontend/src/pages/Analytics.jsx` at `/analytics` (nav "Analytics" under Trading). Two tabs — Realized (`GET /ops/risk-scorecard`) and Option-priced backtest (`POST /ops/options-backtest`). Sortable scorecard table (grade chips A–F, Sharpe/Sortino/PF/expectancy/maxDD/trades/win%/P&L), REAL equity-curve sparklines from each strategy's equity_curve, and buyers-vs-sellers structure summary cards. Deployed + verified (bundle chunk 749 contains the page, nav present, endpoint live, site 200).

**Also (frontend "fix everything" pass)**: audited every page; removed data-theater — Layout fake hardcoded Sparkline → honest P&L trend glyph; OpsConsole/BrokerStatusPanel fabricated `Math.random` "Order Latency" → real last-tick metric; hardcoded "Sync Interval 4.0s" → 15s; deleted dead `TelemetryMetric`; Dashboard `endsWith("CE")` option-detection bug → robust spaced-token/exchange check.

**Files**: frontend/src/pages/Analytics.jsx, frontend/src/App.js, frontend/src/components/Layout.jsx, frontend/src/pages/Dashboard.jsx, frontend/src/pages/OpsConsole.jsx, frontend/src/components/ops/BrokerStatusPanel.jsx

---

## PRIORITY 11 — Hermes Agent Integration (Operator + Research Assistant)

Context: Hermes becomes a QuantG operator/research assistant, NOT the trading brain. QuantG stays the source of truth for execution/P&L/readiness/broker/strategy state and all live gates. **Key fact (verified 2026-06-20):** the in-app read-only "Ask QuantG Agent" ALREADY EXISTS (`backend/routes/ai.py` — `agent_router`, `READ_ONLY_AGENT_TOOLS` 8 tools, `_run_agent_tool`, local fallback, `PROPOSED_ACTION` draft/approve seed; UI `frontend/src/pages/AIBot.jsx`). So Hermes is a **rebrand + extension**, not greenfield — Stages 2 & 4 are ~80% built. Founder decisions 2026-06-20: (1) **design-doc first**, then code; (2) **read-only now**, approval-gated **non-trading** writes later (Stage 7). Design + safety policy live in `wiki/Projects/Hermes Integration Design Doc.md` and `wiki/Projects/Hermes Agent Integration Roadmap.md`. Rule for every Hermes feature: **wiki = context; DB/orders/fills/readiness = truth.** Security boundary = the read-only tool allowlist — NEVER register a mutating tool. Permanently forbidden at all stages: place/cancel/modify/exit trades, enable live trading, change broker creds, change strategy/risk/capital settings, direct mutation of trading collections.

### Stage 0 — Design & Safety Contract

### TASK-H001 — Hermes integration design doc + safety policy
- **Status**: `[x]` 2026-06-20 — `wiki/Projects/Hermes Integration Design Doc.md` (folds in TASK-H002 safety policy)
- **Tier**: 2

**Done**: design doc grounded in a live read of `routes/ai.py`; maps existing agent → roadmap stages; defines tool-envelope contract (+ stale/confidence/warnings), the read-only safety policy, and the Stage-7 approval-gated non-trading write end state. Folds in TASK-H002.

### TASK-H002 — Hermes safety policy (allowed tools / forbidden actions / audit / rollout gates)
- **Status**: `[x]` 2026-06-20 — folded into the TASK-H001 design doc (§5 Safety policy)
- **Tier**: 2

---

### Stage 2 — Read-Only Tool Gateway (extend existing agent — START HERE for code)

### TASK-H005 — Finalize read-only tool schema (envelope + stale/confidence/warnings)
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H001

**Problem**: the current `_run_agent_tool` envelope (`routes/ai.py:62`) carries name/status/timestamps but not the roadmap-required `source`, `stale`, `confidence`, `warnings`, `user/account`. **Steps**: add those fields to every tool response; document the final schema in the design doc. Additive, read-only, no behavior change.

### TASK-H006 — Wire the missing Stage-2 read-only tools into READ_ONLY_AGENT_TOOLS
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H005

**Problem**: roadmap wants 12 tools; 8 exist. Most missing ones just WRAP endpoints/functions that already exist. **Add**: `get_live_readiness` (→ `/ops/live-readiness` ops.py:1091), `get_strategy_scorecard` (→ `/ops/risk-scorecard` ops.py:131), `get_backtest_summary` (→ `/ops/options-backtest` ops.py:147), `get_today_fills` (→ `db.trade_fills`), `get_skipped_signals` (→ `db.signals` FILTERED/SKIPPED), `get_daily_report` (→ `daily_strategy_reporter`), `search_wiki` (→ Knowledge Hub collection), `get_recent_alerts` (→ `notifications`), and alias `get_feed_status`/`get_token_status` to the existing `get_market_data_status`/`get_upstox_status`. **Verify**: each new tool returns a proper envelope; no mutating tool added; agent answers cite the new sources.

### TASK-H007 — Agent tool audit log (`agent_tool_audit` collection)
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H006

**Problem**: tool calls aren't persistently audited (also the SEBI audit-trail seed). **Steps**: log every `_run_agent_tool` call — name, user, timestamp, args, status — to `agent_tool_audit`; best-effort (never block the agent). **Verify**: each agent question writes one audit row per tool used.

---

### Stage 1 / 3 — Sidecar & Runbook (infra; needs founder runtime/channel inputs)

### TASK-H003 — Install Hermes in an isolated environment (sidecar)
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 3

### TASK-H004 — Hermes deployment runbook
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H003

---

### Stage 3 — Daily Operator Reports

### TASK-H008 — Market-Open Readiness Report
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H006

Identify token expiry, feed stalls, strategies-armed count, readiness changes at/just-before open. Reuses `get_live_readiness`/`get_token_status`/`get_feed_status`. Overlaps with TASK-024 ReadinessBanner + TASK-047 watchdog — reuse, don't duplicate.

### TASK-H009 — Intraday Health Watch
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H006

Periodic check for feed stalls, no-trade drought, abnormal loss, stale positions. Reuse the TASK-047 watchdog signals.

### TASK-H010 — EOD Trading Report
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H006

End-of-day P&L, per-strategy performance, no-trade/abnormal-loss flags. Reuse `daily_strategy_reporter` + `daily_reports` (TASK-012).

---

### Stage 4 — In-App Hermes Analyst (extend existing AIBot UI)

### TASK-H011 — Add "Hermes mode" to Ask QuantG Agent
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H006

Label/brand the existing agent as Hermes; system prompt enforces "cite tool output or say unsure". UI surface = existing `AIBot.jsx` + `components/aibot/`.

### TASK-H012 — Source cards on agent answers (cited tool outputs)
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H011

Render each cited tool's source, timestamp, stale-data warning, and confidence as a card under the answer.

---

### Stage 5 — QuantG Hermes Skill Pack

### TASK-H013 — Create QuantG Hermes skill pack
- **Status**: `[x]` commit 5da3ca4 · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H006

Skills: `quantg-live-readiness`, `quantg-why-no-trade`, `quantg-strategy-loss-review`, `quantg-feed-token-diagnosis`, `quantg-eod-report`, `quantg-backtest-review`, `quantg-vps-deploy-check`, `quantg-incident-postmortem`. Each composes read-only tools; wiki=context, DB=truth.

### TASK-H014 — Sync selected wiki context with Hermes
- **Status**: `[x]` commit 5da3ca4 · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H013

Expose Knowledge Hub notes as retrievable context (`search_wiki`), clearly tagged context-not-truth.

---

### Stage 6 — Strategy Research Assistant

### TASK-H015 — Weekly Strategy Ranking Report
- **Status**: `[x]` commit 5da3ca4 · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H006

Reuse the existing scorecard + the scheduled `quantg-credit-spread-weekly-rank` agent (TASK-046).

### TASK-H016 — Backtest Experiment Generator
- **Status**: `[x]` commit 5da3ca4 · 2026-06-20
- **Tier**: 3
- **Prerequisite**: TASK-H015

Propose backtest experiments over `POST /ops/options-backtest`; draft-only.

### TASK-H017 — Strategy Experiment Ledger
- **Status**: `[x]` commit 5da3ca4 · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H016

Track every hypothesis, version, clean baseline, result, decision, reason.

---

### Stage 7 — Approval-Gated Operations (NON-TRADING writes only — founder-approved end state)

**Founder GO 2026-06-20: Stage 7 is approved and is the recommended next code priority.** This is the highest-leverage Hermes work — the `PROPOSED_ACTION → approve/reject` machinery already half-exists (`routes/ai.py:740` `approve_agent_action`, but it only handles 6 *profile* fields). Stage 7 generalizes that into a typed draft → queue → approved-executor pipeline for **non-trading** writes. Permanently forbidden at every step: live order place/cancel/modify/exit, live-mode enable (`CORE_ENGINE_LIVE_ENABLED`), broker-credential changes, risk/capital overrides. The approval executor is an **allowlist of action_types**, never a denylist.

### TASK-H018 — Draft-only operation framework (typed action proposals)
- **Status**: `[x]` commit e8d20d7 · 2026-06-20
- **Tier**: 3
- **Prerequisite**: TASK-H007
- **Session size**: ~2–3 hours

**Problem**: today the only proposable action is `update_profile` (6 risk/sizing fields) parsed from a `PROPOSED_ACTION:` JSON block (`_parse_and_store_pending_action`, `routes/ai.py:427`). Stage 7 needs Hermes to also draft **non-trading content writes** (wiki note, TASKS.md entry, incident report, PR summary) as pending drafts that never auto-apply.

**Files to touch**: `backend/routes/ai.py`.

**Exact steps**:
1. Extend the Gemini system prompt (`_gemini_agent_reply_sync`, `routes/ai.py:465`) to allow these NEW `PROPOSED_ACTION` action_types alongside `update_profile`:
   - `draft_wiki_note` — params: `{title, body_markdown, folder}` (folder ∈ existing `wiki/` subdirs)
   - `draft_task_entry` — params: `{task_id, title, body_markdown}`
   - `draft_incident_report` — params: `{title, body_markdown}`
   - `draft_pr_summary` — params: `{title, body_markdown}`
2. Keep the parse path generic: `_parse_and_store_pending_action` already stores `{action_type, params, status:"pending"}` in `db.pending_actions` — confirm it stores these new types unchanged (it should; it is action-type agnostic).
3. Do NOT add an executor here — H018 only produces pending drafts. Approval/execution is H019/H020.
4. Hard rule in the prompt: Hermes may NEVER propose a trading/broker/live/risk action; those stay in the deterministic app only.

**Verify**: ask Hermes "draft a wiki note summarizing today's feed outage" → response stores a `db.pending_actions` row with `action_type:"draft_wiki_note"`, `status:"pending"`, and the chat returns a `pending_action` envelope. No file is written yet.

### TASK-H019 — Founder approval queue (in-app)
- **Status**: `[x]` commit e8d20d7 · 2026-06-20
- **Tier**: 3
- **Prerequisite**: TASK-H018
- **Session size**: ~3 hours

**Problem**: pending drafts exist in `db.pending_actions` but there is no dedicated UI to review/approve/reject them — approval is currently inline per chat message only, and only for `update_profile`. A founder needs one queue showing all pending Hermes drafts.

**Files to touch**: `backend/routes/ai.py` (list endpoint), `frontend/src/pages/AIBot.jsx` + `frontend/src/components/aibot/` (queue panel).

**Exact steps**:
1. Add `GET /agent/actions/pending` → returns all `db.pending_actions` for the user with `status:"pending"` (newest first), projecting `action_id, action_type, params, created_at`.
2. Reuse the existing `POST /agent/action/approve` and `/agent/action/reject` (`routes/ai.py:740/796`) — they already flip status; H020 adds the executor branch for the new action_types.
3. Frontend: add an "Approvals" panel/tab in the Agent page listing pending drafts as cards (title, type chip, preview, Approve/Reject buttons). On approve/reject, call the existing endpoints and refresh.
4. Show a badge count of pending drafts in the Agent nav.

**Verify**: a drafted wiki note from H018 appears as a card in the Approvals panel; Reject sets `status:"rejected"`; the card disappears on refresh. (Approve wiring to an executor is H020.)

### TASK-H020 — Safe (non-trading) mutation framework (approved-only executors)
- **Status**: `[x]` commit e8d20d7 · 2026-06-20
- **Tier**: 3
- **Prerequisite**: TASK-H019
- **Session size**: ~3–4 hours

**Problem**: approving a non-`update_profile` draft currently does nothing — `approve_agent_action` only has an `update_profile` branch. H020 adds the guarded executors.

**Files to touch**: `backend/routes/ai.py` (executor branch), possibly a small `backend/core/hermes_writer.py` helper.

**Exact steps**:
1. In `approve_agent_action` (`routes/ai.py:740`), add a dispatch on `action_type` AFTER the existing `update_profile` branch:
   - `draft_wiki_note` → write a markdown file under `wiki/<folder>/<slug>.md` with frontmatter (topic/tags/date) AND upsert the Knowledge Hub Mongo doc, so it syncs both ways (mirror how the Wiki feature writes).
   - `draft_task_entry` → append a task block to `TASKS.md` (never rewrite existing tasks).
   - `draft_incident_report` → write `wiki/Incidents/<date>-<slug>.md` + Hub doc.
   - `draft_pr_summary` → store the summary text on the action doc (no git action; founder copies it).
2. **Allowlist enforcement**: if `action_type` is not in the explicit approved set, raise 400 — never fall through to a generic write. Add an assert that the action_type is NOT any trading/broker/live/risk keyword.
3. Audit: write each executed approval to `agent_tool_audit` (or a new `hermes_writes` collection) with who/what/when/the resulting path.
4. STILL forbidden (must have no code path): live order place/cancel/modify/exit, `CORE_ENGINE_LIVE_ENABLED`, broker-cred changes, strategy/risk/capital mutation.

**Verify**: approve a `draft_wiki_note` → the markdown file appears under `wiki/`, the Hub doc exists, and an audit row records the write. Approving an unknown action_type returns 400.

---

### Stage 8 — Incident Commander

### TASK-H021 — Automated incident timeline
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Commit**: `16dd5fe`
- **Tier**: 3
- **Prerequisite**: TASK-H007 (audit log), TASK-H009

Reconstruct what happened/when/which strategies/whether trading stayed gated from `agent_tool_audit` + `core_events` + notifications.

### TASK-H022 — Postmortem generator
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Commit**: `16dd5fe`
- **Tier**: 3
- **Prerequisite**: TASK-H021

Draft a postmortem (evidence-cited) into the wiki via the Stage-7 approval gate.

---

### Stage 9 — Two-Way Telegram & Proactive Alerts (founder-chosen 2026-06-20)

Context & founder decisions 2026-06-20: (1) **build a two-way Telegram system** — today the sidecar (`hermes/agent.py`) only PUSHES (watchdog/pre-market/EOD); founder wants to also ASK Hermes from Telegram. (2) **Stay on Gemini 2.5-flash for now** (`DEFAULT_GEMINI_MODEL`, `routes/ai.py:21`) — revisit model choice later; do NOT introduce a new provider in these tasks. These tasks close the two gaps the roadmap (H001–H022) never covered: the chat channel beyond in-app, and proactive (behavioral, not just plumbing) alerting. Same safety contract applies — Telegram is a READ-ONLY question channel; any write still goes through the Stage-7 approval queue in-app, never auto-applied from chat.

### TASK-H023 — Two-way Telegram command bridge
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Commit**: `4ea022b`
- **Tier**: 3
- **Prerequisite**: TASK-H006 (read-only tools), TASK-H009 (sidecar loop)
- **Session size**: ~3–4 hours

**Problem**: `hermes/agent.py` is push-only. The founder reconnects Upstox every morning and wants to query state ("/status", "/pnl", "/why-no-trade", or free text) from the phone without opening the app.

**Files to touch**: `hermes/agent.py` (Telegram long-poll + command router), reuse the in-app agent endpoint.

**Exact steps**:
1. Add a Telegram `getUpdates` long-poll loop (offset-tracked) alongside the existing time-based push loop in `run_loop()`. Keep it best-effort; never let a Telegram error kill the watchdog.
2. Authorize ONLY the configured `TELEGRAM_CHAT_ID` — ignore messages from any other chat id (prevents strangers querying account state).
3. Command router:
   - `/status` → call `GET /core/feed-status` + `/trading/live-readiness`, format like the pre-market report.
   - `/pnl` → call `GET /reports/daily/<today>` (or risk snapshot), format like the EOD summary.
   - `/why` or free text → POST the text to `POST /agent/chat` (the existing Hermes read-only agent) and relay `content` back, including a one-line "sources: <tools_used>" footer.
4. Reuse `QuantGClient` (already logs in as operator and auto-reauths). All queries are READ-ONLY — the bridge must NEVER call a mutating endpoint.
5. If a reply would contain a `PROPOSED_ACTION`, strip it and tell the user "approve in-app" — Telegram cannot approve writes (keeps the Stage-7 gate in one place).

**Verify**: send `/status` from the authorized chat → get a formatted readiness reply; send "why didn't NIFTY trade today" → get the agent's grounded answer with a sources footer; send from an unauthorized chat id → no reply.

### TASK-H024 — Proactive behavioral alerts (drought / drawdown / loss-streak)
- **Status**: `[x]` commit 1243636 · 2026-06-20
- **Tier**: 2
- **Prerequisite**: TASK-H009
- **Session size**: ~2 hours

**Problem**: the intraday watchdog (TASK-H009) only watches plumbing (feed/token). It never alerts on *trading behavior* — the trade-drought and oversizing/drawdown classes the founder has had to catch manually.

**Files to touch**: `hermes/agent.py` (extend `run_watchdog` or add `run_behavior_watch`).

**Exact steps**:
1. During market hours, periodically pull read-only state (`/reports/daily/<today>` or risk snapshot + today's fills via the agent tools).
2. Alert (rate-limited, one per type per cooldown, reuse `should_rate_limit`) on:
   - **No-trade drought**: 0 fills by a configurable cutoff (e.g. 12:00 IST) while feed is healthy and ≥1 strategy armed.
   - **Drawdown breach**: day P&L below a configurable fraction of `max_daily_loss`.
   - **Loss streak**: any strategy hits N consecutive losing closes (reuse `strategy_loss_streaks` if exposed; else derive from fills).
3. Thresholds via `.env.hermes` (e.g. `DROUGHT_CUTOFF_IST`, `DRAWDOWN_ALERT_FRAC`, `LOSS_STREAK_N`), with safe defaults.

**Verify**: with 0 fills past the cutoff on a healthy feed, a single drought alert fires; it does not repeat within the cooldown.

### TASK-H025 — Document two-way Telegram in DEPLOY_HERMES.md
- **Status**: `[x]` Completed by Antigravity · 2026-06-20
- **Commit**: `4ea022b`
- **Tier**: 1
- **Prerequisite**: TASK-H023
- **Session size**: ~30 min

Update `docs/DEPLOY_HERMES.md` + `.env.hermes.example` with the new env vars (drought/drawdown thresholds), the authorized-chat-id requirement, and the supported `/status` `/pnl` `/why` commands. Do not create a new doc file.

---

## PRIORITY 12 — Hermes Second-Brain Campaign (2026-06-22)

**Status: BACKLOG — do NOT start until the PRIORITY 0 Win-Rate & Expectancy campaign settles.**
Money-correctness (win-rate fixes, clean measurement epoch) comes first; this campaign must not muddy that
measurement window. Pick up once Win-Rate Phase 1 validation is stable.

**Source**: Design session 2026-06-22 (extends the existing Hermes program H001–H025 and folds in the strategy
AutoResearch ratchet from [[project_autoresearch]] Phases 2–5). Founder decisions this session:
(1) RAG + prompt engineering, **no model fine-tuning** (no GPU; stays on Gemini 2.5-flash);
(2) web context via **Gemini Google-Search grounding** (no new search API key);
(3) **AI score = quant-grounded, LLM-narrated** — deterministic math computes every number, LLM only explains;
(4) the strategy ratchet is **folded into this campaign** (one unified plan), judge-first;
(5) this campaign is **lower priority** than the active Win-Rate work.

### Design law (non-negotiable, applies to EVERY task below)
> **The LLM narrates; deterministic code computes.** Every number (P&L, score, win-rate) originates in code over
> real `db.trade_fills` / backtest data. Web/Gemini-grounding context is ALWAYS tagged `external/unverified` and can
> never enter a numeric claim. Every answer carries source + confidence (the envelope `_run_agent_tool` already returns).

### Permanent safety boundary (carried from H-series)
Hermes **cannot edit application code** and **cannot place/cancel/modify trades**, flip `CORE_ENGINE_LIVE_ENABLED`,
or touch broker keys. Its ONLY write path stays the approval-gated `draft_*` action framework (H018–H020). New
governed actions added here (`draft_strategy_pause`, `draft_experiment`) are non-trade status/candidate writes that
still require in-app founder approval and never auto-execute. Code changes still route through Claude Code + review.

### Architecture — three rings
- **Truth ring (deterministic, mostly built):** `core/strategy_scorecard.py`, `core/metrics.py`, `core/options_backtest.py`, `ops_live_readiness`, real `trade_fills`.
- **Memory ring (the upgrade):** episodic (daily history) + semantic (wiki/Decisions) + recall (vector retrieval over `db.hermes_memory`).
- **Reasoning ring (Gemini 2.5-flash):** query→playbook router, narrate + cite + confidence, bounded Google-Search grounding at the edge.

### Critical path (judge-first — DO NOT build the proposer before the judge exists)
HSB-01/02 (truth+routing) → HSB-03/04/05 (memory) → HSB-07/08/09 (advisor + self-improvement loop) →
HSB-11 (data audit) → **HSB-12 (OOS judge) → HSB-13 (ratchet gate) → HSB-14 (paper-forward)** → HSB-15 (Hermes proposer) → HSB-16 (unify).
**WARNING from [[project_autoresearch]]: in-sample backtest Sharpe LIES. The ratchet MUST clamp on out-of-sample /
walk-forward / paper-forward, never in-sample. Letting the proposer (HSB-15) run before the judge (HSB-12/13) builds
a confident overfitting machine — that is the single worst outcome and is explicitly forbidden by sequencing.**

---

### Phase A — Truth & Grounding (read-only, low risk)

*Completed and verified.*

### Phase B — Memory Ring

*Completed and verified.*

### Phase C — Web Context  ✅ DONE (see Completed Tasks)

### Phase D — Advisor Behavior + Self-Improvement Loop  ✅ DONE (see Completed Tasks)

### Phase E — Strategy AutoResearch Ratchet (folded in; judge-first)

> **⚠ SUPERSEDED 2026-07-02 (dedupe): HSB-12..16 are re-framed as HSI Stages 4–5 (HSI-41..54, bottom of file) — build there, extend don't fork (the HSI section itself says so). Only `HSB-11` (historical_chains data audit — genuine prerequisite for ANY OOS backtest, do it before HSI-42) and `HSB-17` (dead-action cleanup) remain independently actionable in this section.**

| HSB-11 | `db.historical_chains` hardening: add capped/TTL index (flagged missing in [[project_autoresearch]]) + audit actual accumulated data volume (days × strikes). **Gating check: confirms whether HSB-12 walk-forward is buildable now or still data-collection.** | `backend/server.py`, Mongo index |
| HSB-12 | **Phase 2 — THE JUDGE:** walk-forward backtester over `db.historical_chains` with train/test split, **deflated-Sharpe + complexity penalty** (clamps on OOS, never in-sample). Pure deterministic, no LLM. Everything else depends on this. | new `backend/core/walkforward.py` |
| HSB-13 | **Phase 3 — THE GATE:** ratchet loop `propose-config → OOS backtest (HSB-12) → keep iff beats incumbent → log to db.experiments`. | `backend/core/`, `db.experiments` |
| HSB-14 | **Phase 4 — REALITY CHECK:** paper-forward promotion gate — an OOS winner must also survive N days live-paper before it joins the live set. | backend promotion job |
| HSB-15 | **Phase 5 — Hermes proposer:** governed action `draft_experiment` — Gemini proposes config changes BOUNDED to the config schema; every proposal enters the HSB-13 ratchet as a candidate, NEVER production. Only safe after HSB-12/13/14 exist. | `routes/ai.py` action framework |

### Phase F — Unify

| HSB-16 | Unify into ONE ratchet engine pointed at two targets: (a) strategy config (HSB-13 experiments) and (b) Hermes's own advice/playbooks (HSB-08/09 recommendation outcomes). Both distill accepted/rejected verdicts into `db.hermes_memory`, so the brain self-improves across trading AND its own advice. | backend |

### Cleanup (opportunistic)

| HSB-17 | Wire or remove the dead `draft_pr_summary` action (currently validates then no-ops on approval in `approve_agent_action`). | `routes/ai.py` |

---

## Completed Tasks

*(Move tasks here when done — include commit hash)*

| Task | Description | Commit | Date |
|---|---|---|---|
| TASK-001 | Fix strategy limits enforcement (cooldown + max_trades_day) | 37c25dc | 2026-06-11 |
| TASK-002 | Block duplicate exit orders before order creation | 6fd78cc | 2026-06-11 |
| TASK-003 | Enforce option quality gate in signal_manager dispatch | 6415dba | 2026-06-11 |
| TASK-004 | Add backend/config.py centralising all tunable constants | 6415dba | 2026-06-11 |
| TASK-005 | AGENT_ROUTER.md symptom-to-file decision tree | 6f61247 | 2026-06-11 |
| TASK-006 | 23 broker-free unit tests in test_core_logic.py | 6f61247 | 2026-06-11 |
| TASK-007 | Canonical get_strategy_pnl_today() in portfolio_ledger | 6f61247 | 2026-06-11 |
| TASK-021 | Implement live readiness and paper trading audit fixes | b38e5c0 | 2026-06-15 |
| TASK-022 | Redesign Strategies UI, relocate test action to About modal | 43d4dd9 | 2026-06-15 |
| TASK-023 | Friendly labels for Phase 2 exit/skip reasons | 86705ba | 2026-06-16 |
| TASK-024 | "Why isn't it trading?" readiness banner | ff7f411 | 2026-06-16 |
| TASK-025 | Greeks (δ/θ/IV) on positions | b8f7f70 | 2026-06-16 |
| TASK-026 | Expandable spread leg detail on Positions | 362f731 | 2026-06-16 |
| TASK-027 | Phase 2 feature-flag status panel | f872522 | 2026-06-16 |
| TASK-028 | Structure badge on strategy cards | 63b9829 | 2026-06-16 |
| TASK-029 | IV-regime visual gauge | 6c15010 | 2026-06-16 |
| TASK-030 | Per-strategy trade-cap indicator | 63b9829 | 2026-06-16 |
| TASK-044 | Fix Upstox V3 feed mode full_d5→full (live ticks finally stream) | 5b026bb | 2026-06-19 |
| TASK-045 | Lower theta entry-guard floor 0.08→0.05 | 5f6089d | 2026-06-19 |
| TASK-046 | Add 4 theta-positive credit spreads + baseline + scheduled rank | (db ops) | 2026-06-19 |
| TASK-047 | Feed + token health watchdog/alert | (multiple) | 2026-06-19 |
| TASK-048 | Build `debit_spread` structure support, then convert one ATM buyer | 85a81ce | 2026-06-19 |
| TASK-049 | Verify live-tick trade frequency + declare clean measurement epoch | 85a81ce | 2026-06-19 |
| TASK-050 | Post-ranking: trim losers, promote winners | 85a81ce | 2026-06-19 |
| TASK-051 | Option-priced backtester + risk-adjusted scorecard | (this session) | 2026-06-19 |
| TASK-052 | Real underlying OHLC → options backtester (db.candles) + SENSEX collector | (this session) | 2026-06-19 |
| TASK-053 | Analytics dashboard UI (scorecard + equity curves) + frontend data-theater fixes | baaef5c | 2026-06-19 |
| TASK-H001 | Hermes integration design doc + safety policy (wiki) | (wiki) | 2026-06-20 |
| TASK-H002 | Hermes safety policy (folded into H001 §5) | (wiki) | 2026-06-20 |
| TASK-H005 | Finalize read-only tool schema (envelope + stale/confidence/warnings) | 0b29554 | 2026-06-20 |
| TASK-H006 | Wire the missing Stage-2 read-only tools into READ_ONLY_AGENT_TOOLS | 0b29554 | 2026-06-20 |
| TASK-H007 | Agent tool audit log (agent_tool_audit collection) | 0b29554 | 2026-06-20 |
| TASK-H011 | Add "Hermes mode" branding and updated system prompt rules in ai.py and AIBot.jsx | 0b29554 | 2026-06-20 |
| TASK-H012 | Save expanded tool metrics in ai.py and render cited tool source cards in ChatFeed.jsx | 0b29554 | 2026-06-20 |
| TASK-H003 | Install Hermes in an isolated environment (sidecar setup, agent client) | 8a4f501 | 2026-06-20 |
| TASK-H004 | Hermes deployment runbook (docs/DEPLOY_HERMES.md) | 8a4f501 | 2026-06-20 |
| TASK-H008 | Market-Open Readiness Report scheduler and messaging logic | 8a4f501 | 2026-06-20 |
| TASK-H009 | Intraday Watchdog loop and alert rate-limiting | 8a4f501 | 2026-06-20 |
| TASK-H010 | EOD Report scheduler and messaging logic | 8a4f501 | 2026-06-20 |
| TASK-H023 | Two-way Telegram command bridge in the Hermes sidecar daemon | 4ea022b | 2026-06-20 |
| TASK-H025 | Document two-way Telegram command capabilities in DEPLOY_HERMES.md and .env.hermes.example | 4ea022b | 2026-06-20 |
| TASK-H013 | Create QuantG Hermes skill pack defining 8 operator playbooks | 5da3ca4 | 2026-06-20 |
| TASK-H014 | Sync selected wiki context dynamically prior to search_wiki query | 5da3ca4 | 2026-06-20 |
| TASK-H015 | Weekly Strategy Scorecard Ranking Report scheduled trigger | 5da3ca4 | 2026-06-20 |
| TASK-H016 | Parameterized Backtest Experiment query processor | 5da3ca4 | 2026-06-20 |
| TASK-H017 | Seed the Strategy Experiment Ledger markdown page | 5da3ca4 | 2026-06-20 |
| TASK-H018 | Draft-only operation framework (typed action proposals) | e8d20d7 | 2026-06-20 |
| TASK-H019 | Founder approval queue (in-app) | e8d20d7 | 2026-06-20 |
| TASK-H020 | Safe (non-trading) mutation framework (approved-only executors) | e8d20d7 | 2026-06-20 |
| TASK-H021 | Automated incident timeline | 16dd5fe | 2026-06-20 |
| TASK-H022 | Postmortem generator | 16dd5fe | 2026-06-20 |
| TASK-H024 | Proactive behavioral alerts (drought / drawdown / loss-streak) | 1243636 | 2026-06-20 |
| HSB-01 | `get_strategy_score_explained` read-only tool with threshold warning | be8d7d4 | 2026-06-22 |
| HSB-02 | Query-aware playbook router filtering executed tools | be8d7d4 | 2026-06-22 |
| HSB-03 | Episodic memory tool `get_historical_context(days=N)` | feabc9d | 2026-06-22 |
| HSB-04 | Recall layer: `db.hermes_memory` and `recall_memory` tool | feabc9d | 2026-06-22 |
| HSB-05 | Auto-memory: daily distillation compiler to `db.hermes_memory` and wiki notes | feabc9d | 2026-06-22 |
| HSB-06 | `get_external_context` via Gemini Google-Search grounding (EXTERNAL/UNVERIFIED, router-gated to macro/news) | (uncommitted) | 2026-06-23 |
| HSB-07 | Proactive prioritized morning briefing (ranked top-3) + `/brief` Telegram command | (uncommitted) | 2026-06-23 |
| HSB-08 | Recommendation ledger `db.hermes_recommendations` (RECOMMENDATION block parser) | (uncommitted) | 2026-06-23 |
| HSB-09 | Outcome scorer EOD job: grades recs vs real fills → `db.hermes_memory` + rolling hit-rate | (uncommitted) | 2026-06-23 |
| HSB-10 | Governed action `draft_strategy_pause` (approval-gated, non-trade status flip) | (uncommitted) | 2026-06-23 |

---

*Last updated: 2026-07-03 (HSI-41..44 OOS judge-first validator shipped; WR-74 Analytics upgraded; OPS-03 fixed as scheduler-adopted.)*
*Open after dedupe: AR-07/AR-08 data-gated · OPS-02 market-hours recheck / OPS-04 · WR-33 deferred / WR-45 / WR-51 / WR-54 ⛔ / WR-71 ⛔ / WR-73 founder-gate · HSI-51..54 founder-gated · HSB-11 + HSB-17. In progress: 0.*
*Founder decisions 2026-06-22 (Hermes Second-Brain): (1) RAG + prompt engineering, no model fine-tuning, stay on Gemini 2.5-flash; (2) web via Gemini Google-Search grounding; (3) AI score = quant-grounded, LLM-narrated; (4) strategy AutoResearch ratchet folded into this campaign, judge-first; (5) lower priority than Win-Rate.*
*Recommended next build order: AR-05 → AR-04/EQ-04/AR-06 → OPS-02 recheck → AR-08 checkpoint → AR-07 + HSB-11 → HSI-41..44 → HSI-51..54 / WR-51 / WR-54 / WR-73.*
# Frontend Trading-Cockpit Polish Task - 2026-06-23

### TASK-UI-01 - Remove cockpit clutter and simplify trading surfaces
- **Status**: `[x]` commit 511d19a · 2026-06-24
- **Tier**: 2
- **Session size**: ~1 hour
- **Files to touch**: `frontend/src/components/Layout.jsx`, `frontend/src/pages/Dashboard.jsx`, `frontend/src/index.css`, `frontend/src/pages/Orders.jsx`, `frontend/src/pages/Calendar.jsx`, `frontend/src/pages/MarketHub.jsx`, `frontend/src/App.js`, `wiki/memory.md`

**Problem**: Dashboard and shell still show duplicated/low-value side widgets: mini blotter rail, Primary Watchlist, Live Feeds, and watchlist panels. These repeat top-bar/Market Hub data and make the trading cockpit feel cluttered.

**Exact steps**:
1. Remove the global mini blotter rail, its expand/collapse affordance, localStorage state, and supporting component code.
2. Remove Dashboard Primary Watchlist, Live Feeds, and secondary Stock Watchlist cards.
3. Keep important feed/readiness state in the top bar, readiness banner, Market Hub, and Hermes brief instead of duplicating it on Dashboard.
4. Preserve the merged Execution workspace, Calendar day-summary expansion, and Hermes market brief changes from the current frontend pass.

**Verify**: run `CI=false npm run build` from `frontend/`; inspect `/dashboard`, `/orders`, `/positions`, `/calendar`, and `/market-hub` locally before deploy.

---

### TASK-UI-02 - Compact dashboard density and refresh app fonts
- **Status**: `[x]` commit 127f91b · 2026-06-24
- **Tier**: 2
- **Session size**: ~1 hour
- **Files to touch**: `frontend/src/pages/Dashboard.jsx`, `frontend/src/components/dashboard/KpiCard.jsx`, `frontend/src/components/dashboard/StrategyLedgerRow.jsx`, `frontend/src/index.css`, `wiki/memory.md`

**Problem**: Dashboard uses too much vertical space for the hero, KPI cards, health checks, and strategy ledger. The serif heading treatment also makes the product feel heavier than a focused trading console.

**Exact steps**:
1. Replace the oversized Dashboard hero/NIFTY chart banner with a compact control/status strip.
2. Reduce KPI cards, health check metrics, and strategy ledger rows while keeping values readable.
3. Refresh global typography to a cleaner UI sans + mono pair and remove decorative serif headings.
4. Verify the frontend production build before commit/deploy.

**Verify**: run `CI=false npm run build` from `frontend/`; inspect `/dashboard` desktop/mobile after deploy.

---

### TASK-UI-03 - Strategies and modals consistency pass
- **Status**: `[x]` commit edb4949 Â· 2026-06-24 Â· build verified; browser QA blocked by local connector
- **Tier**: 2
- **Session size**: ~1.5 hours
- **Files to touch**: `frontend/src/pages/Strategies.jsx`, `frontend/src/components/strategies/StrategyCard.jsx`, `frontend/src/components/strategies/RuntimeSettingsForm.jsx`, `frontend/src/components/strategies/AboutStrategyModal.jsx`, `frontend/src/index.css`, `wiki/memory.md`

**Problem**: The Strategies surface still carries older dense styling in nested controls and modals: hardcoded white text, indigo focus states, cramped grids, all-caps label noise, and modal/action styling that does not fully match the compact trading-console direction.

**Exact steps**:
1. Normalize strategy cards, runtime settings inputs/selects, and About modal sections to the shared `qd-*` tokens and current Manrope/mono typography.
2. Remove hardcoded `text-white`, legacy indigo focus colors, and old dark utility leakage where shared tokens already exist.
3. Tighten modal spacing and mobile scroll behavior while preserving the existing About, test/backtest, runtime edit, enable/disable, and strategy action flows.
4. Keep all strategy data, risk controls, and broker/trading behavior unchanged.

**Verify**: run `$env:CI='false'; npm run build` from `frontend/`; inspect `/strategies` desktop/mobile, including runtime settings expansion and About modal scroll/action states.

---

### TASK-UI-04 - Hermes and AIBot polish
- **Status**: `[x]` commit edb4949 Â· 2026-06-24 Â· build verified; browser QA blocked by local connector
- **Tier**: 2
- **Session size**: ~1.5 hours
- **Files to touch**: `frontend/src/pages/AIBot.jsx`, `frontend/src/components/aibot/ChatFeed.jsx`, `frontend/src/components/aibot/AgentContextPanel.jsx`, `frontend/src/components/aibot/PromptSuggestionsPanel.jsx`, `frontend/src/index.css`, `wiki/memory.md`

**Problem**: Hermes Analyst Co-Pilot is powerful but visually dense. The chat, context rail, citations, suggestions, and approvals queue need a calmer operator layout with better empty/loading/error states and mobile handling.

**Exact steps**:
1. Polish the chat header, input area, message spacing, citation/tool cards, and approvals queue to match the compact `qd-*` design system.
2. Improve empty, loading, streaming, and error states without adding in-app explanatory copy or changing Hermes safety semantics.
3. Verify the context rail and prompt suggestions behave cleanly on desktop and mobile, including long chats and narrow viewports.
4. Preserve the read-only tool boundary and approval-gated action behavior; no backend or trading behavior changes.

**Verify**: run `$env:CI='false'; npm run build` from `frontend/`; inspect `/ai-bot` desktop/mobile, including chat, citations, suggestions, context rail, and pending approvals tab.

---

### TASK-UI-05 - Mobile QA and theme leakage cleanup
- **Status**: `[x]` commit edb4949 Â· 2026-06-24 Â· build verified; browser QA blocked by local connector
- **Tier**: 2
- **Session size**: ~2 hours
- **Files to touch**: `frontend/src/index.css`, `frontend/src/components/Layout.jsx`, and only the page/component files required by the QA findings; `wiki/memory.md`

**Problem**: After the cockpit cleanup and dashboard compaction, the remaining risk is cross-page polish drift: mobile overflow, clipped controls, bottom-nav overlap, table scroll issues, modal scroll regressions, and old theme utility classes leaking through nested forms or cards.

**Exact steps**:
1. QA `/dashboard`, `/orders`, `/positions`, `/strategies`, `/market-hub`, `/calendar`, `/wiki`, and `/ai-bot` at mobile and desktop widths.
2. Fix only evidenced visual issues: text wrapping, overflow, clipped buttons, modal scroll, table scroll, bottom-nav overlap, and old `bg-black/*`, `bg-white/*`, `border-white/*`, `text-white`, or legacy focus-color leakage.
3. Prefer shared `index.css` token fixes for repeated leakage; use page-level edits only where the issue is local.
4. Do not remove routes, data, trading actions, or safety/readiness surfaces.

**Verify**: run `$env:CI='false'; npm run build` from `frontend/`; inspect all listed routes locally at mobile and desktop widths before any deploy, then rebuild the frontend container if deployed.

---

## PRIORITY — HERMES SELF-IMPROVEMENT LOOP ("the trading brain") (2026-06-30)

**Goal**: evolve Hermes from a co-pilot that *narrates* the day into a brain that *learns* from it — analyzes every day's trading, grows validated knowledge, scores its own past calls, and (human-approved) tunes the book toward measured edge.

**The loop**: OBSERVE → ATTRIBUTE → HYPOTHESIZE → VALIDATE (OOS) → REMEMBER → ADVISE → (back to OBSERVE), with a central SELF-SCORE that grades Hermes' own past lessons.

**Two non-negotiable laws** (keep it a brain, not a hallucinator):
1. **Every claim is backed by a computed number + sample size.** Code computes the truth; Gemini only phrases it. No fact with n<5 stated as fact. ("LLM narrates, code computes.")
2. **No lesson influences trading until it survives an out-of-sample backtest.** Hermes proposes; the backtester judges on held-out data; the human approves. JUDGE-FIRST (build the OOS judge before the proposer can promote anything).

**Constraints / context**:
- Stays **Gemini 2.5-flash** (no GPU; flash is the analyst, not the calculator). Runs as a cheap **once-a-day batch**, not per-tick.
- Hermes **never trades and never edits code** — read-only tools + approval-gated `pending_actions` only (existing law, `read_only=True`).
- **Prerequisite CLEARED 2026-06-30**: this was backlogged "until win-rate settles" because the data was fake (phantom wallet). The phantom-wallet exit-qty fix (`ff4fd58`) + self-healing reconcile (`0c3d1b2`) made paper P&L real, so the loop can now learn from trustworthy data.
- **Data-maturity reality**: build now, but the brain only gets *smart* over weeks. 1 clean day = nothing; ~30 clean days = real signal. Stages 1–3 are buildable immediately; Stage 4 (OOS) needs ~3–4 weeks of clean attribution before its held-out windows have signal; Stage 5 is founder-gated.
- **Reuses existing assets**: `hermes_memory` (embeddings/RAG) · `READ_ONLY_AGENT_TOOLS` (routes/ai.py) · `pending_actions`/Approvals UI · `core/backtest_engine` + `core/options_backtest` + `backtrader_runner` · `core/strategy_scorecard` · AutoResearch (Phase 0/1 done — see project_autoresearch) · `_position_exposure_bias` (strategy_runner, added 2026-06-30) · EOD `_compile_eod_memory` (position_monitor).
- **⚠ Relationship to the existing HSB campaign** (PRIORITY 12, HSB-01..17 above — see project_hermes_second_brain): **HSB-01..10 are DONE/deployed — REUSE, don't rebuild.** Already built: `get_strategy_score_explained` + `get_external_context` (tools), `db.hermes_recommendations` + `_score_open_recommendations` (advise→observe→score ring — overlaps Stage 3), `draft_strategy_pause` (governed action — the Stage 5 pattern), `recall_memory`/`hermes_memory`. **What HSI genuinely ADDS** = the missing **"why" layer (Stage 1 Trade Attribution — brand new, no equivalent exists)**; Stages 4–5 are a concrete re-framing of HSB-11..17's OOS-judge/ratchet. When building, **extend the existing collections/functions, don't fork parallel ones** (e.g. fold the scored-lesson store into the `hermes_recommendations`/`_score_open_recommendations` machinery).

---

### Stage 1 — Trade Attribution Engine (code only, no LLM) — *the "why"*
**Goal**: tag every closed trade with the dimensions that explain win/loss, and make them queryable. Pure deterministic code = fully trustworthy. Everything else reads from this.
- `[x]` **HSI-11** New `trade_attribution` collection + `core/trade_attribution.py`. Per CLOSED trade write one record: `{trade_id, strategy_id, strategy_name, user_id, date_ist, underlying, asset_type, structure, exposure_bias, regime_at_entry, entry_time, exit_time, hold_minutes, exit_reason, entry_price, exit_price, realized_pnl, planned_risk, R_multiple, slippage, is_win}`. `exposure_bias` via `strategy_runner._position_exposure_bias`. `R_multiple = realized_pnl / planned_risk` (planned_risk = |entry−stop|×qty from `tp_sl_tsl_config`; for spreads use `max_loss`).
- `[x]` **HSI-12** Populate it: new `compile_trade_attribution(db, user_id, date)` called from `position_monitor._run_eod_aggregation` (after the `daily_reports` write, alongside `_compile_eod_memory`). Idempotent per (trade_id) — unique index on `trade_id`.
- `[x]` **HSI-13** Persist `regime_at_entry` + `planned_risk` on the position doc **at creation** (so attribution is exact, not reconstructed). Add to position-creation sites: `core/portfolio_ledger.py` (single-leg/equity) and `core/spread_lifecycle.py` (spreads). Source regime from the signal's `regime_snapshot.regime` (strategy_runner already attaches it). Until backfilled, attribution falls back to "UNKNOWN".
- `[x]` **HSI-14** Aggregation helper `attribution_rollup(db, user_id, since, group_by)` → group by any of `{strategy, structure, regime, exposure_bias, exit_reason, hold_bucket, time_of_day}` → returns per-bucket `{n, net_pnl, win_rate, avg_R, expectancy, avg_hold_min}`. Pure code, no LLM.
- `[x]` **HSI-15** New read-only tool `get_trade_attribution` (register in `READ_ONLY_AGENT_TOOLS` + handler beside the other `get_*` tools in routes/ai.py + add to the tool-matching keywords). Lets the user ask Hermes "why did BANKNIFTY lose this week" and get a numbers-backed table.
- **Acceptance**: after one session, one row per closed trade with non-null bias/structure/exit_reason/R; `get_trade_attribution` returns a grouped table; spot-check 3 trades' R vs manual calc.
- **Files**: `core/trade_attribution.py` (new), `position_monitor.py`, `routes/ai.py`, `core/portfolio_ledger.py`, `core/spread_lifecycle.py`. **Deps**: none.

### Stage 2 — Grounded EOD analysis (LLM reads attribution, not raw JSON)
**Goal**: the daily distillation produces observations backed by numbers + sample sizes instead of vague prose.
- `[x]` **HSI-21** Rewrite `_compile_eod_memory` input (position_monitor.py): replace the raw `json.dumps(strategies/alerts/signals)` blob with the Stage-1 `attribution_rollup` output (per-structure, per-regime, per-bias expectancy + sample sizes) + a **week-to-date** rollup for trend.
- `[x]` **HSI-22** Update the `distill_daily_report_to_facts` prompt (find the distiller — `core/embeddings.py` / hermes module): require each fact to cite **metric + value + sample_size + dimension**; forbid stating any claim with n<5 as fact (label "insufficient sample"); emit **structured** facts `{claim, dimension, metric, value, sample_size}` (not just prose).
- `[x]` **HSI-23** Store distilled observations both as embeddings (existing `hermes_memory`, keep RAG) AND as structured rows (new `hermes_observations`) for Stage 3 to score.
- **Acceptance**: EOD facts read like "BANKNIFTY credit_spread expectancy +X over n=Y (WTD)" — no n<5 claim asserted as fact.
- **Files**: `position_monitor.py`, the distiller module, `core/trade_attribution.py`. **Deps**: Stage 1.

### Stage 3 — Scored Lesson Store (knowledge that self-corrects) — *the SELF-SCORE core*
**Goal**: lessons carry confidence/sample/hit-rate and decay. This is "knowledge increases" + "improves itself".
- `[x]` **HSI-31** DONE 2026-07-01 (`fb486ef`): `hermes_lessons` collection + `core/hermes_lessons.py`. Keyed by (dimension,bucket); fields incl. claim/direction/metric_at_creation/sample_size/confidence/status/hit_rate/observations_count/correct_count/last_confirmed_at.
- `[x]` **HSI-32** DONE (`fb486ef`): `score_and_update_lessons(db,user_id,date)` wired into `position_monitor` EOD after Stage 1/2. Deterministic (scores sign of today's `attribution_rollup` expectancy vs the lesson's claim): confirm→correct_count++ + confidence up; contradict→hit_rate falls. Decay: stale (LESSON_DECAY_DAYS) or hit_rate<0.5 over ≥LESSON_DECAY_MIN_OBS. **Idempotent per (lesson,date)** via last_scored_date. Env-tunable thresholds.
- `[x]` **HSI-33** DONE (`fb486ef`): candidate→active on LESSON_PROMOTE_K confirmations at hit_rate≥0.6.
- `[x]` **HSI-34** DONE (`fb486ef`): read-only tool `get_hermes_brain_health` (active/candidate/decayed counts, avg confidence, overall hit-rate, top lessons) + keyword routing in routes/ai.py. Verified on real data: 07-01 seeded 10 candidate lessons with correct directions (credit_spread/RANGE/squareoff=good; debit_spread/single_leg/time-exit/killswitch/TREND_UP/BULLISH=bad). NOTE: `attribution_rollup(since=date)` is cumulative-from-date; the live EOD path passes since=today (today-only, correct) — do NOT backfill a PAST date (pulls in later days, inflates sample_size).
- **Acceptance**: lessons accumulate over a week; a deliberately-wrong test lesson decays after contradicting data; hit-rate updates daily; brain-health tool returns sane numbers.
- **Files**: `core/hermes_lessons.py` (new), `position_monitor.py`, `routes/ai.py`. **Deps**: Stages 1–2.

### Stage 4 — OOS Hypothesis Validator (the guardrail — JUDGE-FIRST)
**Goal**: no lesson becomes a trading rule until it passes an **out-of-sample** backtest. Build the judge BEFORE the proposer can promote.
- `[x]` **HSI-41** DONE 2026-07-03 (`d1553fc`) — hypothesis schema now derives from `hermes_lessons`: `{lesson_id, dimension, bucket, direction, claim, objective:"expectancy", source_window_end, oos_start}`.
- `[x]` **HSI-42** DONE 2026-07-03 (`d1553fc`) — `validate_hypothesis(db, hypothesis)` lives in `core/hermes_validator.py`. It tests held-out `trade_attribution` rows after the lesson source window, compares bucket expectancy against the control population, and passes only if OOS sample and effect-size thresholds clear. Until the 3–4 week data window matures, results correctly return `INSUFFICIENT_DATA`.
- `[x]` **HSI-43** DONE 2026-07-03 (`d1553fc`) — judge-first state is enforced in data: each lesson gets `oos_status`, `oos_passed`, and `last_oos_result`; Stage 5 must require `oos_passed=true` before proposing any config action.
- `[x]` **HSI-44** DONE 2026-07-03 (`d1553fc`) — every OOS test is written to `hermes_hypothesis_tests`; per-run tests are capped by `HERMES_OOS_MAX_TESTS_PER_RUN`; pass requires effect size (`HERMES_OOS_MIN_EFFECT_INR`) and minimum OOS sample (`HERMES_OOS_MIN_TRADES`), not just sign.
- **Acceptance**: feed one known-good and one known-noise hypothesis → validator promotes the good, rejects the noise; OOS result stored + auditable.
- **Files**: `core/hermes_validator.py` (new), backtester wiring, `core/hermes_lessons.py`. **Deps**: Stages 1–3 + existing backtester. ⛔ **needs ~3–4 weeks of clean attribution** for OOS windows to carry signal.

### Stage 5 — Gated Advisor (learning changes behavior — human-approved)
**Goal**: OOS-validated lessons become (a) approval-gated config proposals and (b) read-only live context the strategy gates may consult. **Never auto-trades, never edits code.**
- `[ ]` **HSI-51** New `pending_action` type `draft_config_change` (extend `allowed_actions` in routes/ai.py ~L1336; mirror `draft_strategy_pause`). Params `{scope/strategy_id, field, current, proposed, lesson_id, oos_evidence}`. Renders in the existing Approvals UI. On approve → applies the delta **respecting the template-resync mechanic** (only `required_capital`/`options.structure` survive a DB-only edit; otherwise surface an "edit-in-template" task).
- `[ ]` **HSI-52** Live read-only advisory context: "active" validated lessons exposed via a cached `hermes_advice` doc the strategy_runner gates MAY read (e.g. a regime×structure confidence multiplier) — **behind a flag, default observe-only**. Code still decides; the lesson only nudges a parameter. (AutoResearch ratchet apply-step, human-gated.)
- `[ ]` **HSI-53** Close the loop: when an approved change is applied, tag subsequent `trade_attribution` rows with the `lesson_id`, so Stage 3 can score whether the CHANGE actually improved real P&L (the ultimate self-score — did the brain's advice help?).
- `[ ]` **HSI-54** Safety rails: every applied change is reversible (store prior value), rate-limited (≤N changes/week), and **auto-reverted** if post-change expectancy drops over a window (with a notification).
- **Acceptance**: a validated lesson surfaces as an Approval card with OOS evidence; approving applies + logs + tags; later attribution shows whether it helped; revert works.
- **Files**: `routes/ai.py`, frontend Approvals (renders `pending_actions` already), `strategy_runner.py` (52 read), `core/hermes_lessons.py` (53). **Deps**: Stages 1–4. **Founder-gated.**

**Suggested order**: HSI-11→15 (Stage 1, do first — unblocks everything) → HSI-21→23 → HSI-31→34 → let ~3–4 weeks of clean attribution accumulate → HSI-41→44 → HSI-51→54.

---
