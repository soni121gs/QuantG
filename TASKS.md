# TASKS.md — QuantG Agent Work Queue

**Read AGENTS.md before starting any task.**
**Pick the first open task `[ ]` that matches your model tier. Mark `[~]` when starting, `[x]` when done.**

Legend: `[ ]` open · `[~]` in progress · `[x]` done · ⛔ blocked (prerequisite not done)

---

## CURRENT STATE & ACTIVE QUEUE (updated 2026-07-10)

**▶▶▶ ACTIVE PROGRAM (2026-07-10) — Regime-Aware Ensemble (RAE) — the umbrella program. NOW the top priority. Full spec: CLAUDE.md §18.**
Founder-directed after the 2026-07-10 trend-up day: the whole book is premium sellers, so on the ~1% of days that trend, everything bleeds and the best case is "everyone wisely stands down." A book whose best case on a trend day is inaction is one strategy with extra steps. **RAE does NOT discard RES (§15) or EM (§16) — it consumes them:** EM = the router's sizing engine, RES-2 `market_context` + ERL `historical_regimes` = the classifier substrate, RES-3 dynamic exits + RES-6 portfolio risk = the risk layer, QG-O1's RES-2 gate = the proven template. **The 498-day study (`scratch/regime_directional_oos.py`) is the mandate:** market = RANGE 60% / INSIDE 26% / HIGH_VOL_CHOP 13% / TREND ~1%; each regime has a DIFFERENT winner; a delta-1 trend module IS +ve on trend days (+₹3,132/day, 80% WR) but only if the gate is PRECISE (loose gate fires ~90 days/yr and goes net-negative on ~130 range fakeouts). **The missing organ is the classifier + router, not more strategies.** Framing law: coverage not omnipotence — no strategy wins every day; the ensemble covers the day-types. `CORE_ENGINE_LIVE_ENABLED=false` until the Phase-7 founder gate.
GOVERNING LAW: (1) every specialist OWNS one regime and MUST stand down outside it; (2) judge each specialist ON ITS REGIME's days, not blended (the fix for small-sample OOS distrust); (3) stand-down is a first-class strategy (chop → don't trade); (4) precision > payoff for the trend gate (IV-cheap mirror of QG-O1 + daily-trend alignment; validated by flipping the all-days number +ve); (5) no specialist scales on backtest — regime-conditional OOS → forward-paper-on-regime → founder-gated live; Hermes narrates, code computes.
REUSE MAP: classifier ← `core/market_context.py` (RES-2) + `core/historical_regimes.py` (ERL, no-lookahead trend/vol/gap tags); router sizing ← `core/edge_sizer.py` + `signal_manager._edge_math_spread_size` (EM); exits ← `core/dynamic_exit.py` (RES-3); portfolio risk ← `core/portfolio_risk.py` (RES-6); data ← index_1m (498d) + options_1m (live capture wired) + bhavcopy. Build 0→7 in order.
- `[x]` **RAE-0** *(DONE 2026-07-10)* Lock the regime taxonomy — `core/regime_taxonomy.py`: canonical labels `TREND_UP/TREND_DOWN/RANGE/HIGH_VOL_CHOP/INSIDE_QUIET/EVENT` + frozen thresholds (RAE_* env-tunable) + `classify_day()` (full-day descriptive labeler for RAE-2 bucketing) + `REGIME_OWNER` map + documented `BASE_RATES`. Pure, no I/O. **VERIFIED on 498 real NIFTY days — base rates match the study to the decimal: RANGE 60.2% (study 60%), INSIDE 25.5% (26%), CHOP 13.1% (13%), TREND_UP 1.2% (1.2%).** (TREND_DOWN=0 in the 2-up-year sample — known bull bias.)
- `[x]` **RAE-1** *(DONE 2026-07-10)* The Regime Classifier — `core/regime_classifier.py`: pure `classify_intraday(bars, context=, is_event=) → RegimeSnapshot(label, confidence, features, reasons)`, NO-LOOKAHEAD (reads only bars-so-far), IDENTICAL live (feed buffer) + historical (store) — plain minute-bar list + optional `market_context`-shaped dict. Confidence is MATURITY-aware (a trend earns confidence only after ~45 bars — deliberately reluctant to call TREND early, since the study showed over-eager trend calls are the loss). `classify_at(day_bars, cutoff)` for backtests/router. Reuses `regime_taxonomy` thresholds + `market_context` vol_state/event overlay. 12 tests green. **VALIDATED on 498 days:** a price-only TREND call @11:30 is LOW precision (16% — trends are rare, fakeouts common — exactly the RAE thesis), BUT **confidence separates signal from noise: correct TREND_UP calls avg conf 0.95 vs 0.83 for fakeouts** → the router (RAE-4) thresholds on confidence, and RAE-3c adds the IV-cheap + daily-alignment gate to tighten precision. Isolated — nothing imports it yet (zero live impact); ships to the container on the next backend rebuild. Wiring = RAE-4.
- `[ ]` **RAE-2** REFORM THE JUDGE (regime-conditional OOS) — **do this BEFORE any specialist** so each can be graded honestly. Grade a strategy ONLY on its regime's days, walk-forward within regime; small-sample → `NEEDS_FORWARD_PAPER`, never an auto-veto. Extends the EOD/IMD OOS engines with a regime filter (from RAE-1). ⛔ needs RAE-1. *Deliverable: regime-conditional scorecard; the answer to "OOS killed my good strategy."*
- `[ ]` **RAE-3a** Specialist: HIGH_VOL_CHOP stand-down — pure risk cut, **ship first** (near-zero downside): when RAE-1 says CHOP, force size→~0 for all specialists. Reuses the EM day-governor path. ⛔ needs RAE-1. *Deliverable: the book stops bleeding on the 13% chop days.*
- `[ ]` **RAE-3b** Specialist: INSIDE_QUIET VWAP mean-revert — the unexploited +₹164/day, 51% WR edge from the study. Delta-1 or defined-risk. Judged on INSIDE days (RAE-2). ⛔ needs RAE-1, RAE-2.
- `[ ]` **RAE-3c** Specialist: TREND delta-1 directional module — the mirror of QG-O1 (long-delta when trend confirmed). Deep-ITM/future (~zero theta). **The hard one — the entire edge is the gate:** IV-cheap + higher-timeframe (daily) alignment + decisive break, tuned so it flips the ALL-DAYS number positive (precision, not payoff). Judged on TREND days (RAE-2). ⛔ needs RAE-1, RAE-2.
- `[ ]` **RAE-3d** Specialist: sellers as the RANGE specialist — keep/retune the existing seller book (QG-O11 etc.) as the RANGE/INSIDE owner; ensure it stands down outside RANGE via RAE-1. ⛔ needs RAE-1.
- `[ ]` **RAE-4** The Router / Capital Allocator — regime+confidence (RAE-1) → activate the owning specialist(s) → size via EdgeMath (§16), with stand-down as a legal output. Replaces "run every strategy every day" with "run the day's specialist(s)." Paper-first, founder-gated. ⛔ needs RAE-1 + specialists. CONFLICT ZONE: this gates `signal_manager` activation — touch carefully alongside EM-4.
- `[ ]` **RAE-5** Regime-aware exits — extend `dynamic_exit`/`spread_lifecycle`/`position_monitor` so exits match the active regime (trend trails wide = let winners run; range books fast = theta). ⛔ needs RAE-1, RAE-4.
- `[ ]` **RAE-6** Forward-paper the ensemble + Hermes telemetry — run the whole thing in paper, judged per-regime; add the live "regime of the day + who's active + who's standing down + per-regime P&L" view. ⛔ needs RAE-4. *Deliverable: evidence that on a trend day the trend module fires while sellers sleep, and vice versa.*
- `[ ]` **RAE-7** Founder-gated live pilot — only after positive regime-conditional forward-paper. `CORE_ENGINE_LIVE_ENABLED` stays false until the founder flips it. ⛔ needs RAE-6.
> Build order: RAE-0 → RAE-1 → RAE-2 (judge before specialists) → RAE-3a (ship, pure risk cut) → RAE-3b/3c/3d (specialists, each OOS-on-regime) → RAE-4 (router) → RAE-5 (exits) → RAE-6 (forward-paper) → RAE-7 (live). Classifier + honest judge precede any strategy; stand-down before new alpha.

**▶▶ ACTIVE PROGRAM (2026-07-08) — Real-Edge System (RES) — founder-directed book rebuild. This is now the top priority.**
The founder rejected the existing book after a −₹4,571 day (holds ONE position all day, no profit-lock so green round-tripped to red, two of three positions the SAME NIFTY bull-put bet, QG-O1 sold puts into a `TREND_DOWN` for −₹5.2k). Replace the "sell one spread and sit" machine with a **dynamic regime-aware seller scalper** (banks profits + trails, re-enters, rotates CE/PE, cuts losers fast) — **validated on the OOS judge before it trades.** Framing law: no "foolproof" — target is validated + cost-robust + risk-controlled. Hermes stays researcher/disciplinarian, NEVER the trader. Full spec: CLAUDE.md §15. Build 1→8 in order (1–6 = reusable machinery, 7 = the strategy, 8 = truth check).

