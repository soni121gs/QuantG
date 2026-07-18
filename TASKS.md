# TASKS.md — QuantG Work Queue · Edge Rebuild Program (REBUILT 2026-07-19)

**Read AGENTS.md first (tiers + workflow), CLAUDE.md §20 for the program spec.**
**This file was rewritten 2026-07-19.** The previous 295KB queue (RES/EM/RAE/IA/IMD/ERL/HIRB campaigns, Priority 0–12) lives in git history (`git log -- TASKS.md`); completed programs are one-liners in ARCHIVE below. Program source: Edge Reports v1–v3 (memory `project_edge_report_v3_full_07_19.md`).

Legend: `[ ]` open · `[~]` in progress · `[x]` done · ⛔ blocked on prerequisite · 🔑 founder decision · (T1/T2/T3) = AGENTS.md model tier

---

## GOVERNING LAWS (every task inherits these)

1. **OOS-first ladder** — hypothesis → OOS (regime-/event-conditional) → forward-paper → founder-gated live. Nothing scales on a backtest. Paper P&L proves nothing alone.
2. **Cost-floor law (Carver)** — reject at DESIGN time any strategy whose expected edge < 3× modeled round-trip friction (≈ never spend >⅓ of expected Sharpe on costs; in practice: no options structure under ~₹250/lot expected edge).
3. **Breadth law (Grinold-Kahn, IR = IC·√BR)** — prefer many independent bets (events × names × regimes) over more parameterizations of one bet.
4. **Overfitting law (Bailey/López de Prado)** — every judge verdict must carry trials-count; Deflated Sharpe once P2-2 lands.
5. **Verify-live-facts law** — exchange microfacts (lots, expiries, margins, SEBI rules) come from the Upstox instrument master / live sources, never from model memory. Verified 2026-07: NIFTY lot **65**, BANKNIFTY **30**, SENSEX **20** (Jan-2026 revision, NSE circ FAOP70616); NIFTY expiry **Tuesday**, SENSEX **Thursday**, BANKNIFTY monthly-only.
6. **LLM narrates, code computes.** Hermes never trades, never edits code; hypothesis cards must cite a corpus source or a deterministic probe.
7. `CORE_ENGINE_LIVE_ENABLED=false` throughout. Live = founder ladder (RAE-7 gate), never a task.

---

## 🔑 FOUNDER DECISIONS NEEDED (blocking gates)

- [ ] **D-1** Approve the Phase-0 **purge list** (§P0-5): ~32 DB rows + dead code templates — incl. all 10 equity intraday strategies and the revived NIFTY Theta/Range spreads. Keep-list (paused): QG-O1, QG-O4, QG-O11, 3 RAE range sellers, 3 RAE trend delta-1.
- [ ] **D-2** **Upstox Plus** (paid tier): unlocks the official Expired Instruments APIs → deep options-1-min backfill (P1-5). Yes/No.
- [ ] **D-3** **Paid research-lane model** for Hermes weekly hypothesis synthesis (Gemini Pro-class or Claude); free Gemini 3 Flash covers everything else (P4-1). Yes/No.

---

## PHASE 0 — CLEANUP & GUARDRAILS (~1 wk) — start here
Goal: a small honest book, config-as-data, design-time tripwires. Absorbs QGX INFRA Rung 1.

