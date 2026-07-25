# TASKS.md — QuantG Work Queue · Edge Rebuild Program (REBUILT 2026-07-19)

**Read AGENTS.md first (tiers + workflow), CLAUDE.md §20 for the program spec.**
**Active work: PHASE 5 (below) — everything above it in ERP Phases 0–4 is complete.**
**This file was rewritten 2026-07-19; Phase 5 appended 2026-07-23.** The previous 295KB queue (RES/EM/RAE/IA/IMD/ERL/HIRB campaigns, Priority 0–12) lives in git history (`git log -- TASKS.md`); completed programs are one-liners in ARCHIVE below. Program source: Edge Reports v1–v3 (memory `project_edge_report_v3_full_07_19.md`).

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

- [x] **D-1** Approve the Phase-0 **purge list** (§P0-5): founder approved in chat 2026-07-19 and P0-5 executed after snapshot/registry upsert.
- [x] **D-2** **Upstox Plus** (paid tier): founder confirmed active 2026-07-19. This unlocks the official Expired Instruments APIs → deep options-1-min backfill (P1-5).
- [ ] **D-3** **Paid research-lane model** for Hermes weekly hypothesis synthesis (Gemini Pro-class or Claude); free Gemini 3 Flash covers everything else (P4-1). Yes/No.

---

## PHASE 5 — TRUTHFUL INSTRUMENTS & BREADTH (added 2026-07-23) ⇦ **START HERE**

**Source:** full-system audit 2026-07-22/23 (loss post-mortem → Hermes findings → self-audit of that
work → statistics review → wiki/RAG audit). Memory: `project_regime_confidence_and_reachability_07_22.md`.

**The three findings that justify this phase:**