- `[x]` **RES-1** *(DONE 2026-07-08, `06b04c2`)* Realistic cost model — the truth layer. Paper credit spreads filled both legs at raw MID (zero bid/ask crossing) on entry AND exit → paper overstated every spread edge. `core/spread_lifecycle.py` `_apply_paper_slippage` + `PAPER_SPREAD_SLIPPAGE_PCT` (env, default 0.03), per-leg, paper-only (live untouched). Single-leg already had slippage via `execution_router`; only the spread path (whole book) was free. 29 spread/paper tests green. Existing book's paper P&L now reads worse — the illusion removed, not a regression.
- `[~]` **RES-2** Market Intelligence Engine (Phase 1) — **ENGINE BUILT 2026-07-08 (`core/market_context.py`, 15 tests, pushed).** ONE `market_context` snapshot bundling the 5 signals as PURE functions (same code live + historical = item G): **A** IV−realized_vol edge (`vol_edge`, NEW — realized vol from index daily closes, `rich` when IV−RV ≥ min pts); **B** regime + `vol_state` CALM/NORMAL/STORMY (reuses `market_regime.compute_regime_from_data`; seller gate = RANGE & not STORMY); **C** `chain_intel` PCR + OI walls + IV skew → richer/safer side; **D** order-flow imbalance attached as filter (reuses `order_flow`); **E** `event_context` expiry/macro fat-tail gate. `build_context()` → `seller_decision{allow_sell, side, reasons}`. **REMAINING (wiring, lands with consumers):** live adapter feeds it real feed data (RES-7) + historical adapter feeds it store data in the OOS backtester (RES-8) + turn order-flow feed to "full" mode. Base reused: `market_regime`,`iv_regime`,`order_flow`.
- `[x]` **RES-3** *(DONE 2026-07-08, deployed)* Dynamic exit engine — `core/dynamic_exit.py` (pure): bank + **trailing lock** + fast stop. On top of the existing hard tp/sl, once a spread captures peak profit (armed at `DYN_EXIT_TRAIL_ARM_FRAC=0.4` of max) and retraces past `DYN_EXIT_TRAIL_GIVEBACK_FRAC=0.5` from peak → exit `trail-lock`, banking a fading winner before it round-trips to red. Purely additive (hard stop still wins; unchanged when inactive). Wired into `position_monitor._process_spread_position`: tracks `peak_pnl` each live tick, exits credit spreads via `evaluate_spread_exit`. Hold-to-expiry spreads (QG-O1) return earlier → unaffected; only intraday spreads that round-trip get trailing. 8 tests + clean in-container startup/import verified.
- `[x]` **RES-4** *(DONE 2026-07-08)* Re-entry / multi-trade policy — `core/reentry.py` (pure): `evaluate_reentry` = no-pyramiding + per-day cap (`SCALP_DAILY_TRADE_CAP=6`) + post-exit cooldown (`REENTRY_COOLDOWN_SECONDS=300`, anti-churn). NOTE the signal_manager anti-pyramiding guard already ALLOWS re-entry after a close (blocks only concurrent stacking); this adds cadence discipline. Wiring lands with RES-7.
- `[x]` **RES-5** *(DONE 2026-07-08)* Side rotation — `core/side_selector.py` (pure): `select_sell_spread(context)` turns market_context `seller_decision` → concrete spread (PE=bull-put/bullish, CE=bear-call/bearish). Regime+skew driven. Wiring lands with RES-7.
- `[x]` **RES-6** *(DONE 2026-07-08)* Portfolio risk layer — `core/portfolio_risk.py` (pure): book-level `evaluate_portfolio_gate` = heat cap (`PORTFOLIO_HEAT_BUDGET=60000`, Σ max_loss of open) + correlation guard (`MAX_CORRELATED_PER_UNDERLYING=2`, same-bias/underlying) + daily-loss stop (`PORTFOLIO_DAILY_LOSS_LIMIT=15000`). Targets today's failure (2/3 same NIFTY bull-put). Consolidates the existing `MAX_DIRECTIONAL_EXPOSURE_PER_UNDERLYING`. Wiring lands with RES-7. **RES-4/5/6: 14 tests green.**
- `[x]` **RES-7** *(BRAIN DONE 2026-07-08)* The regime-conditioned seller scalper — `core/seller_scalper.py` `decide(context, book_state, config)`: one deterministic entry verdict assembling RES-2 (market_context gate) + RES-5 (side PE=bull-put/CE=bear-call) + RES-4 (cadence: no-pyramid/cap/cooldown) + RES-6 (heat/correlation/daily-loss) + entry-window gate. RES-3 trailing lock already owns the exit. Geometry defaults = OOS-surviving near-ATM width-1 seller (env-tunable). PURE — same brain drives live runner and the OOS backtester. 11 tests (48 across RES stack). **DOES NOT TRADE:** per OOS-first law it stays unactivated until RES-8 validates + founder gates. REMAINING (RES-8 territory): backtester drives `decide()` on historical `market_context`; live runner wiring + strategy seed happen only on an OOS pass.
**▶▶ ACTIVE PROGRAM (2026-07-09) — EdgeMath (EM): continuous edge-based sizing & P&L intelligence. Supersedes gate-tweaking.**
Founder mandate: NO more hard gates/blockers. Replace every binary allow/block with a **continuous edge→size function** so the book sizes UP on real edge and fades toward ZERO as edge decays or the day turns red — smoothly, mathematically. Honest target: **E[daily P&L] > 0 with an asymmetric, bounded loss distribution** (small capped losses, larger let-run wins) — NOT a guaranteed green day (variance forbids that; state it plainly). Three layers, all PURE so the live monitor and the OOS backtester share one code path (RES design rule): **L1** signal→edge score (expectancy + payoff ratio from the strategy's OWN rolling closed trades, conditioned on regime×vol); **L2** edge→base size (fractional-Kelly conviction × volatility-target risk); **L3** day-P&L governor (green-day compounding + red-day de-risking + profit ratchet). Grade on OOS before wiring live. `CORE_ENGINE_LIVE_ENABLED=false` throughout. Reuses: `trade_attribution` (rolling W/expectancy by dimension — already built), `market_context` (RES-2 IV−RV/vol_state/regime), `dynamic_exit` (RES-3 trail), `spread_builder.lots_for_risk`, `profit_lock` (must reconcile — see EM-5).
CONFLICT MAP (verified 2026-07-09): sizing injection = `signal_manager.py:845/890` (`_spread_lots = lots_for_risk(max_loss, lot_size, required_capital)` → `open_credit_spread`); day-P&L = `portfolio_ledger.get_strategy_pnl_today` / `strategies.today_pnl`; equity = `db.paper_wallets`; margin cap = `core/capital_model` (blocks max_loss on open); risk sizing = `core/risk_manager` (`REJECTED_RISK_SIZING`); **OVERLAP: `core/profit_lock.py` is an existing L3 that HARD-BLOCKS re-entry via `day_profit_locked` — must convert to continuous size-down (EM-5)**; legacy `alloc_mult` loss-streak throttle at `signal_manager.py:1110` (subsume in EM-4). Spreads bypass the single-leg 1-lot cap → EM targets the spread path first.
- `[x]` **EM-1** *(DONE 2026-07-09, Opus — 25 tests green)* Pure sizing core `core/edge_sizer.py` (NO I/O, callers pass primitives): `rolling_stats_from_pnls` (→W/μ_win/μ_loss/E), `payoff_ratio` (b, None on no losses), `kelly_conviction` (f=W−(1−W)/b → conviction∈[0,1] vs `EDGE_KELLY_REF=0.15`), `vol_target_base_lots` (risk_pct×equity ÷ per-lot-max-loss, `EDGE_RISK_PER_TRADE_PCT=0.004`), `day_governor_mult` (clamp(1+day_pnl/budget, 0.25, 1.5) + profit ratchet on `EDGE_DAY_GOV_GIVEBACK=0.5`), `edge_size` (combines → lots + edge_score + reason; cold-start n<`EDGE_MIN_TRADES=10` → neutral 0.5, E≤costs → conviction 0 = soft stand-down, never raises, respects `floor_lots`), `vol_scaled_exit_frac` (widen SL by vol ratio, clamped). All env-tunable (`EDGE_*`). `tests/test_edge_sizer.py`. Isolated — nothing imports it yet, zero live impact; ships to the container on the next backend rebuild (EM-4).
- `[x]` **EM-2** Rolling-stats provider — adapter over `trade_attribution.attribution_rollup` → per-(strategy, regime, vol_bucket) W/μ_win/μ_loss over rolling last-N CLOSED trades; cached per strategy per IST day. Feeds EM-1 primitives. Read-only on `trade_attribution` — no conflict.
- `[x]` **EM-3** market_context adapter (live + historical) — make `core/market_context.build_context` callable at signal time to supply regime + vol_state + IV−RV for L1 conditioning and L3 budget scaling, identically live (feed) and in the OOS backtester (store). Reuses RES-2; already pure — no conflict.
- `[x]` **EM-4** Wire sizing into `signal_manager` — replace `_spread_lots = lots_for_risk(...)` (`:845`/`:890`) with `edge_size()` output (base × conviction × day_mult), **capped to `risk_manager` capital + `capital_model` available margin** (growth must not exceed funds), and **subsume the legacy `alloc_mult`** (`:1110`). Paper-first, founder-gated. CONFLICT ZONE — touch carefully.
- `[x]` **EM-5** Reconcile `profit_lock.py` (L3 overlap) — converted profit giveback protection to `day_profit_size_mult=0.25`; it banks open gains and continuously fades later size without blocking re-entry.
- `[x]` **EM-6** Volatility-scaled exits — TP/SL in σ_intraday/ATR units from market_context → `spread_lifecycle.compute_exit_levels`, additive to per-strategy `credit_tp_frac`/`credit_sl_mult` (keep env defaults). Targets breathe with vol so noise doesn't stop winners.
- `[x]` **EM-7** Validation harness — flat-vs-dynamic no-lookahead judge implemented. Founder waived the daily-backtester promotion block on 2026-07-09 because it cannot reproduce intraday chain rotation; clean forward-paper telemetry is now the judge. Live remains disabled.
- `[x]` **EM-8** Self-tuning ratchet — EOD writes observe-only per-strategy expectancy/risk-multiplier advice to `edge_math_advice`; it never mutates strategy configuration.
- `[x]` **EM-9** Telemetry — Execution snapshot and Analytics surface contract score, EdgeMath score/reason, selected lots, signatures, factors, and observe-only advice.
  - **2026-07-09 contract layer landed (Codex):** `core/dynamic_contract_selector.py` now continuously ranks both CE/PE credit-spread candidates across multiple deltas using credit/width, theta, OI liquidity, delta fit, direction fit, time remaining, and a smooth repeated-contract penalty. The selected `contract_edge_score`, `contract_size_mult`, signature, and factors persist on spread positions; exit geometry and spread lots scale continuously from them. This is the contract-quality input to EM, not a replacement for EM-2 rolling expectancy or EM-7 OOS proof. Focused suite: 48 passed. **Still open:** expose these fields in UI together with the eventual expectancy/day-governor telemetry.
> Build order: EM-1 (core) → EM-2/EM-3 (feeders) → EM-7 (OOS proof) BEFORE EM-4 (live wiring) → EM-5 (profit_lock reconcile) → EM-6 (exits) → EM-8/9. Money-correctness + proof precede wiring.

- `[x]` **RES-8** *(DONE 2026-07-08 — VERDICT IN)* OOS gate — `core/res8_oos.py` + `scripts/run_res8_validation.py`: reconstructs a DAILY market_context from bhavcopy (IV proxied from ATM straddle) → signals → settlement-priced EOD engine + walk_forward. Added `signals=` injection to `EODOptionsBacktest.run`. **VERDICTS (NIFTY 2024–25 real data):** (1) **BUYER dead — 5th confirmation**: IV-cheap+trending debit spread = NO_EDGE_NEGATIVE, −₹380/tr, n=50, both years negative. Abandon buying. (2) **The RES-2 gate WORKS**: on the EOD-testable 3% OTM put spread held-to-expiry, UNGATED = NO_EDGE_NEGATIVE (−₹14/tr, 2025 −₹145); GATED by IV−RV+RANGE = monotonic edge — min_edge 0.0 → **CANDIDATE_EDGE, n=36, +₹112/tr, BOTH years +ve, 74% green** (stricter gates: 1.0→+352, 2.0→+443, 3.0→+487 per-tr but n<30). **First strategy in the whole rebuild to PASS OOS.** CAVEATS: (a) intraday ATM width-1 scalp geometry held DAILY = negative — the EOD judge is the wrong instrument for the intraday scalp exit; that needs the IMD 1-min judge + a data backfill; (b) bull-biased 2 up years. NEXT: forward-paper the gated hold-to-expiry seller (deployable, EOD-validated) + backfill intraday data for the true scalp judge. `CORE_ENGINE_LIVE_ENABLED=false`.

---

## PRIOR STATE (updated 2026-07-04)

**⭐ HEADLINE (2026-07-04): the data wall is broken, and the OOS verdict is in — the current book has NO EDGE.**
2 years of real NSE option prices are ingested (`backend/scripts/bhavcopy_ingest.py`, 494 days, ~2.5M rows) and the EOD OOS validator (`backend/core/eod_options_backtest.py`) graded the whole book: **0 of 11 option strategies are positive out-of-sample**; a **72-config sweep found 0 winners**. Corroborates live ~−₹86/trade. This UNBLOCKS old `WR-71` (real options-chain backtest) and completes the data layer for HSI Stage 4. See CLAUDE.md §13.

**🚫 NEW LAW (supersedes daily tweaking):** do NOT tune the existing strategies. Every strategy change / new strategy must PASS the OOS validator first. Discipline: hypothesis → OOS backtest → forward-paper → live. Grade ideas on OOS expectancy, not daily paper P&L.

**▶ NEW ACTIVE PROGRAM — Edge Discovery & Book Rebuild (EDR) — restructured 2026-07-04, priority-wise:**

The findings are in: short OTM vol (vol risk premium) is the ONE real edge, monotonic in
OTM distance (SENSEX ~2% OTM strangle +₹2,047/cycle, 82% WR); directional is a random walk;
naked = fat tails. So the program is no longer "search" — it is "turn the naked edge into a
defined-risk deployable strategy, make the science visible in the app, then archive the dead
book." See [[project_base_rate_findings_07_04]].

🔴 **PRIORITY 0 — prove ONE deployable edge (blocks live, blocks the rebuild):**
- `[x]` **EDR-01** Base-rate studies (`backend/scripts/base_rate_studies.py`, ran 2026-07-04, `d94e925`) — DONE. Verdict: short OTM vol pays (monotonic in distance); no directional edge; regime unreliable; naked = undefined tail risk.
- `[x]` **EDR-06** *(2026-07-04, `90fbeb1`/`ef28527`/`499f46f`)* `core/eod_options_backtest.py` now prices a **4-leg `iron_condor`** (sell `short_otm_pct` OTM strangle, buy `wing_width` wings). Three fixes were needed for faithfulness: `exit_mode="expiry"` (hold to expiry, NO credit-based stop — a condor's max loss is wing-widths not credit-multiples), cap the pre-expiry mark at the wing width (illiquid far wings mark ~0 → spurious naked-like spikes), and **cash-settle at index intrinsic at expiry** (per-leg premiums are garbage on expiry day → every trade falsely marked max-loss). Params: `short_otm_pct`/`wing_width`/`credit_tp`/`credit_sl`/`exit_mode`.
- `[x]` **EDR-07** *(2026-07-04, `90fbeb1`, `scripts/condor_study.py`)* Condor base-rate study — **the edge SURVIVES defined risk on SENSEX** (2% OTM, wing 4-6, held to expiry): +₹639/cycle @ 86% WR, worst cycle −₹7k (vs −₹44k naked). **NIFTY & BANKNIFTY condors are NEGATIVE** (thinner premium — wings eat the edge). So the vehicle is SENSEX.
- `[x]` **EDR-08** *(answered 2026-07-04 — CORRECTED)* First read (n=33, off SENSEX Theta's signals) looked like CANDIDATE_EDGE (+₹318/tr, both years +). But **EDR-02(a) firmed it with honest weekly cadence (n≈103) and it is only FRAGILE**: best config SENSEX 2.5% OTM / wing 6 = +₹5/tr, 81% WR, **2024 −₹67 / 2025 +₹73** — one year up, one down. Cost-sensitivity: even at an optimistic 0.5% slip/leg it stays FRAGILE (2024 −25 / 2025 +178). **So it is NOT robust cross-year; the n=33 CANDIDATE was an entry-timing selection artifact.** NIFTY/BANKNIFTY condors strongly negative. Lesson: the OOS-first firm-up killed the illusion cheaply, before any live-infra spend.
- `[x]` **EDR-02** *(closed 2026-07-06 as REJECTED/NO-BUILD)* Weekly-cadence condor + 18-config OTM×wing sweep on SENSEX+NIFTY found **0 CANDIDATE_EDGE, best SENSEX = FRAGILE**. The planned (b)/(c) 4-leg live engine + seeded strategy are intentionally cancelled: do NOT build condor live infra unless a future OOS study finds a robust defined-risk condor edge. The replacement edge is the 2-leg NIFTY put spread in EDR-09..12.
  - **→ EDR-09 *(DONE 2026-07-04 — FIRST DEPLOYABLE EDGE FOUND, `8156469`)*** the one-sided **short put spread held to expiry** keeps the short-vol edge where the symmetric condor gave it back. Tested via `/tmp/tail_defense.py` (base-rate, per-year) + backtester walk-forward at realistic 3% slip/leg. **Robust region (not one cell): NIFTY 3% OTM put spread, wing 6-10 = CANDIDATE_EDGE, +₹214-380/trade, 95% WR, BOTH years positive (wing6: 2024 +120 / 2025 +305), 75%+ green months, n=106.** SENSEX 2% put spread also CANDIDATE (thin, +₹4). Call spreads fail (up-market breaches). **Deploys on the EXISTING 2-leg `credit_spread` engine — no 4-leg infra needed (EDR-02b avoided entirely).** CAVEAT: bull-biased short-vol on 2 UP years — the risk is a sustained downtrend; defined risk caps each loss (~₹22k/lot at wing6) + 95% WR; **must forward-paper before live.** Backtester gained `short_otm_pct` for credit_spread (0=ATM default) + index-intrinsic hold-to-expiry settlement for all structures.
  - `[x]` **EDR-10 *(DONE 2026-07-04, `69ea4a1`)*** Seeded **"NIFTY Put Spread Theta (OOS)"** (`credit_spread`, `short_otm_pct=0.03`, wing 6, `exit_mode=expiry`) as a DRAFT strategy. Edge Lab OOS backtest confirms **CANDIDATE_EDGE, n=106, +₹214/tr, 95% WR, 2024 +120 / 2025 +305, 88% green** — the first green in the book. Also fixed a latent bug: `seed_default_strategies_for_user` built docs but never `insert_many`'d them (no-op for existing users).
  - `[x]` **EDR-03 *(DONE 2026-07-04, `86815b0`)*** Archived all 23 zero-OOS-edge strategies (11 option + 10 equity + 2 extra) via the startup normalization loop (`DEAD_STRATEGY_NAMES` → status=archived, keeps P&L history, runner skips). Book is now: 1 draft edge + 23 archived. Templates kept (harmless; archive is idempotent) — optional de-template later.
  - `[x]` **EDR-11 *(DONE 2026-07-04, `52b69ed`)*** Live hold-to-expiry wiring for the put spread, verified on VPS. (1) Strike: `short_delta=0.12` on the template (~3% OTM short leg) + passthrough to the live options config the spread builder reads (server.py:16773). (2) Risk: `daily_loss_limit=30000` (> one defined max loss ~₹22k) + `time_exit_minutes=0` so the killswitch/time-exit can't force-close before expiry — the wing width IS the stop. (3) `position_monitor._process_spread_position`: hold-to-expiry spreads (options.exit_mode="expiry", cached lookup) skip the 15:25 squareoff and intraday tp/sl, settling only on/after the option's actual expiry day (`_spread_past_expiry`; unknown expiry → settle, never hold forever). Guardian already excludes spreads; staleness already handled. Reuses the 2-leg credit_spread execution end-to-end. **Strategy is still DRAFT — founder flips DRAFT→paused/live to start forward-paper.** CAVEAT not yet exempted: book-level profit-lock could still close a big winner early (conservative, acceptable for forward-paper; revisit if it bites).
  - `[x]` **EDR-12 *(DONE 2026-07-06 — forward-paper started)*** The DRAFT was renamed to **QG-O1 NIFTY Put Spread Theta Core** (same config: `credit_spread`, `exit_mode=expiry`, `short_otm_pct=0.03`). Flipped `status=live`, `mode=paper`, `manual_paused=false`, `schedule_paused=false` on VPS Mongo (stamped `edr12_forward_paper_started_at`). It now forward-papers from the next 09:15 IST open, held to expiry. Observe 3-6 wks: does live paper P&L track the OOS +₹214/trade @ 95% WR? Then → live pilot (WR-73, founder gate).
  - `[x]` **EDR-13 *(DONE 2026-07-06 — QG-O1 expectancy upgrade)*** Focused VPS OOS sweep of the QG-O1 family selected the higher-expectancy 3% OTM / 10-strike-width put spread: **CANDIDATE_EDGE, n=102, +₹382/trade overall, +₹549/trade OOS, 95.1% WR, both years positive**. Updated QG-O1 template + Hermes historical canonical credit-spread config from width 6 to width 10, kept one trade/day, and raised `required_capital=35000` / `daily_loss_limit=40000` so the wider defined-risk cap is honest. Caveat: theoretical per-lot max loss rises from ~₹18.9k to ~₹31.7k; this remains paper-forward until founder-gated live promotion.
  - `[x]` **EDR-14 *(DONE 2026-07-06 — QG-O5 founder-directed paper-forward rebuild)*** Founder requested QG-O5 for tomorrow despite the n<30 caveat. Current QG-O5 buyer variants remain rejected: baseline debit-spread call buyer is still `NO_EDGE_NEGATIVE` on Jan-Mar 2025 1-minute data (direct compare best buyer: 54 trades, -₹10,634 net, -₹197/trade, OOS -₹83/trade, 0/3 green months). Cheaper/tighter/faster buyer variants got worse. Rebuilt QG-O5 as **bullish opening-range trigger → tiny bull-put credit spread** (sell 2 strikes OTM PE, buy 1 strike lower, 60-minute hold): 22 trades, +₹4,248 net, +₹193/trade, OOS +₹207/trade, 63.6% WR, 3/3 green months, avg max loss ~₹2.3k/lot. Added live offset-based credit-spread builder support so QG-O5 uses this exact geometry. Still paper-forward only; promote no real money until the IMD sample gate matures.
  - `[x]` **EDR-15 *(DONE 2026-07-06 — tomorrow book lock: only QG-O1 + QG-O5)*** Restricted the auto-live paper-forward allowlist to **QG-O1 and QG-O5 only**. QG-O2/O3/O4/O6/O7/O8/O9/O10 are now `PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES`; startup migration archives them with `manual_paused=true` and `schedule_paused=false`. Scheduler and startup restore now require `status="paused"` before auto-activating `schedule_paused` rows, so archived rows with stale schedule flags cannot wake up. Production Mongo was also set so every non-QG-O1/QG-O5 strategy is archived for tomorrow.

🟠 **PRIORITY 1 — make the science visible + ship the research modules:**
- `[x]` **FE-01** *(2026-07-04, `45d0b2e`)* Backend Edge Lab endpoints: `GET /ops/edge-lab` (serves cached snapshot instantly) + `POST /ops/edge-lab/refresh` (background rebuild) backed by `core/edge_lab.py` (coverage + short-vol base-rate + per-strategy OOS verdict + credit-spread sweep) and `scripts/build_edge_lab_snapshot.py` → `db.edge_lab_snapshots(_id='latest')`. Build ~9min (perf: memoized `option_chain`/`underlying_daily`, single-pass base-rate, skip no-data equity). Verified on VPS.
- `[x]` **FE-02** *(2026-07-04, `fb15cbf`)* Frontend: repurposed the Analytics **"Option-priced backtest" tab → "Edge Lab (OOS)"** (data-coverage banner + short-vol base-rate table + OOS verdict scorecard + sweep evidence, background-rebuild Refresh+poll). Old in-sample backtest tab removed from UI. `frontend/src/pages/Analytics.jsx`.
  - **First snapshot verdict (live):** coverage 494d 2024-25 all 6 index underlyings; short-vol edge monotonic in OTM (SENSEX 2% OTM ₹1,872/81.7%, NIFTY ₹1,400/75.5%); directional ≈ random walk; OOS = 8 NO_EDGE / 5 thin / 10 equity-no-data / **0 candidate edges**; sweep **0/9 positive-OOS** both theta spreads. The app now shows the truth.
- `[x]` **FE-03** *(DONE 2026-07-04, `227840d`)* Retired the old in-sample options backtester: deleted `core/options_backtest.py`, removed `POST /ops/options-backtest`, repointed the 2 remaining callers (AI `get_backtest_summary` + `/core/backtests/run` option branch) → `ops_eod_options_backtest`, updated the agent-tools test + docstrings. Verified clean on VPS. **Kept:** `core/backtest_engine.py` (equity/underlying engine, still used by `/core/backtests/run` equity branch) + `backtrader_runner` (`/strategies/backtest`, PythonEditor) — separate, legitimately live. Left `scratch/*` (dev-only) importing the deleted module — harmless, not runtime.
- `[x]` **EDR-04** Research modules are committed (`bhavcopy_store`, `eod_options_backtest`, `edge_lab`, `run_*`) and the data volume `./data/bhavcopy_fo:/app/data/bhavcopy_fo:ro` is mounted in `docker-compose.yml` — a normal `build backend` now ships them; no more `docker cp`. (Verify on next rebuild.)

🟡 **PRIORITY 2 — data gaps + cleanup (gated on P0 producing a replacement):**
- `[x]` **EDR-05** *(DONE 2026-07-04, `0410fc5`/`4395512`/`d5af4e7`)* Data-gap closed. **(a) NSE_EQ stock EOD ingested:** added a `cm` source to `bhavcopy_ingest.py` (NSE cash bhavcopy → per-stock OHLC for the 10 equity tickers) into a new `data/bhavcopy_cm` store; backfilled 494 days 2024-25. **(b) Freshness automated:** docker-compose now mounts writable `./data:/app/data`; a host cron (13:30/13:35 UTC weekdays, `docker exec -u root`, 5-day idempotent lookback) keeps NSE FO + CM fresh. **(c)** Fixed a store-path bug: the ingest default overshot to the `/data` named volume in-container — now matches the reader's `/app/data`. **⚠️ BSE (SENSEX/BANKEX) stays MANUAL** — Akamai bot-gate can't be server-automated; keep the browser + `--from-zips` flow. Equity-CM backtester wiring is not an active task; reopen only if the founder explicitly pursues an equity rebuild.
- `[x]` **EDR-03** DONE 2026-07-04 (see P0 block, `86815b0`) — 23 dead strategies archived. De-templating them from `server.py` is cosmetic and removed from the active queue; reopen only if template churn becomes a real maintenance issue.
- `[x]` **FE-04** *(DONE 2026-07-04, `98fbaeb`)* Edge Lab "strategy proposer": `POST /ops/edge-lab/propose` builds a synthetic strategy from a requested underlying/structure/side/OTM/width/DTE/exit, runs the walk-forward bhavcopy backtester, returns the verdict — read-only, nothing seeded. Frontend: a compact Proposer form + verdict card in the Edge Lab tab (`Analytics.jsx`). Verified: proposing the NIFTY 3% OTM put spread returns CANDIDATE_EDGE +₹214/95% WR. Turns OOS-first into a one-click tool.

⏸ **Gating updates:** `WR-73` (enable live) and HSI Stage-5 advisor now additionally require an OOS `CANDIDATE_EDGE` — which NO current strategy has, so both stay founder-gated behind Priority 0.

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

**🩹 Alpha Repair Campaign (2026-07-02 full-book analysis) — measurement gate**
- **DONE 2026-07-02:** `AR-01`+`AR-02` risk/config epoch, `AR-03` equity ATR brackets, `AR-04` equity economics/cutoff, `AR-05` attribution inputs, `AR-06` BANKNIFTY theta expiry guard.
- **NEXT → `AR-08` measurement checkpoint** once the AR-01..06 deploy window has ~8 trading sessions of clean data. `AR-07` stays blocked until that checkpoint proves regime/delta changes are warranted.

**🧠 Hermes Self-Improvement Loop — the headline initiative (§ bottom of file)**
- **Stages 1–4 SHIPPED** ✅ `HSI-11..15` Stage 1 attribution · `HSI-21..23` Stage 2 grounded EOD · `HSI-31..34` Stage 3 scored lesson store (`fb486ef`, 2026-07-01) · `HSI-41..44` Stage 4 OOS validator (`d1553fc`, 2026-07-03 — judge-first, returns `INSUFFICIENT_DATA` until held-out samples mature).
- **Stages 1–5 SHIPPED** ✅ `HSI-51..54` Stage 5 gated advisor DONE 2026-07-06 (`core/hermes_advisor.py`): OOS-gated `draft_config_change` approval action, observe-only advice surface (flag `HERMES_ADVICE_ENABLED`), lesson-tagged attribution, and reversible/rate-limited/auto-reverting safety rails. The full self-improvement loop is now built end-to-end; it only gets *smart* as clean attribution + OOS windows mature. **NEXT (founder):** enable `HERMES_ADVICE_ENABLED` once lessons pass OOS, or approve a `draft_config_change` card.

**🧠 Hermes Intelligence Research Brain (HIRB) — founder-directed 2026-07-09**
- **HIRB-01..07 complete. NEXT frontier:** feed HIRB from more truthful data sources and wire the verifier/critic outputs deeper into Hermes answers, while preserving the no-trading/no-self-mutation boundary.
- Build order: `HIRB-01` truth contracts ✅ → `HIRB-02` research hypothesis ledger ✅ → `HIRB-03` quant math kernel ✅ → `HIRB-04` verifier/critic loop ✅ → `HIRB-05` frozen evidence anti-leakage store ✅ → `HIRB-06` multi-agent research desk ✅ → `HIRB-07` Hermes Research Lab UI ✅. Hermes remains researcher/disciplinarian only; code/OOS judges decide truth, not LLM confidence.

**📈 Win-Rate / Expectancy (cleaned active remainder)**
- Active only after `AR-08`: `WR-45` correlation matrix · `WR-51` risk sizing · `WR-54` auto-pause. `WR-33` is closed/deferred; reopen only if AR-08 proves winners are being cut early. **Folded 07-02:** `WR-42`/`WR-43` → AR-07/AR-08 · `WR-44`/`WR-72` → HSI-41..44.
- Done / not active: `WR-71` real options-chain backtest **DONE 2026-07-04** via free NSE bhavcopy + EOD OOS validator. `WR-73` live enablement is founder-gated, not agent-pickable, and still requires OOS candidate edge + forward-paper evidence.

**Intraday Minute Data / Options Backtesting (IMD) — required before trusting QG-O5..QG-O10**
- **ALL IMD-01..IMD-10 code DONE + WIRED + PROVEN ON REAL DATA 2026-07-06** (schema → resolver → store/importer → capture → reader → selector → backtester → OOS validator → Edge Lab UI → gate). 81 unit tests. IMD-04 live index capture is wired + deployed; a historical **index-minute** layer (`core/index_minute_store.py` + `scripts/index_1m_ingest_upstox.py`) supplies the underlying series. Verified end-to-end on VPS: fetched real Jan-2025 NIFTY 1-min data (index 1,875 bars + 50 option contract-days / 18,750 rows, 0 errors, idempotent), and the validator produced **real intraday trades** (QG-O5/O6 = 5 trades each) — correctly graded `INSUFFICIENT_DATA` at 5 days. **FIRST REAL VERDICTS (2026-07-06, Jan–Mar 2025 NIFTY, 62 index days + 600 option contract-days / 206k rows):** QG-O5/QG-O6/QG-O10 = **NO_EDGE_NEGATIVE at 60 trades each** (the intraday NIFTY buyers have no OOS edge on Q1-2025 — consistent with the book-wide "buyers don't pay" finding); QG-O9 INSUFFICIENT_DATA (rare tail setup, 1 trade); QG-O7/O8 DATA_QUALITY_FAIL (BANKNIFTY not yet backfilled). **Next data step:** backfill BANKNIFTY + extend the window (more months / other quarters) before any QG-O5..O10 promotion. See CLAUDE.md §14.
- ~~`IMD-01` schema~~ ✅ ~~`IMD-02` resolver~~ ✅ ~~`IMD-03` importer~~ ✅ ~~`IMD-04` capture~~ ✅ ~~`IMD-05` reader~~ ✅ ~~`IMD-06` selector~~ ✅ ~~`IMD-07` backtester~~ ✅ ~~`IMD-08` validator~~ ✅ ~~`IMD-09` Edge Lab UI~~ ✅ ~~`IMD-10` gate~~ ✅

**Edge Lab Research Ledger (ERL) — next real build after measurement blockers**
- **NEXT build → `ERL-01`** Strategy Trial Registry. Save every backtest/parameter run with config hash, train/OOS split, costs, verdict, and reject reason so QuantG stops remembering only the lucky run.
- Build in order: `ERL-01` experiment registry → `ERL-02` robustness/overfit metrics → `ERL-03` regime tagging → `ERL-04` parameter heatmaps → `ERL-05` reject-reason engine → `ERL-06` evidence-weighted portfolio allocation → `ERL-07` frontend Edge Lab v2 panels.

**🏗 Backlog programs — do NOT start unless the founder directs:** Architecture redesign Stages 0–1 (event catalog / publish-only bus — see CLAUDE.md §11) · Phase-2 UI polish · capital allocator. Old Hermes `HSB-11..17` ratchet items are superseded by HSI + ERL and are no longer active queue items.

**🔧 OPS hygiene:** `OPS-01`, `OPS-02`, `OPS-03`, `OPS-05`, and `OPS-06..10` are done. `OPS-04` is a founder/env credential task, not an agent code task.

**🛠 Shipped 2026-07-06 (live session — all deployed to VPS):**
- `[x]` **OPS-06** Anti-pyramiding guard for spreads (`63e8e53`, `signal_manager.py`): hold-to-theta spread strategies re-opened a fresh spread every runner cycle (8+ stacked/underlying). Guard blocks a new spread entry when an active spread already exists for `(user, strategy, underlying, mode)`. Symbol-based dedup missed shifting strikes + SELL-entered credit spreads bypassed it.
- `[x]` **OPS-07** Spread legs subscribed to WS feed (`b3ba091`, `server.py _subscribe_open_position_tokens_on_startup`): spreads have no top-level `instrument_key`, so their `legs[]` were REST-priced every tick → 429 storm → dark marks (PNL shown ₹0.00). Now collect `legs[].instrument_key`; marks price off the warm WS cache.
- `[x]` **OPS-08** Manual exit + EXIT ALL no longer phantom-short (`79dce36`+`8efebda`): `/positions/{sym}/exit` and `/ops/squareoff-all` placed generic orders under a `manual_recovery` bucket → ledger nets by `(strategy_id,target_symbol)` → phantom SHORT / equity-skip on 0 price. Both now route through `_close_strategy_positions` (canonical path; equity+single-leg+spreads). **Rule: never close via a generic opposite order under manual_recovery — always `_close_strategy_positions`.**
- `[x]` **OPS-09** QG-O4 (SENSEX Call Spread Range Pilot) PAUSED (`manual_paused=true`): −₹1,850/day, no OOS edge on the call side (fights bull drift); QG-O3 already harvests the SENSEX put-spread edge.
- `[x]` **OPS-10** (`5695288`, deployed to VPS) Wire per-strategy 1-minute candle feed for QG-O5..O10. `_price_history` (server.py ~16994) now reads `visual_config.options.candle_interval` (default stays `"5minute"` — QG-O1..O4/all others untouched) and clamps lookback to 2 days for `"1minute"` (Upstox UDAPI1148 caps minute-history at ~1 month). Also fixed a latent bug found while wiring this: the live-tick bar's time-bucket flooring in `_fetch_strategy_history` hardcoded a 5-min bucket regardless of the requested interval — would have mis-timestamped the newest 1-min bar. `candle_interval` added to the QG-O5..O10 templates and synced via `migrate_user_to_v12_upstox` (survives startup re-sync per the DB-edits-get-overwritten rule). **Verified on VPS (market closed, so data-shape only):** DB shows QG-O5..O10 = `1minute`, QG-O1..O4 = `5minute`; a direct in-container call proved the 1-minute path returns 1,875 real NIFTY bars (source `upstox-v3-websocket+historical:1minute:NIFTY`) while the 5-minute path is unchanged (116 bars, `is_live=True`). No errors in startup logs, all containers healthy. **This only fixes the data path — does NOT validate the strategies.** Watch the next live session for QG-O5..O10 signals; results still must clear the IMD 1-min OOS gate (§14) before any promotion.
- **QG-O status (checked 07-06 EOD):** QG-O1 +₹452 & QG-O3 +₹816 working (both fire *unconditionally* in-window — time+bar gate only); QG-O2 1 position (trend-gated); QG-O4 PAUSED; QG-O5..O10 now receive correct 1-min data (OPS-10) — whether they actually fire is unverified until the next market session.

---

## INTRADAY MINUTE DATA / OPTIONS BACKTESTING (IMD) — QG-O5..QG-O10 JUDGE

**Created 2026-07-05 after the Options Alpha Rebuild pack was seeded for paper.**

**Goal:** build a legal, reproducible 1-minute options-history layer and no-lookahead intraday options backtester so QuantG can honestly judge intraday option buyers (`QG-O5`..`QG-O10`). The current EOD bhavcopy OOS engine is still the judge for held-to-expiry theta structures; it cannot prove ORB/VWAP/25-minute option-buying systems.

**Legal data rule:** no pirated datasets, scraped paid-data dumps, Telegram/Drive leaks, or mystery CSVs. They are not acceptable research evidence and must not enter `data/`, Mongo, Edge Lab, or strategy promotion reports. Allowed sources are broker/API data under the account's terms, official exchange/authorized vendor feeds, and open datasets with clear license/provenance.

**Researched legal sources (2026-07-05):**
- Upstox expired F&O historical candle API supports `1minute` candles for expired contracts and requires `expired_instrument_key`; docs show `UDAPI1149` when the endpoint requires Upstox Plus. Use this first because QuantG already uses Upstox. Source: https://upstox.com/developer/api-documentation/get-expired-historical-candle-data/
- Upstox V3 active historical candles support `minutes/1` from Jan 2022, but 1-15 minute intervals are limited to about one month per request window. Use for active/recent contracts, not the full old-history answer by itself. Source: https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/
- ExpiryTrack is an AGPL open-source Upstox Plus expired-F&O collector. Study its design, but do not vendor it blindly into QuantG. Source: https://github.com/marketcalls/ExpiryTrack
- Global Datafeeds and NSE authorized feeds are legal paid alternatives if Upstox Plus is insufficient. Sources: https://globaldatafeeds.in/apis/ and https://www.nseindia.com/static/market-data/real-time-data-subscription

**Architecture decision:** raw 1-minute candles must live in Parquet/DuckDB under the existing `data/` bind mount, not Mongo. Mongo may store only manifests, coverage summaries, and validation verdicts.

Target store shape:
```text
data/options_1m/
  source=upstox/
    underlying=NIFTY/
      year=2025/
        date=2025-07-04.parquet
```

Minimum candle schema:
```text
timestamp_ist, instrument_key, expired_instrument_key, underlying, expiry,
strike, option_type, open, high, low, close, volume, open_interest, source,
fetched_at, checksum
```

**Promotion law for intraday buyers:** `QG-O5`..`QG-O10` stay paper-observation hypotheses until `IMD-08` produces enough clean out-of-sample evidence. Do not tune their thresholds from one paper day.

---

### IMD-00 — Legal data-access proof and one-contract smoke test
- **Status**: `⛔ blocked` 2026-07-05 (Codex smoke test reached token validation only; stored Upstox token returned `UDAPI100050` invalid token before expired-instrument access could be tested)
- **Current status**: `[x]` DONE 2026-07-06: Upstox reconnect succeeded; legal expired F&O contract lookup and 1-minute candle fetch are usable through documented `/v2/expired-instruments/...` endpoints. The older blocked line above is retained as attempt history.
- **Tier**: 2
- **Session size**: 1-2 hours
- **Prerequisite**: none
- **Files to touch**: `TASKS.md` only for the status note; optional scratch script under `scratch/` if needed, but do not commit secrets or downloaded raw data.

**Goal:** prove the data source before building infra.

**Steps:**
1. Confirm whether the current Upstox account can call the expired F&O historical candle endpoint for `1minute`.
2. Fetch one known expired NIFTY weekly option contract for one trading day using `backend/brokers/upstox_gateway.py::get_expired_historical_candles_v3` if possible.
3. Record endpoint status, exact error code if blocked, candle count, timestamp timezone, and sample OHLC/OI fields.
4. If Upstox blocks with `UDAPI1149`, stop and mark this task blocked on Upstox Plus or an approved paid vendor.
5. Do not use unofficial/pirated data to bypass this gate.

**Attempt log:**
- 2026-07-05 Codex ran the smoke test inside `quantg-backend` without printing secrets. `broker_keys` exist and an access token is stored, but `get_user_upstox_status()` reported `token_state="expired"`, `token_valid=false`, `reconnect_required=true`, error code `UDAPI100050`.
- 2026-07-06 Codex reran the same VPS smoke test. Result unchanged: `token_present=true`, `token_state="expired"`, `token_valid=false`, `reconnect_required=true`, error code `UDAPI100050`; no expired-contract or candle endpoint was reached.
- Because token validation failed first, no conclusion was reached on Upstox Plus entitlement (`UDAPI1149`) or 1-minute candle availability.
- Current Upstox docs show the expired contracts and expired candles endpoints under `/v2/expired-instruments/...`; QuantG currently has `get_expired_historical_candles_v3()` using `/v3/expired-instruments/...`. When unblocked, test both the documented `/v2` path and the existing helper path, then correct the helper if needed.
- Resume command shape: get contracts from `/v2/expired-instruments/option/contract?instrument_key=NSE_INDEX|Nifty 50&expiry_date=YYYY-MM-DD`, choose one returned `instrument_key`, then fetch `/v2/expired-instruments/historical-candle/{expired_instrument_key}/1minute/{to_date}/{from_date}` for one date.
- 2026-07-06 after founder reconnected Upstox, Codex verified `token_state="valid"`, `token_valid=true`, `connected=true` from inside `quantg-backend` without printing secrets.
- `/v2/expired-instruments/option/contract?instrument_key=NSE_INDEX|Nifty 50&expiry_date=2025-01-09` returned 188 NIFTY contracts. `/v3/expired-instruments/option/contract` returned `UDAPI100060 Resource not Found`, proving the existing helper path was wrong.
- Picked `NIFTY 24200 CE 09 JAN 25`; `/v2/expired-instruments/historical-candle/{instrument_key}/1minute/2025-01-09/2025-01-09` returned 375 candles with 7 fields and IST timestamps; `2025-01-03..2025-01-09` returned 1,875 one-minute candles. Invalid interval `minute` returned `UDAPI1147`; valid interval is `1minute`.
- Corrective code note: `backend/brokers/upstox_gateway.py::get_expired_historical_candles_v3` now uses the documented v2 endpoint through a v2 helper and stays as a compatibility wrapper.

**Acceptance:**
- One legal source is confirmed usable OR the task clearly records the blocker and the subscription/vendor needed.
- Sample output includes at least one 1-minute candle with timestamp, OHLC, volume, and OI.
- No strategy/backtester code is written yet.

**How to verify:**
```powershell
python -m py_compile backend\brokers\upstox_gateway.py
```

---

## EDGE LAB RESEARCH LEDGER (ERL) — USE HISTORICAL DATA AS A STRATEGY JUDGE

**Created 2026-07-06 after Edge Lab proved QG-O1/QG-O4 can produce real historical samples once live-style strategies are replayed day-by-day.**

**Goal:** turn the historical EOD option store into a strategy research operating system, not a one-off P&L table. QuantG should remember every idea tested, penalize overfit/fragile ideas, explain reject reasons, and only promote strategies that survive train/OOS, costs, regimes, nearby parameters, and forward-paper evidence.

**Research law:** do not tune strategies by eyeballing one green backtest. Every candidate must go through: hypothesis → saved experiment → train/OOS result → robustness/overfit check → forward-paper checkpoint → founder-gated live promotion. If many variants were tested, the winner must be penalized for data snooping.

### ERL-01 — Strategy Trial Registry
- **Status**: `[x]` DONE 2026-07-09 (`strategy_trials`, deterministic config/window hash, deduplicated run count, list/history API)
- **Tier**: 2
- **Session size**: 3-5 hours
- **Prerequisite**: current EOD Edge Lab working
- **Files to touch**: `backend/core/strategy_trial_registry.py` (new), `backend/routes/ops.py`, `backend/scripts/build_edge_lab_snapshot.py`, `backend/tests/test_strategy_trial_registry.py` (new)

**Goal:** persist every OOS/Edge Lab experiment with enough metadata to reproduce and audit it.

**Steps:**
1. Define a trial document schema: strategy id/name, config hash, python_code hash, parameter overrides, data window, train/OOS split, slippage/cost assumptions, source store coverage, metrics, verdict, reject reason, created_at, run_id.
2. Save a trial row for Edge Lab OOS runs and proposer runs; keep raw candles/trades out of Mongo unless compacted.
3. De-duplicate identical config/data-window trials by hash while keeping run timestamps.
4. Add a route to list recent trials and fetch a strategy's trial history.

**Acceptance:**
- Re-running the same strategy/config creates a deterministic hash and does not flood duplicate trial rows.
- A future agent can answer "what did we test and why did we reject/promote it?" from Mongo alone.

**How to verify:**
```powershell
python -m pytest backend\tests\test_strategy_trial_registry.py -q
```

---

### ERL-02 — Robustness and Overfit Metrics
- **Status**: `[x]` DONE 2026-07-09 (sample, expectancy, green-month, plateau and multiple-testing robustness score)
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `ERL-01`
- **Files to touch**: `backend/core/research_metrics.py` (new), `backend/core/eod_options_backtest.py`, `backend/core/edge_lab.py`, `backend/tests/test_research_metrics.py` (new)

**Goal:** judge whether a green backtest is robust or likely curve-fit.

**Steps:**
1. Add metrics: profit factor, max drawdown, worst month, worst year, green-month %, slippage sensitivity, sample-size status, and nearby-parameter stability.
2. Add a conservative Deflated-Sharpe-style score or simpler "multiple-testing penalty" field for large sweeps.
3. Add an overfit warning when the best cell is isolated while nearby cells fail.
4. Keep the first implementation deterministic and transparent; do not add ML.

**Acceptance:**
- Edge Lab can mark a candidate as `CANDIDATE_EDGE_BUT_FRAGILE_TO_COSTS` or `OVERFIT_RISK` instead of just green/red.
- Unit tests cover a robust plateau, one lucky isolated cell, and a high-P&L/high-drawdown failure.

**How to verify:**
```powershell
python -m pytest backend\tests\test_research_metrics.py -q
```

---

### ERL-03 — Market Regime Tagging for Historical Backtests
- **Status**: `[x]` DONE 2026-07-09 (no-lookahead trend/vol/gap/large-move tags and per-regime expectancy)
- **Tier**: 2
- **Session size**: 4-6 hours
- **Prerequisite**: `ERL-01`
- **Files to touch**: `backend/core/historical_regimes.py` (new), `backend/core/eod_options_backtest.py`, `backend/tests/test_historical_regimes.py` (new)

**Goal:** explain when a strategy works or fails.

**Steps:**
1. Tag each historical day/trade with simple deterministic regimes: uptrend, downtrend, range, high-vol, low-vol, gap day, expiry week, large-move day.
2. Aggregate expectancy and hit-rate by regime in OOS results.
3. Add clear labels to candidate/reject output: "works in range/uptrend, fails in crash/downtrend" etc.

**Acceptance:**
- QG-O1/QG-O4 reports include regime breakdowns.
- Regime labels are deterministic from the same candle store and have no future lookahead.

**How to verify:**
```powershell
python -m pytest backend\tests\test_historical_regimes.py -q
```

---

### ERL-04 — Parameter Heatmaps and Plateau Detection
- **Status**: `[x]` DONE 2026-07-09 (sweep heatmaps and positive-neighbor plateau score)
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `ERL-02`
- **Files to touch**: `backend/core/edge_lab.py`, `backend/routes/ops.py`, `frontend/src/pages/Analytics.jsx`, optional `frontend/src/components/analytics/*`

**Goal:** make nearby-parameter robustness visible, not just the best result.

**Steps:**
1. Persist heatmap cells for OTM %, wing width, min/max DTE, exit mode, and cost/slippage assumptions.
2. Add plateau score: how many neighboring cells remain positive OOS.
3. Frontend: display compact heatmap panels and highlight isolated winners as risky.

**Acceptance:**
- Edge Lab can show "this edge exists across a region" vs "one lucky cell."
- UI labels train/OOS and costs clearly.

**How to verify:**
```powershell
python -m pytest backend\tests\test_ops_edge_lab.py -q
cd frontend
$env:CI='false'; npm run build
```

---

### ERL-05 — Reject-Reason Engine
- **Status**: `[x]` DONE 2026-07-09 (explicit sample/OOS/consistency/data-domain reject reasons)
- **Tier**: 2
- **Session size**: 3-5 hours
- **Prerequisite**: `ERL-02`
- **Files to touch**: `backend/core/strategy_reject_reasons.py` (new), `backend/core/edge_lab.py`, `backend/tests/test_strategy_reject_reasons.py` (new)

**Goal:** make every failed strategy useful by explaining exactly why it failed.

**Reject reasons to support:**
- too few trades
- negative OOS expectancy
- only one lucky year
- fails after realistic costs/slippage
- drawdown too high
- unstable nearby parameters
- works only in one regime
- daily EOD data cannot fairly judge intraday strategy
- data-quality gap

**Acceptance:**
- Edge Lab rows have a `reject_reasons` array and a human-readable primary reason.
- A `NO_EDGE_NEGATIVE` or `INSUFFICIENT_DATA` row never shows as a meaningless all-zero row.

**How to verify:**
```powershell
python -m pytest backend\tests\test_strategy_reject_reasons.py -q
```

---

### ERL-06 — Evidence-Weighted Portfolio Allocation
- **Status**: `[x]` DONE 2026-07-09 (paper-only evidence weights and recommended capital; never auto-applies)
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `ERL-05` and at least one forward-paper checkpoint
- **Files to touch**: `backend/core/evidence_allocator.py` (new), `backend/routes/ops.py`, `frontend/src/pages/Analytics.jsx`, `backend/tests/test_evidence_allocator.py` (new)

**Goal:** convert strategy evidence into paper capital recommendations without auto-enabling live trading.

**Steps:**
1. Define allocation tiers: candidate edge, fragile, insufficient data, no edge, data-quality fail.
2. Weight by OOS expectancy, drawdown, robustness score, correlation/family, and forward-paper result.
3. Output recommended paper capital and promotion status; do not mutate strategies automatically.

**Acceptance:**
- QuantG can say "QG-O1 deserves larger paper allocation than QG-O5" with evidence fields.
- Live promotion remains founder-gated and `CORE_ENGINE_LIVE_ENABLED=false`.

**How to verify:**
```powershell
python -m pytest backend\tests\test_evidence_allocator.py -q
```

---

### ERL-07 — Edge Lab v2 Frontend Panels
- **Status**: `[x]` DONE 2026-07-09 (research ledger ladder, parameter plateaus and paper allocation in Analytics)
- **Tier**: 2
- **Session size**: 4-8 hours
- **Prerequisite**: `ERL-05`
- **Files to touch**: `frontend/src/pages/Analytics.jsx`, optional `frontend/src/components/analytics/*`, `frontend/src/index.css` only if required

**Goal:** make the research judge understandable in the app.

**Panels:**
- Strategy trial history
- Verdict and reject reasons
- Regime performance
- Robustness/overfit warnings
- Parameter heatmap
- Promotion ladder: rejected → watch → paper-forward → founder-gated live

**Acceptance:**
- The user can open Analytics and understand whether a strategy is good, fragile, unproven, or rejected without reading logs.
- The UI does not imply paper P&L alone proves edge.

**How to verify:**
```powershell
cd frontend
$env:CI='false'; npm run build
```

---

## HERMES INTELLIGENCE RESEARCH BRAIN (HIRB) — TRUTHFUL QUANT RESEARCH OS

**Created 2026-07-09 after external research into agentic trading systems, QRAFTI-style typed research workflows, TradingAgents-style specialist desks, TrustTrade-style selective evidence trust, and LLM backtest leakage risks.**

**Goal:** expand Hermes beyond an in-app assistant into a disciplined quant research brain: it proposes hypotheses, gathers evidence, runs math, tries to falsify itself, stores verdicts, and recommends human-gated research actions. It never places trades, never enables live trading, and never treats an LLM narrative as proof.

**Operating law:** Hermes can narrate and reason, but only deterministic code, clean data, OOS/walk-forward validation, cost/slippage stress, and forward-paper evidence decide what is true. Every research claim must carry source, timestamp, sample size, confidence, stale state, limitations, and a verdict.

### HIRB-01 — Truthful Research Context Contract
- **Status**: `[x]` DONE 2026-07-09 (Codex). `backend/routes/ai.py` now routes strategy scoring, explained scoring, market analysis, and training-context payloads through one current-domain research context helper. Retired commodity/MCX/HFT context is explicit metadata, not a dead route call. `backend/tests/test_agent_tools.py` pins the contract.
- **Tier**: 2
- **Session size**: 2-4 hours
- **Prerequisite**: HSI stages shipped; existing `/ai` and `/agent` routes working
- **Files to touch**: `backend/routes/ai.py`, `backend/tests/test_agent_tools.py`, `TASKS.md`, `wiki/memory.md`

**Goal:** clean the Hermes/AI data-source wiring before adding intelligence. Stale commodity/HFT-era context must not enter Hermes research prompts, and research payloads must explicitly declare their truth quality.

**Steps:**
1. Remove `commodity_watchlist` dependencies from AI strategy scoring, explained scoring, and training-context payloads.
2. Build one current-domain market context helper for NSE/BSE/Upstox V3 watchlist rows.
3. Add a standard `research_context` payload with source, generated_at, sample_n, confidence, stale, limitations, and domain.
4. Keep existing API shapes backward-compatible where practical, but mark removed domains as `deprecated`/empty rather than calling dead routes.
5. Add focused tests that prove the AI routes do not call `commodity_watchlist` and expose the research context metadata.

**Acceptance:**
- Hermes scoring/training-context endpoints work when commodity routes are absent or retired.
- Returned context clearly says it covers current QuantG domains only: NSE/BSE index options, equities, Upstox V3, OOS/forward-paper research.
- No trading behavior changes and no live-enablement flags changed.

**How to verify:**
```powershell
python -m pytest backend\tests\test_agent_tools.py -q
python -m py_compile backend\routes\ai.py
```

---

### HIRB-02 — Research Hypothesis Ledger
- **Status**: `[x]` DONE 2026-07-09 (Codex). Added `backend/core/research_ledger.py` plus `/api/ops/research/hypotheses` create/list/get/evidence/verdict routes. Hypotheses dedupe by canonical hash, carry evidence links and verdicts, and never mutate strategy configs.
- **Tier**: 2
- **Session size**: 4-6 hours
- **Prerequisite**: `HIRB-01`
- **Files to touch**: `backend/core/research_ledger.py` (new), `backend/routes/ops.py` or `backend/routes/ai.py`, `backend/tests/test_research_ledger.py` (new)

**Goal:** every idea becomes a structured, testable hypothesis instead of a chat note.

**Schema:** hypothesis, market premise, instrument, timeframe, entry/exit idea, data window, null hypothesis, expected edge, cost model, risk thesis, failure modes, evidence links, status, verdict, created_by, created_at.

**Acceptance:** Hermes can save/list/fetch hypotheses and attach evidence/verdict ids without mutating strategy configs.

---

### HIRB-03 — Quant Math Kernel
- **Status**: `[x]` DONE 2026-07-09 (Codex). Added `backend/core/quant_research_metrics.py` with deterministic expectancy, payoff, profit factor, max drawdown, CVaR, bootstrap CI, Monte Carlo drawdown, fractional Kelly, Bayesian win-rate, slippage stress, and multiple-testing penalty summary.
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `HIRB-02`
- **Files to touch**: `backend/core/quant_research_metrics.py` (new), tests

**Goal:** centralize research math: expectancy, R multiple, payoff skew, profit factor, max drawdown, CVaR, bootstrap confidence, Monte Carlo path risk, fractional Kelly, Bayesian hit-rate confidence, slippage stress, and multiple-testing penalty.

**Acceptance:** deterministic unit tests cover robust edge, fragile edge, overfit winner, and high-win-rate negative-expectancy cases.

---

### HIRB-04 — Hermes Verifier and Critic Loop
- **Status**: `[x]` DONE 2026-07-09 (Codex). Added `backend/core/research_critic.py` plus Hermes tools `get_research_hypotheses`, `get_research_critique`, and `get_research_desk`. The critic enforces allowed claim strength from evidence, OOS/forward-paper/cost coverage, sample size, confidence, contradictions, and falsification tests.
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `HIRB-02`, `HIRB-03`
- **Files to touch**: `backend/routes/ai.py`, `backend/core/research_critic.py` (new), tests

**Goal:** every serious Hermes research answer follows: plan → retrieve → compute → challenge → verify → summarize. The answer must state what would falsify it and what data is missing.

**Acceptance:** Hermes refuses to label ideas as proven without OOS/forward-paper evidence and includes contradiction/limitation notes in research answers.

---

### HIRB-05 — Frozen Evidence and Anti-Leakage Store
- **Status**: `[x]` DONE 2026-07-09 (Codex). Added `backend/core/evidence_store.py` to freeze source snapshots with observed_at/collected_at, content hash, source metadata, dedupe, as-of listing, and explicit anti-leakage rules.
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `HIRB-02`
- **Files to touch**: `backend/core/evidence_store.py` (new), optional scripts/tests

**Goal:** timestamp external/news/context evidence so historical research cannot accidentally leak future-updated web data into backtests.

**Acceptance:** a research run can cite exactly what Hermes knew at collection time; future-updated sources are not silently reused as historical evidence.

---

### HIRB-06 — Multi-Agent Quant Research Desk
- **Status**: `[x]` DONE 2026-07-09 (Codex). Added `backend/core/research_agents.py`, a deterministic specialist research desk with regime, volatility, flow, strategy scientist, backtest engineer, risk officer, execution-cost auditor, skeptic, memory curator, and founder-briefing outputs. Outputs are evidence/review objects only, not trades or config mutations.
- **Tier**: 3
- **Session size**: 8-12 hours
- **Prerequisite**: `HIRB-04`
- **Files to touch**: `backend/core/research_agents.py` (new), `backend/routes/ai.py`, tests

**Goal:** specialist roles with strict schemas: regime analyst, volatility analyst, flow analyst, strategy scientist, backtest engineer, risk officer, execution-cost auditor, skeptic, memory curator, founder-briefing agent.

**Acceptance:** agents debate only inside research workflows; their outputs are evidence objects, not trade orders or config mutations.

---

### HIRB-07 — Hermes Research Lab UI
- **Status**: `[x]` DONE 2026-07-09 (Codex). Added a Research tab to `frontend/src/pages/AIBot.jsx` that loads `/api/ops/research/hypotheses`, shows hypothesis/verdict/evidence/sample/confidence/limitations, summarizes validated/watch/rejected counts, and lets the founder ask Hermes to critique a hypothesis through the new read-only HIRB tools.
- **Tier**: 2
- **Session size**: 6-10 hours
- **Prerequisite**: `HIRB-02`, `HIRB-04`
- **Files to touch**: `frontend/src/pages/AIBot.jsx` or new research components, backend list endpoints if needed

**Goal:** expose active hypotheses, test runs, verdicts, sample sizes, confidence, contradictions, and pending founder-approved research actions.

**Acceptance:** the founder can see what Hermes believes, why it believes it, what failed, and what should be tested next.

---

### IMD-01 — Define the 1-minute options store schema and manifest
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). `backend/core/options_minute_schema.py` + `backend/tests/test_options_minute_schema.py` (15 tests, pass). Canonical 16-field candle row (`CANDLE_FIELDS`), IST timestamp normalization, per-row content checksum + order-independent manifest checksum, hard-fail reasons (row-too-short/bad-timestamp/non-numeric/negative/high<low/ohlc-out-of-range) vs collection DQ flags (zero-volume/dup-ts/non-monotonic/missing-oi). Pure logic, no broker/fs.
- **Tier**: 2
- **Session size**: 2-3 hours
- **Prerequisite**: `IMD-00` usable legal data source
- **Files to touch**: `backend/core/options_minute_schema.py` (new), `backend/tests/test_options_minute_schema.py` (new), optional `docs/OPTIONS_1M_DATA.md`

**Goal:** create a small, testable schema layer before any downloader exists.

**Steps:**
1. Define normalized candle fields and a manifest record shape.
2. Include source, fetch window, checksum, row count, first/last timestamp, and data-quality flags.
3. Add helpers to normalize Upstox candle arrays into typed dicts.
4. Keep this pure-logic; no broker calls and no filesystem writes in unit tests.

**Acceptance:**
- Upstox raw candle arrays normalize into QuantG's canonical candle schema.
- Bad rows fail validation with explicit reasons.
- Manifest checksum is deterministic.

**How to verify:**
```powershell
python -m pytest backend\tests\test_options_minute_schema.py -q
```

---

### IMD-02 — Expired-contract resolver for historical option universe
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). `backend/core/expired_option_resolver.py` + gateway methods `get_expired_option_expiries`/`get_expired_option_contracts` (documented `/v2/expired-instruments/...` paths) + `backend/tests/test_expired_option_resolver.py` (16 tests, pass). `resolve(underlying,trade_date,expiry,strike,option_type)` → `ResolveResult` with a stable `expired_instrument_key` or a typed reason (`STRIKE_NOT_FOUND`/`EXPIRY_NOT_FOUND`/`BLOCKED_UNDERLYING`/`UNSUPPORTED_UNDERLYING`/`OPTION_TYPE_INVALID`/`FETCH_ERROR`) — never a guessed symbol. NIFTY+BANKNIFTY supported; SENSEX/BANKEX blocked. Helpers `select_weekly_expiry`, `atm_strike`, `resolve_atm`. Per-(underlying,expiry) mem + optional on-disk cache; tokens never persisted. Injected fetchers → unit-testable with no broker; `from_gateway()` wires the real path.
- **Tier**: 3
- **Session size**: 4-6 hours
- **Prerequisite**: `IMD-01`
- **Files to touch**: `backend/core/expired_option_resolver.py` (new), `backend/brokers/upstox_gateway.py`, `backend/tests/test_expired_option_resolver.py` (new)

**Goal:** resolve the exact historical contracts the backtester would have selected, without downloading the whole exchange.

**Steps:**
1. Add a resolver that maps `(underlying, trade_date, expiry, strike, option_type)` to Upstox `expired_instrument_key`.
2. Cache resolved contract metadata in a local manifest collection/file; do not store access tokens.
3. Support at least `NIFTY` and `BANKNIFTY`; leave `SENSEX/BFO` blocked until a legal BSE path is confirmed.
4. Add tests for weekly expiry selection, ATM rounding, CE/PE, and missing contract behavior.

**Acceptance:**
- Given one historical date and target strike, resolver returns a stable expired key or a typed `NOT_FOUND` reason.
- No fallback to string-guessed symbols when the API has not confirmed the contract.

**How to verify:**
```powershell
python -m pytest backend\tests\test_expired_option_resolver.py -q
```

---

### IMD-03 — Bounded Upstox 1-minute importer to Parquet/DuckDB
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). `core/options_minute_store.py` (write side) + `scripts/options_1m_ingest_upstox.py` + `tests/test_options_minute_store.py`/`test_options_1m_ingest.py` (13 tests). NOTE: store format is **gzipped CSV per contract-day** (same house pattern as `core/bhavcopy_store`), NOT Parquet — pyarrow/duckdb are not installed and adding them is a founder-gated rebuild; the `./data` mount is already writable. Layout `data/options_1m/source=upstox/underlying=NIFTY/year=2025/date=YYYY-MM-DD/{strike}{CE|PE}_exp{YYYYMMDD}.csv.gz` + `.manifest.json` sidecar. Importer: pure `plan_contract_days` (ATM±N via bhavcopy spot, weekly expiry via resolver) + `ingest` (idempotent `is_clean` skip unless `--force`, `--dry-run` prints planned count). Bounded to NIFTY/BANKNIFTY, ATM±5, CE+PE.
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `IMD-02`
- **Files to touch**: `backend/scripts/options_1m_ingest_upstox.py` (new), `backend/core/options_minute_store.py` (new skeleton), `backend/tests/test_options_1m_ingest.py` (new), `docker-compose.yml` only if the data mount needs a new path