- [x] **P0-1** (T2) History snapshot before ANY delete: JSON export of `strategies` + `strategy_positions` + `trade_fills` (+ signals counts) to `data/archive/book_snapshot_2026-07/`. Done 2026-07-19 by Codex before purge: VPS snapshot contains 41 strategies, 505 positions, 741 fills, 2472 signals, plus `signal_counts_by_strategy.json` and `manifest.json`.
- [x] **P0-2** (T3, =INFRA-13) Registry migration: moved the ERP keep/purge manifest into `core/strategy_registry.py` and added `scripts/phase0_registry_cutover.py` to upsert every keeper into `db.strategy_registry` from the pre-purge DB row before deletion. Default code-backed seed templates now retain only the QG keepers; RAE DB-only keepers stay registry rows.
- [x] **P0-3** (T3, =INFRA-14) Kill the frozensets: startup wake behavior now follows the registry keep/purge manifest; `PAPER_FORWARD_ACTIVE_STRATEGY_NAMES` is empty, keepers are forced paused/paper with `registry.active=false`, and purge names are not auto-woken.
- [x] **P0-4** (T3, =INFRA-15) Remove startup template re-sync/migrations: disabled `_migrate_strategy_code_versions`, debit/credit structure rewrites, alpha-repair followup rewrites, and v12 template field sync. DB strategy geometry now persists across restart.
- [x] **P0-5** (T2) Execute the purge (founder approved in chat 2026-07-19): `phase0_registry_cutover.py --apply` deletes approved purge strategy config rows after registry upsert/snapshot; catalog tests updated for the Phase 0 keeper catalog.
- [~] **P0-6** (T2, =INFRA-17) Cutover verification: code/tests pass and VPS deploy verifies restart with no template resurrection; one full clean paper market session remains the runtime confirmation gate.
- [x] **P0-7** (T2) Cost-floor tripwire: added Diagnostician probe `static.cost_floor` for expected edge vs modeled friction. ERL promotion-gate reuse is the next refinement in the unified judge facade, but the deterministic tripwire now files findings.
- [x] **P0-8** (T2) Contract-spec drift tripwire: added `market_domains.contract_spec_for_underlying()` and Diagnostician probe `infra.contract_spec_drift` for lot/expiry mismatches against central domain specs.
- [x] **P0-9** (T1) Doc hygiene: CLAUDE.md §20 and AGENTS.md §2 reflect the ERP state; Phase 0 execution notes are recorded here. A deeper stale file-map prune can ride normal docs cleanup.

## PHASE 1 — DATA FOUNDATION (~1–2 wks; parallelizable with Phase 0)
All sources legal per §14.1 (exchange/broker/public).

- [~] **P1-1** (T2) Stock derivatives ingest: code landed 2026-07-19. `bhavcopy_ingest.py` now accepts `STO`/`STF`, includes stock F&O underlyings, and exposes `--instr-types`; `BhavcopyStore` serves `STF` futures via `underlying_daily()` and `STO` option chains. VPS smoke verified real RELIANCE data for 2025-01-09 (216 rows; 3 expiries). Remaining: run the full 2024–2026 re-ingest on VPS.
- [~] **P1-2** (T2) Earnings calendar: code landed 2026-07-19. Added `core/earnings_calendar.py` and `scripts/earnings_calendar_ingest.py` for CSV/public-source backfills and `events_for(symbol, date_range)`. Remaining: collect/backfill top-30 F&O result dates for 2024–2026 and schedule weekly forward capture.
- [~] **P1-3** (T2) Pre-2024 history: old NSE F&O archive URL adapter hook landed 2026-07-19 via `OLD_NSE_FO_URL` / pre-2024 source routing. Remaining: run the 2019–2023 backfill and confirm the EOD judge runs on 2020 dates.
- [~] **P1-4** (T2) Participant-wise **F&O** OI: code landed 2026-07-19. Extended `india_flows.py` with participant-OI parser/store and added `scripts/participant_oi_ingest.py`. Remaining: download/backfill NSE participant-OI files and validate before wiring into P3-5.
- [ ] **P1-5** (T2) ⛔D-2 Options-minute backfill via Upstox **Expired Instruments APIs** (official). Acceptance: `options_1m` coverage grows beyond 204 days + live capture.
- [x] **P1-6** (T1) Freshness watchdogs: `data.store_coverage` now includes earnings dates and participant-OI stores alongside bhavcopy/index/options stores.

## PHASE 2 — ANALYTICS & JUDGE REFORM (~2 wks; needs parts of Phase 1)