1. **The judge has never computed a variance.** `eod_options_backtest._bucket_metrics` (:508) returns
   `n / pnl / expectancy / win_rate` and no dispersion — the `pnls` list is right there and never gets a
   second moment. `walk_forward` (:543) then decides on pure thresholds. **Every verdict QuantG has ever
   issued** (0/11 strategies, 0/72 configs, QG-O11 `CANDIDATE_EDGE`, QG-O1's §15.5 pass) was made with no
   notion of variance: `+₹5/trade @ σ=3000` and `+₹500 @ σ=200` are indistinguishable to this code. The one
   place claiming rigour, `edge_research_ledger.deflated_sharpe` (:79), is `(expectancy / 500.0)` — an
   arbitrary constant, never touches volatility, so "Deflated **Sharpe**" is a misnomer that cannot detect
   a high-variance strategy.
2. **Hermes cannot tell its own lessons from noise.** 19 hypotheses tested, promotion at
   `correct≥3 AND hit_rate≥0.6` with **no multiple-testing correction** and **no base-rate null** (the book
   runs 202/558 = **36.2%** win rate). Under a coin-flip null ≈**6** false promotions are expected;
   **5 lessons are active.** `confidence = hit_rate × sample_factor` is a shrunk effect size wearing a
   probability's name. These feed `get_hermes_brain_health` and get narrated as learned wisdom.
3. **The flagship sleeve cannot pass its own gate, and three ✅ tasks hide it.** P1-1 delivered **10**
   stock names (cron omits `--all-underlyings`; the downloaded file already contains ~180). P1-2 delivered
   **30** earnings names / 309 events. P3-1's paper gate needs **n≥300 events**. The intersection is
   10 names × ~4 events/yr × 2.5 yr ≈ **100 events** — the earnings IV-crush flagship, QuantG's only
   genuine breadth play, is structurally starved by two config constants. Disk is not the blocker
   (80 GB free; `bhavcopy_fo` is 571 MB today, ~6–9 GB at full universe).

**Money framing (honest):** tracks R/J/K do not make money — they stop the book deploying noise and stop
Hermes asserting falsehoods. Only track **M** has a mechanism for new P&L, and `M1→M2→M3` is the only path
in this file to an edge that isn't a re-parameterisation of the one bet the census already killed
(−₹68,011 / 494 trades). `IR = IC·√BR`; today BR ≈ 1.

**Order:** `M1` (one flag, unblocks everything, backfill runs overnight) → `R1,R2` (live defects) →
`J1` (highest value-per-hour in the codebase) → rest in parallel.

### Track X — emergent live-session fixes (2026-07-23, not originally listed) ✅ SHIPPED
- [x] **P5-X1** Credit-spread strategies were opening NAKED single-leg option buys when the spread build
  failed (a geometry veto left `option_contract` single-leg → fell through to the buyer path). The
  reachability veto turned this rare latent bug into a constant one — 7 naked buys by credit sellers in one
  morning. Fix: `signal_manager` returns SKIPPED (`SPREAD_BUILD_FAILED`) when a spread-declared strategy has
  no spread payload — stand down, never degrade to a single leg. **Deployed + verified LIVE in-market**
  (0 naked buys post-deploy; strategy reason shows "standing down, no naked single-leg fallback").
- [x] **P5-X2** Hermes blind spot: it filed 0 execution findings while 7 naked buys sat in the book. New
  CRITICAL probe `exec.structure_mismatch` flags any spread-declared strategy holding a single-leg position.
  Deployed + verified: catches all 7 (QG-O11 ×3, IDX NIFTY ×3, IDX SENSEX ×1). 3 probe tests.
- [x] **P5-X3** (07-22) Static probes hardened: `static.cost_floor` measured GROSS credit not bankable
  (`tp_frac×credit×lot`) — was ~1/tp_frac too permissive, hiding QG-O1 (1.85×) and QG-O11 (1.61×) sub-floor
  violations; `persistent_live_loss` now splits at `geometry_changed_at` but never resolves on it.

### Track R — regression repair (my own 2026-07-22 changes; ~2 h total)
- [x] **P5-R1** (T3) ✅ DONE 2026-07-23 — ⚠️ **LIVE DEFECT — do first in this track.** `core/regime_router.route()`: my
  coarse-regime fallback rewrites `regime` at the TOP of the function, above every protective guard, which
  breaks three things at once:
  **(F)** the `long_vol` guard at :110 — explicitly documented as the 2026-07-21 *"IDX Long-Gamma
  inversion"* fix — never sees `HIGH_VOL_CHOP`, so the long-gamma sleeve **stands down on the one regime it
  owns**. Verified: `route(HIGH_VOL_CHOP, 0.40, specialist=long_vol)` = TRADES 1.0; with
  `fallback_regime=RANGE` = STAND_DOWN. **This is live now** (bites at low confidence, i.e. early session).
  **(A)** with `RAE_CHOP_STANDDOWN=true` a low-confidence chop day is laundered into RANGE and sellers
  trade it — a safety veto I silently defeated. **(B)** a low-confidence `TREND_DOWN` + coarse `RANGE`
  now TRADES: I documented a safety net but implemented a label swap that can *increase* risk.
  **Fix:** stop rewriting the label. Evaluate `route()` on the fine label and on the coarse label, return
  the **more conservative** decision (stand_down wins; else `min(size_mult)`), preserving both reason
  strings. That single restructure fixes A, B and F together.
  **Acceptance:** new test — long_vol still trades CHOP with a RANGE fallback; chop veto holds when
  `RAE_CHOP_STANDDOWN=true`; the 2026-07-22 case (fine RANGE/0.40, coarse TREND_DOWN) still stands the
  seller down; existing 14 router tests green.
- [x] **P5-R2** (T2) ✅ DONE 2026-07-23 (epoch 00:00Z→18:02Z; verified trades_since_change==0). `geometry_changed_at` epoch was **18.5 h too early**:
  `scripts/fix_qgo4_costfloor_and_epoch_07_22.py` stamps `2026-07-22T00:00:00Z` but the fix deployed at
  **18:34Z**, so all six trades opened 04:17–09:20Z **under the old code** count as post-re-cut (hence the
  finding reading *"1 of them since the re-cut"*). This is precisely the sample-laundering the epoch split
  was built to prevent. Fix the constant (and prefer stamping apply-time), re-apply, re-run diagnostics.
  **Acceptance:** `trades_since_change == 0` for QG-O1/QG-O4/QG-O11 until they trade again.
- [~] **P5-R3** (T2) mostly settled by R1 (FINE_MIN_CONF no longer gates the defer; it only scales seller size) — `RAE_ROUTER_FINE_MIN_CONF` (0.50) **exceeds** `RANGE_BASE_CONF` (0.40), so the RANGE
  branch can never clear the maturity test — a 200-bar established range defers exactly like a 10-bar one
  (verified). The gate I documented does not exist; only the size scaling works. Decide deliberately:
  lower the threshold (~0.30) or raise `RANGE_BASE_CONF`. **Depends on R1** — once the fallback is
  conservative-only, an always-on fallback is safe, so this is correctness, not urgency.
- [ ] **P5-R4** (T1) `core/hermes_diagnostics/probes_static.py:58` imports `dte_from_expiry` inside a
  function while `core.spread_builder` is already imported at :13 — no circular-import justification,
  violates CLAUDE.md §9. Move to module scope.
- [x] **P5-R5** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — **`pytest tests/` does not hang because it is slow — it blocks on network I/O.**
  Diagnosed 2026-07-23: four attempts sat 60–90 min each at **~26 s CPU** (i.e. idle, waiting). At least
  `tests/backend_test.py` is a LIVE-SERVER integration suite (`requests` against
  `REACT_APP_BACKEND_URL`, 44 network calls); `test_iteration2-5`, `test_hermes_sidecar`,
  `test_agent_tools`, `test_environment_preflight`, `test_p4_gemini_model` also reach out. Excluding those
  still hung, so at least one more culprit remains unidentified.
  **Do:** (a) mark the network suites with a `@pytest.mark.integration` marker and default
  `addopts = -m "not integration"` in `pytest.ini`; (b) finish the per-file hang sweep to name the
  remaining offender; (c) get the offline suite green (known pre-existing red:
  `test_trade_frequency.py::…boosted_cap`).
  **Run it ON THE VPS in background, not in a session** (see P5-R7).
- [x] **P5-R7** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — **Move all long jobs to the VPS.** The box runs 24/7; nothing slow should ever block
  a local session. Add a `scripts/ci_run.sh` that runs the offline suite + the diagnostics run in the
  backend container, logs to `/var/log/quantg_ci.log`, and a nightly cron for it. Same pattern for
  backfills and OOS sweeps (`nohup docker exec … &`, read the log next turn).
- [ ] **P5-R6** (T1) Verify the two `options_1m` repairs actually fire — neither has run yet:
  the 15:35 IST capture flush (window widened + failures now WARN with traceback; previously swallowed at
  debug, which is how the store went 8 days stale) and the new ingest cron (11:15 UTC weekdays). Check
  `docker logs | grep "minute capture flush"` and `/var/log/options_1m.log` after the first firing.
  Minor edge case to note while there: `dte_from_expiry` returns `0.0` for a past expiry, which *passes*
  reachability rather than flagging it.

### Track J — make the instruments trustworthy (~2 days)
- [x] **P5-J1** (T2) ✅ DONE 2026-07-23 — `_bucket_metrics` now reports `std`, per-trade `sharpe`, and
  `t_stat` (expectancy / standard_error); `walk_forward` requires the overall t-stat to clear
  `JUDGE_T_STAT_MIN` (default 3.0) for `CANDIDATE_EDGE`, else FRAGILE. Verified: same-sign expectancy,
  t=0.36 (noisy)→FRAGILE vs t=99 (tight)→CANDIDATE. Research-only (never in the live loop). 9 guardrail
  + 24 affected tests green. Commit pushed; real-book re-grade run in progress (feeds P5-M3).
- [x] **P5-J2** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — Replace the DSR proxy with a real Deflated Sharpe from stored per-trade returns.
  `edge_research_ledger.deflated_sharpe` (:66) already declares itself the replacement point
  (*"when trade-return vectors are stored, this function is the single replacement point"*) and
  `run()` already returns `trades`. Kill the `/500.0` constant.
- [x] **P5-J3** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — `core/hermes_lessons.py`: add a **binomial test against the 36.2% book base rate** and
  a **Šidák/BH correction across the ~19 tested hypotheses** before promotion; make `confidence` calibrated
  (or rename it `effect_size`). Re-score the 5 active lessons under the corrected bar and record how many
  survive. **Acceptance:** a lesson indistinguishable from the base rate cannot reach `active`.
- [x] **P5-J4** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — `phase4_research.calibration_summary` (:336) is a hit-rate tally, not calibration —
  no confidence bins, no Brier score, no stated prior on the cards. Either make it real (cards carry a
  numeric prior; score with Brier + reliability bins) or rename it so `get_hermes_brain_health` stops
  reporting it as calibration.
- [ ] **P5-J5** (T3) Newey-West / HAC standard errors on the significance tests (returns and vol cluster,
  so raw errors flatter the signal). Do after J1 — marginal until a t-stat exists at all.

### Track K — truthful knowledge for Hermes & the wiki (~2 days)
- [x] **P5-K1** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — **Index CLAUDE.md into the RAG.** `research_rag.reindex_all` indexes
  `wiki/Research/*.md`, `db.wiki_docs`, lessons and OOS verdicts — **CLAUDE.md is not indexed**, so the
  1,000+ line canonical manual holding every law, pitfall and root cause is invisible to Hermes. Chunk by
  `##` section with `source_ref=CLAUDE.md §N`. Cheapest large knowledge gain available.
- [x] **P5-K2** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — **Kill the false-capability notes — the wiki currently teaches Hermes things that are
  not true.** `wiki/Research/Lopez de Prado Deflated Sharpe.md` asserts *"ERL carries … a DSR proxy"*
  (it is a scaled expectancy); `Grinold-Kahn Fundamental Law.md` asserts breadth practice while IC is
  computed nowhere (`grep information_coefficient|spearman` = **0 hits**). Add required frontmatter to
  every note — `claim_type: measured|literature|aspiration`, `verified: <date>`, `reproduction: <cmd>`
  (mandatory when `measured`) — and downgrade the false ones to `aspiration` until J1/J2 land, then flip.
  **0 of 34 notes currently carry any provenance.**
- [x] **P5-K3** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — **Write the dead-ends register** (`wiki/Research/Dead Ends.md`) — QuantG's most
  valuable and completely unrecorded knowledge; without it every new agent session re-proposes what you
  already killed. Seed from existing evidence: option **buyers dead across 5 independent studies**;
  credit geometry risking 100% of credit for 50% (needs ~67% WR, runs 33–48%); iron condor FRAGILE
  (0/18 configs → do not build 4-leg infra); GEX/OI dead on NIFTY; FII/DII cash dead (9-yr test);
  BANKNIFTY impossible as an intraday theta seller at any width (monthly expiry, §21.2); 0-DTE fails the
  cost floor outright.
- [x] **P5-K4** (T2) ✅ DONE 2026-07-25 (`b337ba3`) — Auto-generate `wiki/Measured/` **from** code/DB so it cannot drift: current book
  geometry, lot sizes + expiry cycles, book base rate, friction constant, cost-floor/reachability
  measurements, store coverage. Generated notes are `claim_type: measured` by construction.
- [x] **P5-K5** (T1) ✅ DONE 2026-07-25 (`b337ba3`) — `wiki/Trading Rules/`, `wiki/Meeting transcripts/`, `wiki/YouTube transcripts/` are
  **empty** and `wiki/Decisions/` has **1** file. Populate Trading Rules from CLAUDE.md §21 (both geometry
  laws + their measurements), §13.5, §14.4, §20.
- [ ] **P5-K6** (T3) **Hermes can observe but cannot research.** All 37 tools read internal state
  (`get_external_context` is Google news; `get_historical_context` is past sessions) — there is **no tool
  to ask a new question of the bhavcopy / index_1m / options_1m stores**, so every hypothesis card needs a
  human or an agent to run it. That human bottleneck is the research engine's rate limit. Add ONE bounded,
  read-only, deterministic query tool (fixed verbs, capped rows, no free-form code) and route it through
  `agent_tool_audit`. **LLM narrates, code computes** stays intact.

### Track M — breadth & money (the only track that can generate P&L)
- [x] **P5-M1** (T1) ✅ DONE 2026-07-23 — the `nse` F&O source no longer whitelists 10 stock names (empty
  set = keep every F&O underlying; instr-type filter still restricts to IDO/IDF/STO/STF). Full 2019→2026
  backfill (`--overwrite`) completed on the VPS. **Acceptance cleared: 210 distinct STO underlyings** on a
  recent day (was 10); store 571 MB → 1.3 GB.
- [x] **P5-M2** (T2) ✅ DONE 2026-07-23 — `fno_universe_from_store()` derives the earnings universe from
  stocks that actually have options in the bhavcopy store (not a hand-maintained top-30), wired into the CLI
  default + Saturday scheduler. Backfill complete: **211 symbols / 3,062 events** (was 30 / 309). The
  earnings sleeve's n≥300 gate is now reachable.
- [ ] **P5-M3** (T3) Re-run the P3-1 earnings IV-crush validator on real breadth
  (`scripts/run_earnings_iv_crush_validation.py`) under the J1 t-stat and J2 DSR. This is the flagship and
  the only sleeve with a plausible new-edge mechanism. Gate unchanged: n≥300 events, DSR pass, ≥3×
  cost floor, before any registry paper wake.
- [ ] **P5-M4** (T3) **Alpha-vs-beta separation — the unanswered question about the whole book.** Build a
  daily short-vol benchmark from bhavcopy (sell ATM straddle/strangle, held per the book's horizon) and
  regress each strategy's daily returns on it (plus NIFTY return). Report α, β, t(α). **If β≈1 and α≈0 the
  seller book is a risk premium you are paying costs to replicate — that finding redirects the entire
  program.** Ties directly to the §20 census ("one bet expressed 11 ways") and to `IR = IC·√BR`.
- [ ] **P5-M5** (T3) **Validate the three scoring systems you already built and never checked** — compute
  IC (rank correlation vs realized forward P&L) for `contract_edge_score`
  (`core/dynamic_contract_selector.py`), RAE regime confidence, and EdgeMath conviction. An IC ≈ 0 means
  that machinery is decoration. Also closes the §20 breadth law's dependency on an IC that is computed
  nowhere.
- [ ] **P5-M6** (T2) Cost-floor siblings left unfixed on 2026-07-22 (all contained — the build-time floor
  vetoes them, so this is hygiene not urgency): `idx-nifty-callspread-0001` at **787/lot** (surfaced only
  after the probe was corrected to measure bankable rather than gross), and RAE SENSEX + IDX SENSEX at
  **894 < 900**. Same measured lever as QG-O4 (tp 0.50→0.60). ⚠️ The IDX rows are founder-created
  (`founder_forced_live`) — §21.4 says registry-scoped fixes miss them; confirm before touching.

---

## PHASE 0 — CLEANUP & GUARDRAILS (~1 wk) — done, kept for provenance
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

- [x] **P1-1** (T2) Stock derivatives ingest: code landed 2026-07-19. `bhavcopy_ingest.py` now accepts `STO`/`STF`, includes stock F&O underlyings, and exposes `--instr-types`; `BhavcopyStore` serves `STF` futures via `underlying_daily()` and `STO` option chains. VPS full 2024–2026 store verified 2026-07-19: 626 trading days through 2026-07-17, with real `underlying_daily()` bars for RELIANCE, SBIN, HDFCBANK, ICICIBANK, TCS, INFY, AXISBANK, LT, BHARTIARTL, KOTAKBANK, plus NIFTY/BANKNIFTY.
- [x] **P1-2** (T2) Earnings calendar: file-backed store + CSV ingest landed 2026-07-19; `earnings_calendar_fetch_nse.py` now fetches official NSE board-meeting financial-results dates for the default top-30 F&O names, handles NSE CSV header quirks, and the backend scheduler refreshes a ±45-day window weekly on Saturday 05:00 IST. VPS runtime store verified 2026-07-19: 309 deduped earnings events across 193 distinct dates from 2024-01-11 through 2026-07-18; RELIANCE has 11 events (2024-01-19 → 2026-07-17), TCS has 10.
- [x] **P1-3** (T2) Pre-2024 history: old NSE F&O archive source fixed to `nsearchives.nseindia.com` and pre-2024 legacy CSV rows normalize into the same UDiFF-shaped store (`IDF`/`IDO`, lot-size fallback remains domain-driven). VPS store verified 2026-07-19: 1,234 index F&O bhavcopy trading days across 2019-01-01→2023-12-29; 2020 Q1 has 63 NIFTY/BANKNIFTY daily bars and a real NIFTY option chain on 2020-03-24. `run_oos_validation.py --start 2020-01-01 --end 2020-03-31` exits 0 in the backend container.
- [x] **P1-4** (T2) Participant-wise **F&O** OI: added official NSE all-reports fetcher `scripts/participant_oi_fetch_nse.py`, hardened the parser for NSE title-preamble CSVs, and backfilled the file store. VPS store verified 2026-07-19: 1,359 available weekdays / 5,436 rows from 2019-01-01→2024-07-05 with `CLIENT`, `DII`, `FII`, and `PRO` buckets; NSE labels the report discontinued from 2024-07-08, so the fetcher caps there and treats archive 404s as unavailable days.
- [x] **P1-5** (T2) Options-minute backfill via Upstox **Expired Instruments APIs** (official). D-2 approved 2026-07-19. App alignment landed: `/api/upstox/data-health`, Broker Keys, Market Hub, Hermes Research Lab, and Hermes tool `get_upstox_data_health` now expose Plus capability + real store coverage. Importer now supports `--sleep-sec` throttling after overnight runs hit Upstox 429s. VPS coverage verified 2026-07-19: `options_1m` store spans 2024-08-23–2026-07-14 across NIFTY/BANKNIFTY with 871 underlying-days, 10,948 contract-days, and 4,070,656 rows, far beyond the prior 204-day wall; the slow fill-in importer had exited cleanly by follow-up process check.
- [x] **P1-6** (T1) Freshness watchdogs: `data.store_coverage` includes earnings dates and participant-OI stores alongside bhavcopy/index/options stores; VPS runtime now has populated earnings, participant-OI, bhavcopy, and options-minute stores for those probes.

## PHASE 2 — ANALYTICS & JUDGE REFORM (~2 wks; needs parts of Phase 1)

- [x] **P2-1** (T3) IV surface wiring: `core/iv_surface.py` now feeds `market_context`, optional seller-gate richness telemetry/blocking (`IV_SURFACE_SELLER_MIN_Z`), dynamic contract-score surface factors, read-only `/ops/iv-surface`, and the Edge Lab Analytics UI/API snapshot.
- [x] **P2-2** (T2) ERL upgrade: **Deflated Sharpe + explicit trials-count** per family on every snapshot/verdict (extends the existing multiple-testing penalty). Done: Edge Lab ERL rows now carry `trials_count` and `deflated_sharpe` proxy payload; CANDIDATE_EDGE is downgraded to OVERFIT_RISK when the deflated score fails.
- [x] **P2-3** (T3) EOD judge extensions: stock F&O support is available through the P1 store (`STF`/`STO`); event-conditional mode filters EOD signals around supplied earnings/event dates and reports the before/after signal audit. CLI supports `run_oos_validation.py --mode event`.
- [x] **P2-4** (T3, =INFRA-62 start) Judge façade: added `core/judge_facade.grade(strategy_cfg, mode=eod|event|intraday|regime)` plus read-only `/ops/judge/grade`; EOD/event return normalized verdicts now, while intraday/regime point to their existing persisted runners until a later deletion pass.
- [x] **P2-5** (T3) Two-expiry calendar structures in the EOD backtester: added `calendar_spread` research pricing (short near expiry, long far expiry, same strike/type), Edge Lab proposal API/UI support, and focused fake-store regression coverage. Research-only; no live execution path added.

## PHASE 3 — STRATEGY SLEEVES (~3–4 wks staged; each rides the full ladder)

- [x] **P3-1** (T3) **S1 Earnings IV-crush premium (FLAGSHIP):** research-only selector + validator landed 2026-07-19. `core/earnings_iv_crush.py` builds top-30 stock-F&O earnings signals with exact T−1 entry / T+1 forced exit, skips event≡expiry and expiry-week physical-settlement traps, prices a defined-risk iron condor through the EOD option backtester, and reports explicit sample / deflated-Sharpe / 3× cost-floor paper gates. CLI: `python scripts/run_earnings_iv_crush_validation.py --start 2024-01-01 --end 2026-07-18`. Local verification passed with fake-store unit coverage; real-stock OOS must be read from the populated VPS bhavcopy store before any registry paper wake. Acceptance for paper remains hard-gated: OOS verdict n≥300 events, DSR-positive, cost-floor pass, before any paper trade.
- [x] **P3-2** (T3) **S3 Daily delta-1 momentum + overnight:** research-only validator landed 2026-07-19. `core/daily_delta1_momentum.py` builds 20-day daily time-series momentum and a separate overnight-drift variant, prices deep-ITM single-leg delta-1 participation via the EOD judge, reports vol-target lots, DSR, and a multi-regime paper gate. CLI: `python scripts/run_daily_delta1_validation.py --start 2019-01-01 --end 2026-07-18`. No paper wake unless OOS verdict passes across ≥2 regimes incl. a down year.
- [x] **P3-3** (T2) **S4 Slow premium core restore:** research-only validator landed 2026-07-19. `core/slow_premium_core.py` restores QG-O1's held-to-expiry 3% OTM / width-10 put-spread geometry and adds a SENSEX monthly defined-risk strangle/condor candidate, both gated by P2-1 IV-surface richness so low-vol/cheap-premium days mostly stand down. CLI: `python scripts/run_slow_premium_validation.py --start 2019-01-01 --end 2026-07-18`. Acceptance remains hard-gated: re-pass OOS + DSR before any registry paper wake.
- [x] **P3-4** (T2) **S1b PEAD validation:** research-only PEAD validator landed 2026-07-19 in `core/phase3_remaining.py`; preliminary honest test follows the stock future in the event-day direction for a 5-day post-earnings window and reports sample/expectancy/DSR/paper gate. Mixed India evidence remains respected: flat/negative output kills the idea until real surprise-magnitude data exists.
- [x] **P3-5** (T2) **S5 participant-OI overlay:** research-only participant-OI overlay validator landed 2026-07-19; uses the P1-4 official store to test FII-vs-client futures bias as a directional gate/size feature and returns `wire_overlay=false` unless sample/DSR/expectancy pass.
- [x] **P3-6** (T2) Paused-seller re-judge: research-only paused-seller proxy re-judge landed 2026-07-19; QG-O11, three RAE sellers, and QG-O4 artifact proxy are priced on re-derived 0.50/0.90 geometry with 2/5/8% slippage stress. Founder wake/delete remains external and evidence-gated.
- [x] **P3-7** (T3) Book assembly: non-mutating Phase 3 book assembly landed 2026-07-19; aggregates P3 sleeve gates, reports candidate count/heat/correlation cap, and keeps `paper_book_ready=false` when no sleeve passes.

## PHASE 4 - HERMES v2: RESEARCH ANALYST (~2 wks staged)

- [x] **P4-1** (T1) `GEMINI_MODEL` -> `gemini-3-flash-preview` (free tier, 1,500 req/day): default chat/planner/narrator/wiki Gemini model updated 2026-07-19, `backend/.env.example` aligned, local + VPS runtime env overrides updated, and focused tests verify tool-planner declarations plus embeddings parity (`gemini-embedding-001`, 768 dimensions).
- [x] **P4-2** (T2) **H1 corpus:** `wiki/Research/` now has 26 curated notes across Grinold-Kahn, Sinclair, Carver, Natenberg, Aronson, Lopez de Prado, SEBI FY25, India VRP/day-night/PEAD/Momentum-30, Edge Report v3, and QuantG laws. `research_rag.reindex_all` indexes these disk notes as `type=research` with source refs before wiki/lessons/OOS recall.
- [x] **P4-3** (T2) **H3 opportunity probes:** deterministic Phase 4 runner added in `core/phase4_research.py` + `scripts/run_phase4_research.py`; probes cover VRP-by-strike, earnings IV-runup, overnight/intraday split, term-structure richness, and participant-OI extremes, with `/ops/research-signals/run` persisting to `db.research_signals`.
- [x] **P4-4** (T3) **H2 hypothesis cards:** Phase 4 cards are falsifiable and require hypothesis, who-pays rationale, universe, horizon, judge, data needed, kill criteria, and at least one corpus/probe citation; citationless cards raise `ValueError`. CLI can persist PROPOSED cards into `db.research_hypotheses` as `phase4_hypothesis_card`.
- [x] **P4-5** (T2) **H5 calibration:** card calibration summary is implemented and exposed through `get_hermes_brain_health` as `research_calibration` with total/tested/hit-rate counts. Outcomes remain tied to verdict/lesson evidence; untested cards do not masquerade as learning.
- [x] **P4-6** (T1) D-3 paid research-lane routing status implemented but disabled by default. Weekly synthesis uses a paid lane only if `HERMES_PAID_RESEARCH_ENABLED=true` and `HERMES_RESEARCH_MODEL`/`PAID_RESEARCH_MODEL` is set; founder D-3 is still open, so current runtime stays free-model only.
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
- [x] Open Hermes finding: `options_1m` coverage only advances via live capture while the book is paused. Closed by P1-5: Upstox Expired Instruments coverage now spans 871 underlying-days / 4,070,656 rows on VPS.
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