**Goal:** fetch only the contracts QuantG needs for `QG-O5`..`QG-O10`, not every strike in India.

**Steps:**
1. Implement CLI args: `--from`, `--to`, `--underlyings`, `--strikes-around-atm`, `--expiry-mode weekly`, `--source upstox`, `--dry-run`.
2. Default scope: NIFTY/BANKNIFTY, weekly expiry, ATM +/- 5 strikes, CE+PE.
3. Use resolver from `IMD-02`, fetch `1minute` candles, normalize with `IMD-01`, write partitioned Parquet or DuckDB.
4. Add manifest rows with checksum, source, row count, and fetch status.
5. Rate-limit and resume idempotently; never re-fetch a clean existing contract-day unless `--force`.

**Acceptance:**
- Dry-run shows planned contract-day count before fetching.
- Re-running the same command skips already clean files.
- One week of NIFTY data can be ingested without duplicate rows.

**How to verify:**
```powershell
python -m pytest backend\tests\test_options_1m_ingest.py -q
python backend\scripts\options_1m_ingest_upstox.py --from 2025-01-06 --to 2025-01-10 --underlyings NIFTY --strikes-around-atm 2 --dry-run
```

---

### IMD-04 — Forward live 1-minute option-candle capture
- **Status**: `[x]` DONE + **WIRED 2026-07-06** (Claude/Opus). Module `core/options_minute_capture.py` (6 tests) + **live wiring deployed**: `UpstoxMarketDataFeedV3.add_tick_listener` (guarded — a listener error can never break the feed/trading loop) + `core/live_index_capture.py` (`LiveIndexCapture`) attached at startup after the baseline subscribe; `server.py` `_daily_scheduler_loop` flushes captured index 1-min bars at **15:35 IST** to the new `core/index_minute_store.py`. This closes the **underlying INDEX 1-min** dependency IMD-08 needed (forward). Full option-contract forward-capture (token→`OptionContractRef`) is a follow-up — the aggregator + options store already support it once refs are wired.
- **Tier**: 3
- **Session size**: 4-8 hours
- **Prerequisite**: `IMD-01`; can run in parallel with `IMD-03` after schema is fixed
- **Files to touch**: `backend/core/options_minute_capture.py` (new), `backend/brokers/upstox_market_data_v3.py`, `backend/server.py` startup wiring only if required, `backend/tests/test_options_minute_capture.py` (new)