- [ ] **P2-1** (T3) `core/iv_surface.py`: per-strike/per-expiry IV from stored chains (BS inversion off futures settle; spline/SVI-lite smoothing), richness **z-scores vs trailing history**, term-structure + skew summary. Feeds `market_context`/`entry_gate`/`dynamic_contract_selector` + Edge Lab. Acceptance: surface + richness for NIFTY/SENSEX any stored date (stocks once P1-1).
- [ ] **P2-2** (T2) ERL upgrade: **Deflated Sharpe + explicit trials-count** per family on every snapshot/verdict (extends the existing multiple-testing penalty).
- [ ] **P2-3** (T3) EOD judge extensions: (a) stock underlyings; (b) **event-conditional mode** (grade only event-window days — the S1 judge), reusing the RAE-2 regime-conditional pattern.
- [ ] **P2-4** (T3, =INFRA-62 start) Judge façade: one `grade(strategy_cfg, mode=eod|intraday|regime|event)` over the 4 judges; duplicate harness code deleted.
- [ ] **P2-5** (T3, deferred) Two-expiry (calendar) structures in the backtester — only if a calendar sleeve is ever promoted.

## PHASE 3 — STRATEGY SLEEVES (~3–4 wks staged; each rides the full ladder)

- [ ] **P3-1** (T3) **S1 Earnings IV-crush premium (FLAGSHIP):** selector (defined-risk condor/strangle+wings; T−1 entry, T+1 exit; top-30 liquid names; never into expiry week — physical settlement; skip event≡expiry) → event-conditional OOS across ~700 events (needs P1-1, P1-2, P2-3) → paper via registry. Acceptance: OOS verdict n≥300 events, DSR-positive, cost-floor pass, before any paper trade.
- [ ] **P3-2** (T3) **S3 Daily delta-1 momentum + overnight:** re-horizon the RAE trend specialist to daily time-series momentum with vol-target sizing (EdgeMath); test the overnight-drift variant separately. Judge on 2019–26 once P1-3 lands. Acceptance: OOS across ≥2 regimes incl. a down year.
- [ ] **P3-3** (T2) **S4 Slow premium core restore:** QG-O1's validated held+gated geometry + the SENSEX monthly strangle candidate, **gated by P2-1 richness** (sell only when rich — at VIX~13 it should mostly stand down). Acceptance: re-passes OOS on extended data before wake.
- [ ] **P3-4** (T2) **S1b PEAD validation** — hypothesis card #1 through the new pipeline (stock futures/verticals in surprise direction; needs P1-1+P1-2). Mixed India evidence: validate honestly, kill fast if flat.
- [ ] **P3-5** (T2) **S5 participant-OI overlay:** multi-regime validation as gate/size feature for S3/S4; wire only on a pass (the IA-4 lesson).
- [ ] **P3-6** (T2) Paused-seller re-judge: QG-O11 + 3 RAE sellers on the re-derived 0.50/0.90 geometry with slippage stress (2/5/8%); founder wakes on pass; **DELETE QG-O11 on another slippage fail.** QG-O4 regime-conditional re-judge (SENSEX-artifact question).
- [ ] **P3-7** (T3) Book assembly: EdgeMath + RAE router size the multi-sleeve book; portfolio heat/correlation caps verified across mixed horizons (intraday premium + multi-day delta-1 + event holds coexisting).

## PHASE 4 — HERMES v2: RESEARCH ANALYST (~2 wks staged)

- [ ] **P4-1** (T1) `GEMINI_MODEL` → `gemini-3-flash-preview` (free tier, 1,500 req/day); verify planner/tool-calling + embeddings parity.
- [ ] **P4-2** (T2) **H1 corpus:** `wiki/Research/` — ~25 curated notes (textbook summaries: Grinold-Kahn, Sinclair ×2, Carver, Natenberg, Aronson, López de Prado; SEBI FY25 study; India papers: VRP anatomy, day/night option returns, PEAD ×2, Momentum-30; Edge Report v3). Reindex via `research_rag.reindex_all` (IA-7 pipeline exists). Acceptance: RAG recall cites them.
- [ ] **P4-3** (T2) **H3 opportunity probes** (deterministic): VRP-by-strike monitor, earnings IV-runup stats (post P1-2), overnight/intraday split, term-structure richness (post P2-1), participant-OI extremes (post P1-4) → `db.research_signals`.
- [ ] **P4-4** (T3) **H2 hypothesis cards:** weekly job — falsifiable cards {hypothesis, who-pays rationale, universe, horizon, judge, data needed, kill criteria} → ERL PROPOSED. Cards without a corpus/probe citation are rejected at write time.
- [ ] **P4-5** (T2) **H5 calibration:** card outcomes (validated/refuted/abandoned) via the lessons confirm/contradict machinery; hit-rate on Analytics + `get_hermes_brain_health`.
- [ ] **P4-6** (T1) ⛔D-3 Paid research-lane model routing (weekly synthesis call only).