**Goal:** build QuantG's own legal forward dataset every market day from the live feed.

**Steps:**
1. Subscribe only to selected option contracts that strategies could trade, not the entire chain.
2. Aggregate ticks/LTP updates into 1-minute OHLCV/OI where available.
3. Flush completed minute bars into the same store schema as imported history.
4. Add health counters: subscribed contracts, bars written, missing minutes, stale feed seconds.
5. Keep capture read-only with respect to trading decisions.

**Acceptance:**
- During market hours, one selected NIFTY CE/PE writes valid minute bars.
- If feed is absent, capture records a clear data-quality gap and does not fabricate bars.

**How to verify:**
```powershell
python -m pytest backend\tests\test_options_minute_capture.py -q
```

---

### IMD-05 — Options minute store reader and coverage report
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). Read side of `core/options_minute_store.py` (shipped with IMD-03) + `tests/test_options_minute_store.py`. `get_option_minutes`, `get_chain_at_time` (no-lookahead chain snapshot: last bar ≤ ts per contract), `missing_minutes` (deterministic vs the 375-bar IST session grid), `coverage` (by underlying/date/contract count/rows), `trading_days`. Missing contracts return TYPED EMPTY — never a nearest-strike substitute. IST-safe throughout.
- **Tier**: 2
- **Session size**: 3-5 hours
- **Prerequisite**: `IMD-03`
- **Files to touch**: `backend/core/options_minute_store.py`, `backend/tests/test_options_minute_store.py` (new)

**Goal:** give the backtester a clean API over Parquet/DuckDB.

**Steps:**
1. Implement `get_option_minutes(...)`, `get_chain_at_time(...)`, `coverage(...)`, and `missing_minutes(...)`.
2. Return typed empty results for missing contracts; do not silently use nearest strikes unless the caller asks.
3. Include timezone-safe IST handling.
4. Add coverage summary by date, underlying, expiry, strike count, and row count.

**Acceptance:**
- Store reader can reconstruct an ATM option chain snapshot at a timestamp.
- Missing-data reports are deterministic and visible to callers.

**How to verify:**
```powershell
python -m pytest backend\tests\test_options_minute_store.py -q
```

---

### IMD-06 — Historical no-lookahead option selection replay
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). `core/intraday_option_selector.py` + `tests/test_intraday_option_selector.py` (7 tests). `select_contract(...)` picks single_leg or debit_spread legs from a chain snapshot (built by `get_chain_at_time`, so no lookahead by construction) + spot; ATM rounding via market_domains strike interval, OTM offset, wing exactly `spread_width` strikes further OTM (never snapped — the width IS the risk). Missing leg → typed `MISSING_LEG`. No-lookahead test proves selection is a pure function of the passed snapshot.
- **Tier**: 3
- **Session size**: 4-6 hours
- **Prerequisite**: `IMD-05`
- **Files to touch**: `backend/core/intraday_option_selector.py` (new), `backend/core/option_selector_v2.py` only if shared pure helpers are extracted, `backend/tests/test_intraday_option_selector.py` (new)