## INFRA TRACK — QGX continuation (runs alongside; money-correctness order; full text in git history)
Rung 1 absorbed into Phase 0 (P0-2..P0-6). Remaining rungs unchanged:

- [ ] **INFRA-21..24** (T3) Single-writer ownership: `strategy_positions` lifecycle via `core/portfolio_ledger` (ARCH-2B); `strategies.today_pnl` derived from `trade_fills`; remove reconciler/admin direct writes; retire redundant hand-rolled locks.
- [ ] **INFRA-31..34** (T3) Carve server.py into cells: fill/exit engine first (`cells/execution/`), endpoints → routers in batches, server.py → wiring <~2k lines.
- [ ] **INFRA-41..45** (T3) Redis Streams event bus + correlation/causation ids + event store (convert the fill loop first).
- [ ] **INFRA-51..54** (T2/T3) Collection registry + ownership CI, index/TTL policy, per-cell contracts, slice tests with replay fixtures.
- [ ] **INFRA-61..65** (T3) ONE research pipeline (P2-4 is the start): merge judges, merge sizing/routing, **delete observe-only debt** (`core/dealer_positioning.py`, `*_SHADOW` gates, `core_legacy.py`, `_mongo_position_monitor_loop`, SQLite `option_state_ledger`), single `promote_strategy()` gate.
- [ ] **INFRA-71..76** (T3, founder-gated where rebuilds) DuckDB/Parquet judge store · CRA→Vite · uv+ruff · pydantic at boundaries · OTel tracing on correlation ids · live hardening LAST.

## CARRY-OVER (still-true open items from the old queue)

- [ ] (T2) `test_trade_frequency.py::…boosted_cap` red test — classifier vs expectation (pre-existing).
- [ ] (T2) RES-2 remainder: order-flow feed "full" mode wiring.
- [~] RAE-7 live-pilot gate — built, stays founder-gated; readiness endpoint `GET /api/ops/rae-live-readiness`.
- [ ] Open Hermes finding: `options_1m` coverage only advances via live capture while the book is paused (P1-5 is the real fix).
- Daily ops (no checkbox): Upstox token reconnect each morning; IMD capture health checklist (§14.5); bhavcopy/index_1m cron monitors.

## ARCHIVE — completed/refuted programs (details: git history, CLAUDE.md §13–§19, memory index)

- **RES-1..8** ✅ 2026-07-08 — seller-scalper machinery + OOS gate; verdict: buyers dead (5th time); gated put spread = first-ever OOS pass.
- **EM-1..9** ✅ 2026-07-09 — EdgeMath continuous sizing, paper-forward; EM-7 promotion block founder-waived.
- **RAE-0..6** ✅ 2026-07-10 — taxonomy/classifier/reformed-judge/specialists/router/exits/watch; router observe-only pending founder flip.
- **IA-1..8** 2026-07-11 — verdicts: GEX/OI **DEAD** on NIFTY; IV-skew marginal; cash FII/DII **DEAD** (9-yr test); RAG built (IA-7); GBM/miner correctly deferred.
- **IMD-01..10** ✅ (§14 1-min judge) · **ERL** ✅ (§17 research ledger) · **HSI/HIRB stages 1–6** ✅ (§19 attribution→lessons→advisor→diagnostician).
- **QGX INFRA rung 0 + 11/12/16(partial)** ✅ 2026-07-14 — audits + registry schema/validation; rung-1 remainder → Phase 0.
- **Priority 0–12 campaigns (2026-06)** ✅/superseded — equity phase, win-rate, profitability, route extraction, calendar/reports, Hermes integration, UI polish.
- **Census 2026-07-19 (the purge evidence):** lifetime book −₹68,011 over 494 closed trades; zero strategies with n≥30 AND positive P&L. Full table: Edge Report v3.