**Goal:** reproduce live option selection historically without peeking at future candles.

**Steps:**
1. Select ATM/OTM contracts at a given timestamp using only the chain available at or before that timestamp.
2. Support single-leg and debit-spread construction for QG-O5..QG-O10.
3. Add spread leg pairing rules, lot sizes from `market_domains`, and missing-leg rejection reasons.
4. Add tests that prove future timestamps do not influence selection.

**Acceptance:**
- Given an underlying minute candle and option chain snapshot, selector returns the same class of contract the live app would trade.
- No lookahead tests fail if future candles are injected.

**How to verify:**
```powershell
python -m pytest backend\tests\test_intraday_option_selector.py -q
```

---

### IMD-07 — Intraday options backtest engine
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). `core/intraday_options_backtest.py` + `tests/test_intraday_options_backtest.py` (7 tests, hand-computed exact P&L). `run_day(...)` — deterministic minute event loop: signal → no-lookahead select → track net premium on the contract's own candles. Exit priority fixed STOP→TARGET→TRAILING→TIME/SQUAREOFF; R-based levels (R=entry net premium); slippage+brokerage; missing option price fails closed (exit at last-known mark + dq flag, never invents a fill). Emits Trade rows (entry/exit ts, gross/net P&L, R-multiple, MFE, MAE, exit_reason, dq flags). single_leg + debit_spread (buyers first).
- **Tier**: 3
- **Session size**: 8-12 hours
- **Prerequisite**: `IMD-06`
- **Files to touch**: `backend/core/intraday_options_backtest.py` (new), `backend/tests/test_intraday_options_backtest.py` (new)

**Goal:** replay minute-by-minute option trades with realistic exits and costs.

**Steps:**
1. Build a deterministic event loop over underlying minutes and selected option minutes.
2. Execute entries from strategy signals, then price fills on the selected option/spread candles.
3. Model brokerage/slippage, max hold minutes, TP/SL/trailing stop, same-day force squareoff, and missing-price exits.
4. Support `single_leg`, `debit_spread`, and `credit_spread`; start with buyer strategies first.
5. Emit trade rows compatible with Edge Lab/Hermes attribution concepts: setup, entry/exit timestamp, gross/net P&L, MFE, MAE, reason, data-quality flags.

**Acceptance:**
- A tiny fixture with known candles produces exact expected P&L.
- Stop/target/time-exit order is deterministic when multiple events happen in one candle.
- Backtest fails closed on missing prices instead of inventing fills.

**How to verify:**
```powershell
python -m pytest backend\tests\test_intraday_options_backtest.py -q
```

---

### IMD-08 — QG-O5..QG-O10 intraday OOS validator
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). `core/intraday_options_oos.py` (pure metrics+verdict, 8 tests) + `scripts/run_intraday_options_validation.py` (orchestration, 4 helper tests). `evaluate_strategy` → temporal walk-forward (hold out latest months) → verdict CANDIDATE_EDGE/FRAGILE/NO_EDGE_NEGATIVE/INSUFFICIENT_DATA/DATA_QUALITY_FAIL against a `GATE` (min 30 trades / 3 months / ≤20% missing / OOS expectancy>0 / ≥50% green months). Metrics: trades, net P&L, expectancy, win rate, profit factor, max drawdown, avg MFE/MAE, avg hold. Script: `compile_signal_fn` wraps a strategy's `run(data)`, replays per day, persists to `db.intraday_options_oos_runs`. **JUDGE-FIRST: returns INSUFFICIENT_DATA until real 1-min data + UNDERLYING INDEX 1-min candles exist** (the outstanding data dependency — see IMD-04 note).
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `IMD-07` and at least 3 months clean NIFTY/BANKNIFTY minute data
- **Files to touch**: `backend/scripts/run_intraday_options_validation.py` (new), `backend/core/intraday_options_oos.py` (new), `backend/tests/test_intraday_options_oos.py` (new)

**Goal:** judge the seeded intraday buyers with sample-size-aware OOS metrics.

**Steps:**
1. Load QG-O5..QG-O10 strategy templates from `server.py` or a read-only exported config.
2. Run train/test splits by month and by rolling walk-forward window.
3. Report trades, net P&L, expectancy, win rate, profit factor, max drawdown, MFE/MAE, average hold, skipped/missing-data counts, and OOS expectancy.
4. Verdicts: `CANDIDATE_EDGE`, `FRAGILE`, `NO_EDGE_NEGATIVE`, `INSUFFICIENT_DATA`, `DATA_QUALITY_FAIL`.
5. Store summary in Mongo `intraday_options_oos_runs` but keep raw candles out of Mongo.

**Acceptance:**
- One command validates all QG intraday buyers and prints a ranked scorecard.
- A strategy cannot pass if sample size is too small or data-quality coverage is poor.

**How to verify:**
```powershell
python -m pytest backend\tests\test_intraday_options_oos.py -q
python backend\scripts\run_intraday_options_validation.py --strategies QG-O5,QG-O6 --from 2025-01-01 --to 2025-03-31
```

---

### IMD-09 — Edge Lab API and UI for intraday OOS
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). Backend: `GET /ops/intraday-oos` (latest run + minute-data coverage) + `POST /ops/intraday-oos/refresh` (background validation) in `routes/ops.py`. Frontend: `IntradayOOS` panel in the Analytics Edge Lab tab (`Analytics.jsx`) — coverage stats + verdict table, **clearly labelled "Intraday 1m OOS — QG-O5…O10" and kept visually separate from the EOD theta OOS** so the two judges aren't mixed. Read-only; no seed/tune controls. Frontend build verified clean.
- **Tier**: 3
- **Session size**: 6-10 hours
- **Prerequisite**: `IMD-08`
- **Files to touch**: `backend/routes/ops.py`, `backend/core/edge_lab.py`, `frontend/src/pages/Analytics.jsx`, optional `frontend/src/components/analytics/*`, tests if existing patterns allow

**Goal:** make the intraday evidence visible beside the existing EOD OOS evidence.

**Steps:**
1. Add backend endpoints for latest intraday OOS summary and background refresh.
2. Add Edge Lab panels: minute-data coverage, strategy verdict table, best/worst expectancy, data-quality warnings.
3. Clearly label EOD theta OOS vs Intraday 1m OOS so users do not mix the judges.
4. Do not add controls that seed/tune strategies from the UI.

**Acceptance:**
- Analytics page shows QG-O5..QG-O10 verdicts and coverage.
- Refresh is backgrounded and cached like current Edge Lab.

**How to verify:**
```powershell
python -m pytest backend\tests\test_ops_edge_lab.py -q
cd frontend
$env:CI='false'; npm run build
```

---

### IMD-10 — Data-quality gate and promotion checklist
- **Status**: `[x]` DONE 2026-07-06 (Claude/Opus). Gate thresholds live in code (`core/intraday_options_oos.GATE`: 30 trades / 3 months / ≤20% missing / OOS>0 / ≥50% green). Documented the full IMD pipeline + intraday promotion ladder + daily forward-capture health checklist in CLAUDE.md §14; updated the Options Alpha pack wiki note with the final ladder. Live promotion stays founder-gated, `CORE_ENGINE_LIVE_ENABLED=false`.
- **Tier**: 2
- **Session size**: 3-5 hours
- **Prerequisite**: `IMD-09`
- **Files to touch**: `CLAUDE.md`, `TASKS.md`, `wiki/Projects/Options Alpha Rebuild Strategy Pack 2026-07-05.md`, optional backend constants if a gate is coded

**Goal:** prevent a pretty intraday backtest from becoming a money strategy without evidence discipline.

**Steps:**
1. Define pass thresholds for intraday buyers: minimum trades, minimum months, max missing-minute rate, positive OOS expectancy after costs, drawdown cap, and forward-paper requirement.
2. Document that live promotion still requires founder approval and `CORE_ENGINE_LIVE_ENABLED` remains false by default.
3. Add a checklist for daily forward data capture health.
4. Update the Options Alpha pack note with the final promotion ladder.

**Acceptance:**
- Future agents can tell whether an intraday strategy is blocked by data, OOS result, or forward-paper result.
- The app docs do not imply paper P&L alone proves an edge.

**How to verify:**
```powershell
rg -n "IMD-|intraday.*OOS|1-minute options" TASKS.md CLAUDE.md wiki
```

---

## OPS HYGIENE — from live full-system audit (2026-07-02, market open, no changes made)

**Audit verdict: system HEALTHY and trading correctly.** Real feed (0 mock fallbacks), 57 fills today, MTM fresh (2–3s), no stuck EXITING/CIRCUIT_BREAKER positions, HSI brain intact (attribution 06-30/07-01, 10 candidate lessons scored 07-01, daily_reports through 07-01), self-healing wallet ledger working. Below are the non-urgent cleanups found — **none affect trading correctness or money integrity; do after 15:30 IST.**

- `[x]` **OPS-01 (P1) — Upstox portfolio-stream 401 storm, no backoff.** DONE 2026-07-02: backend now only starts the Upstox portfolio stream when `CORE_ENGINE_LIVE_ENABLED=true`; paper mode keeps REST reconciliation but skips the portfolio WS handshake entirely. Commit: `18f70fd`.
- `[x]` **OPS-02 (P2) — 429 rate-limiting on `/v2/market-quote/ltp`.** CLOSED 2026-07-06 by OPS-07. Root cause was spread legs being REST-priced every tick because spread docs had no top-level `instrument_key`; subscribing `legs[].instrument_key` to the WS feed moved marks to the warm cache and removed the 429 storm. Reopen only if fresh market-hours logs show new `/v2/market-quote/ltp` 429s after OPS-07.
- `[x]` **OPS-03 (P3) — RELIANCE Trend Rider orphaned-paused.** DONE 2026-07-03: RELIANCE was exactly orphaned (`status=paused`, `manual_paused=false`, `schedule_paused=false`) with 0 open positions, 0 pending signals, 0 open orders. After the equity ATR/deadline fixes, production Mongo now has `status=paused`, `manual_paused=false`, `schedule_paused=true`, so the 9AM scheduler will adopt/reactivate it with the rest of the book instead of leaving it idle overnight. No code commit; DB-only ops fix.
- `[x]` **OPS-04 (P3) — Hermes Telegram alerts 404.** Removed from agent queue 2026-07-06. This is a founder/env credential task, not a code task: `.env.hermes` needs a real bot token + chat_id, then the Hermes container must be force-recreated because restart will not reload `env_file`.
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
- `[x]` **WR-33** CLOSED/DEFERRED 2026-07-06: do not tune winner exits now. The premise (winners cut tight) came from the old 06-20 single-leg-buyer-heavy book. The current winners are spreads held to theta-TP (`spread-tp`, tastytrade-optimal 50%) / EOD square-off, and trailing is already enabled by default (`trailing_sl_enabled=True` suppresses fixed TP). Reopen only if AR-08 attribution proves a real "winner cut early" pattern.

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
- `[x]` **WR-71** DONE 2026-07-04: free NSE bhavcopy data replaced the Upstox expired-option wall. `backend/scripts/bhavcopy_ingest.py`, `core/bhavcopy_store.py`, `core/eod_options_backtest.py`, `scripts/run_oos_validation.py`, and `scripts/run_edge_sweep.py` now provide the real options-chain/OOS validator.
- `[x]` **WR-72** FOLDED into HSI-41..44 2026-07-02 (dedupe): the walk-forward/OOS harness is HSI Stage 4's `core/hermes_validator.py`; historical market OOS is now covered by bhavcopy/IMD validators. One OOS judge family for the platform — don't build a parallel one.
- `[ ]` **WR-73** Enable `CORE_ENGINE_LIVE_ENABLED` on 2–3 proven strategies. **Founder-gated; not agent-pickable.** Current prerequisites: OOS `CANDIDATE_EDGE`, 3-6 weeks clean forward-paper evidence, AR-08 checkpoint green, and founder approval. `CORE_ENGINE_LIVE_ENABLED` remains `false`.
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

## PRIORITY 8A — Architecture Redesign Stage 2 (Single-Writer Ownership)

Founder approved 2026-07-09: `core/portfolio_ledger.py` becomes the owner of
`strategy_positions`; `strategies.today_pnl` becomes a derived compatibility view from
canonical `trade_fills`; legacy fill/monitor/SQLite paths are deprecated before deletion.
Work one rung per commit and obtain a deploy approval after tests.

### ARCH-2A — Ledger-owned position mark writes
- **Status**: `[x]` DONE 2026-07-09 (Codex)
- **Tier**: 3
- **Files**: `backend/core/portfolio_ledger.py`, `backend/position_monitor.py`,
  `backend/position_guardian.py`, focused tests
- **Acceptance**: monitor and guardian no longer write mark fields directly; both call
  one ledger compare-and-swap method that refuses to update a position closed while a
  quote was awaited. No mark/P&L calculation changes.

### ARCH-2B — Ledger-owned position lifecycle transitions
- **Status**: `[ ]`
- **Prerequisite**: ARCH-2A deployed and observed
- **Scope**: route OPEN/EXITING/CLOSED/CANCELLED transitions from server, reconciler and
  spread lifecycle through explicit ledger commands; preserve spread atomicity.

### ARCH-2C — Derived strategy P&L compatibility view
- **Status**: `[ ]`
- **Prerequisite**: ARCH-2B
- **Scope**: make `trade_fills` the only P&L truth; replace runtime `today_pnl` writers
  with one derived projection and parity-check every consumer before removing writes.

### ARCH-2D — Deprecate parallel legacy state paths
- **Status**: `[ ]`
- **Prerequisite**: ARCH-2C
- **Scope**: add deprecation inventory/telemetry for the legacy fill engine,
  `_mongo_position_monitor_loop`, and SQLite `option_state_ledger`; delete nothing until
  a clean observation window and separate founder approval.

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

### Critical path (superseded by HSI + ERL)
The original HSB ratchet path is no longer active. The judge-first law still applies, but the implementation path is now:
HSI observed/attributed lessons + bhavcopy/IMD OOS validators + ERL trial registry → forward-paper → founder-gated live.
Do not revive the old HSB-11..16 `db.historical_chains` ratchet unless the founder explicitly asks for that legacy path.

---

### Phase A — Truth & Grounding (read-only, low risk)

*Completed and verified.*

### Phase B — Memory Ring

*Completed and verified.*

### Phase C — Web Context  ✅ DONE (see Completed Tasks)

### Phase D — Advisor Behavior + Self-Improvement Loop  ✅ DONE (see Completed Tasks)

### Phase E — Strategy AutoResearch Ratchet (superseded)

> **SUPERSEDED / REMOVED FROM ACTIVE QUEUE 2026-07-06:** HSB-11..16 are replaced by the shipped HSI loop (`HSI-41..54`) plus the Edge Lab Research Ledger (`ERL-01..07`). Do not build a parallel `db.historical_chains` ratchet or `backend/core/walkforward.py`. Use the bhavcopy EOD OOS validator, IMD intraday OOS validator, Hermes historical validator, and ERL trial registry instead.

| HSB-11 | SUPERSEDED — historical-chain audit no longer gates OOS; bhavcopy/IMD data stores are the current source. |
| HSB-12 | SUPERSEDED — OOS judges already exist in `core/eod_options_backtest.py`, `core/intraday_options_oos.py`, and `core/hermes_validator.py`. |
| HSB-13 | SUPERSEDED — experiment logging belongs in `ERL-01` Strategy Trial Registry. |
| HSB-14 | SUPERSEDED — paper-forward promotion is documented in EDR/IMD/WR-73 gates. |
| HSB-15 | SUPERSEDED — Hermes governed proposals are handled by `HSI-51` `draft_config_change`. |

### Phase F — Unify

| HSB-16 | SUPERSEDED — unification now flows through HSI advice + ERL trial history, not a separate HSB ratchet. |

### Cleanup (opportunistic)

| HSB-17 | REMOVED FROM ACTIVE QUEUE — dead `draft_pr_summary` cleanup is low-value/non-trading polish; reopen only if the approval UI surfaces broken PR-summary cards. |

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

*Last updated: 2026-07-06 (task queue cleaned: OPS-02/OPS-04/WR-33/WR-71/HSB ratchet stale items closed or superseded; HSI-51..54 and IMD-01..10 are shipped.)*
*Open after cleanup: AR-08 measurement checkpoint; AR-07 data-gated after AR-08; WR-45/WR-51/WR-54 after AR-08; WR-73 founder-gated; ERL-01..07 research-ledger build. In progress: 0.*
*Founder decisions 2026-06-22 (Hermes Second-Brain): (1) RAG + prompt engineering, no model fine-tuning, stay on Gemini 2.5-flash; (2) web via Gemini Google-Search grounding; (3) AI score = quant-grounded, LLM-narrated; (4) strategy AutoResearch ratchet folded into this campaign, judge-first; (5) lower priority than Win-Rate.*
*Recommended next build order: AR-08 checkpoint → AR-07 only if evidence warrants → ERL-01 → ERL-02/03/05 → WR-45/WR-51/WR-54 after AR-08 → WR-73 only with founder approval and proven OOS + forward-paper evidence.*
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
- **Data-maturity reality**: the full loop is now built, but the brain only gets *smart* as clean attribution + historical/OOS validation mature. 1 clean day = nothing; ~30 clean days = real signal.
- **Reuses existing assets**: `hermes_memory` (embeddings/RAG) · `READ_ONLY_AGENT_TOOLS` (routes/ai.py) · `pending_actions`/Approvals UI · EOD/IMD OOS validators · `core/strategy_scorecard` · `_position_exposure_bias` (strategy_runner, added 2026-06-30) · EOD `_compile_eod_memory` (position_monitor).
- **Relationship to old HSB campaign**: HSB-01..10 are done/deployed and HSB-11..17 are superseded by HSI + ERL. Do not build a parallel HSB ratchet.

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
- `[x]` **HSI-51** DONE 2026-07-06 (Claude/Opus). New `draft_config_change` pending action (in `allowed_actions` + prompt action #7 + approve handler in `routes/ai.py`). Params `{strategy_id, field, proposed, lesson_id, reason}`. Renders in the existing Approvals UI. **OOS-GATED**: rejects unless the cited lesson has `oos_passed=true`. Respects the template-resync mechanic — only `required_capital`/`visual_config.options.structure` are applied DB-only; any other field appends an **edit-in-template** task to TASKS.md instead of a fake DB write. Stores prior value + pre-change expectancy for reversibility.
- `[x]` **HSI-52** DONE 2026-07-06. `core/hermes_advisor.compile_hermes_advice` builds a cached `db.hermes_advice` doc (regime×structure confidence multipliers) from active OOS-passed lessons; `strategy_runner` consults it via `_hermes_advice_multiplier` and attaches `hermes_advice_multiplier` to every signal for diagnostics. **Behind `HERMES_ADVICE_ENABLED` (default false = observe-only)** — the multiplier only nudges `confidence` when the founder flips it on; code still decides.
- `[x]` **HSI-53** DONE 2026-07-06. `compile_trade_attribution` tags each row with `hermes_lesson_id` from the active applied-change map (`active_change_lesson_by_strategy`), so Stage 3 can later score whether the CHANGE improved real P&L.
- `[x]` **HSI-54** DONE 2026-07-06. Safety rails in `core/hermes_advisor`: every applied change is reversible (`prior_value` stored in `db.hermes_applied_changes`), rate-limited (`within_rate_limit`, `HERMES_MAX_CHANGES_PER_WEEK=2`), and **auto-reverted at EOD** (`check_and_autorevert`, wired into `position_monitor._run_eod_aggregation`) if post-change expectancy regresses (negative AND worse than pre, ≥`HERMES_AUTOREVERT_MIN_TRADES`), logged WARNING. 8 unit tests in `test_hermes_advisor.py`.
- **Acceptance**: a validated lesson surfaces as an Approval card with OOS evidence; approving applies + logs + tags; later attribution shows whether it helped; revert works.
- **Files**: `routes/ai.py`, frontend Approvals (renders `pending_actions` already), `strategy_runner.py` (52 read), `core/hermes_lessons.py` (53). **Deps**: Stages 1–4. **Founder-gated.**

**Suggested order**: HSI-11→15 (Stage 1, do first — unblocks everything) → HSI-21→23 → HSI-31→34 → let ~3–4 weeks of clean attribution accumulate → HSI-41→44 → HSI-51→54.

---
