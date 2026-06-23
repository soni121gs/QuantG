import React, { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  Bot,
  Layers,
  PieChart,
  RefreshCw,
  Shield,
  Target,
  TrendingDown,
  TrendingUp,
  Wallet,
  Zap,
  Power,
} from "lucide-react";
import { api, formatINR } from "../lib/api";
import { useExecutionState } from "../hooks/useExecutionState";
import { usePolling } from "../hooks/usePolling";
import { KpiCard } from "../components/dashboard/KpiCard";
import { StrategyPerformanceTable } from "../components/dashboard/StrategyPerformanceTable";
import { StrategyLedgerRow, Field, StatusPill } from "../components/dashboard/StrategyLedgerRow";
import { HealthScoreList } from "../components/dashboard/HealthScoreList";
import { AllocationList } from "../components/dashboard/AllocationList";

const money = (value) => `INR ${formatINR(value ?? 0)}`;

const toneClass = (value) => ((value ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]");
const ACTIVE_POSITION_STATES = ["OPEN", "FILLED"];
const PENDING_POSITION_STATES = ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "EXITING"];
const BROKER_OPEN_ORDER_STATES = ["NEW", "PLACED", "OPEN", "PARTIAL_FILL", "PENDING", "PENDING_BROKER", "TRIGGER PENDING", "MODIFY PENDING", "VALIDATION PENDING", "EXIT_PENDING"];
const PROBLEM_ORDER_STATES = ["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE"];

const asStatus = (value) => String(value || "").toUpperCase();
const hasQty = (value) => Math.abs(parseInt(value || 0, 10)) > 0;

const quoteAge = (value) => (value == null ? "-" : `${Math.round(Number(value) || 0)}s`);

const metric = (row, path) => {
  const parts = path.split(".");
  let cur = row;
  for (const p of parts) {
    if (cur == null) return 0;
    cur = cur[p];
  }
  return cur ?? 0;
};

const statusTone = (status) => {
  const s = String(status || "").toUpperCase();
  if (["OPEN", "FILLED", "ACTIVE"].includes(s)) return "good";
  if (["PENDING", "RESERVED", "PENDING_OPEN", "PENDING_BROKER", "EXITING"].includes(s)) return "warn";
  if (["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE", "ERROR"].includes(s)) return "bad";
  return "neutral";
};

const filledOrder = (status) => {
  const s = String(status || "").toUpperCase();
  return ["COMPLETE", "FILLED", "TRADED"].includes(s);
};

export default function Dashboard() {
  const {
    positions: execPositions,
    orders: execOrders,
    skippedSignals,
    strategyPositions: execStrategyPositions,
    summary: executionSummary,
    upstoxDataHealth,
    brokerReconciliation,
  } = useExecutionState({ pollMs: 15000 });
  const positions = execPositions;
  const orders = execOrders;
  const [funds, setFunds] = useState(null);
  const [telemetry, setTelemetry] = useState(null);
  const [marketSession, setMarketSession] = useState(null);
  const [strategyAnalytics, setStrategyAnalytics] = useState(null);
  const [optionChain, setOptionChain] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [busyStrategy, setBusyStrategy] = useState(null);
  const [activeTab, setActiveTab] = useState("overview");

  const load = useCallback(async () => {
    try {
      const [f, t, c, s, leaderboard] = await Promise.all([
        api.get("/funds"),
        api.get("/v1/dashboard/telemetry"),
        api.get("/market/session-status"),
        api.get("/strategies/leaderboard"),
        api.get("/upstox/option-chain", { params: { underlying: "NIFTY" } }).catch(() => ({ data: null })),
      ]);
      // Execution/positions/PnL are already kept fresh by the global
      // ExecutionStateContext (15s poll) — no need to re-fetch them here.
      setFunds(f.data);
      setTelemetry(t.data);
      setMarketSession(c.data);
      setStrategyAnalytics(s.data);
      setOptionChain(leaderboard.data);
      setLoadError("");
    } catch (e) {
      setLoadError(e?.response?.data?.detail || e.message || "Dashboard data could not be loaded");
    }
  }, []);

  usePolling(load, 60000, { hiddenMs: 0 });

  const pnl = executionSummary.net_pnl ?? 0;
  const grossPnl = executionSummary.gross_pnl ?? pnl;
  const charges = executionSummary.charges ?? 0;
  const openPositions = executionSummary.open_positions ?? positions.length;
  const strategies = useMemo(() => telemetry?.strategies_page_data || [], [telemetry?.strategies_page_data]);
  const liveStrategies = useMemo(() => strategies.filter((s) => s.status === "live").length, [strategies]);
  const strategyCount = strategies.length;
  const marketOpen = marketSession ? marketSession.global_status === "OPEN" : telemetry?.market_status?.is_open;
  const marketStatusLabel = marketSession?.global_status || (marketOpen ? "OPEN" : "CLOSED");
  const firstRisk = strategies[0]?.risk_settings || {};
  const openOrders = useMemo(() => orders.filter((o) => BROKER_OPEN_ORDER_STATES.includes(asStatus(o.execution_status || o.status))), [orders]);
  const failedOrders = useMemo(() => orders.filter((o) => PROBLEM_ORDER_STATES.includes(asStatus(o.execution_status || o.status))), [orders]);
  const openStrategyPositions = useMemo(() => positions.filter((p) => hasQty(p.qty)), [positions]);
  const pendingStrategyPositions = useMemo(() => execStrategyPositions.filter((p) => PENDING_POSITION_STATES.includes(asStatus(p.execution_status || p.status))), [execStrategyPositions]);
  const missingProtectionCount = useMemo(() => openStrategyPositions.filter((p) => p.stop_loss == null || p.take_profit == null).length, [openStrategyPositions]);

  const strategiesWithExecution = useMemo(() => {
    const positionsByStrategy = new Map();
    openStrategyPositions.forEach((p) => {
      if (p.strategy_id && !positionsByStrategy.has(p.strategy_id)) positionsByStrategy.set(p.strategy_id, p);
    });
    execStrategyPositions.forEach((p) => {
      if (p.strategy_id && !positionsByStrategy.has(p.strategy_id)) positionsByStrategy.set(p.strategy_id, p);
    });

    const pendingByStrategy = new Map();
    const failedByStrategy = new Map();
    orders.forEach((o) => {
      const sid = o.strategy_id || (String(o.source || "").includes("strategy:") ? String(o.source).split("strategy:").pop() : "");
      if (!sid) return;
      const status = asStatus(o.execution_status || o.status);
      if (BROKER_OPEN_ORDER_STATES.includes(status) && !pendingByStrategy.has(sid)) pendingByStrategy.set(sid, o);
      if (PROBLEM_ORDER_STATES.includes(status) && !failedByStrategy.has(sid)) failedByStrategy.set(sid, o);
    });

    const known = new Set();
    const rows = strategies.map((s) => {
      known.add(s.strategy_id);
      return {
        ...s,
        position: positionsByStrategy.get(s.strategy_id),
        pendingOrder: pendingByStrategy.get(s.strategy_id),
        failedOrder: failedByStrategy.get(s.strategy_id),
      };
    });

    openStrategyPositions.forEach((p) => {
      if (!p.strategy_id || known.has(p.strategy_id)) return;
      rows.push({
        strategy_id: p.strategy_id,
        name: p.strategy_name || p.strategy_id,
        status: "unknown",
        state: p.execution_status || "OPEN",
        position: p,
      });
    });

    return rows.sort((a, b) => {
      const priority = (row) => (row.failedOrder ? 0 : row.position && hasQty(row.position.qty) ? 1 : row.pendingOrder ? 2 : row.status === "live" ? 3 : 4);
      return priority(a) - priority(b);
    });
  }, [strategies, openStrategyPositions, execStrategyPositions, orders]);

  // Tab Filtering
  const equityPositions = useMemo(() => positions.filter((p) => p.asset_type === "equity" || p.exchange === "NSE"), [positions]);
  const equityOrders = useMemo(() => orders.filter((o) => o.exchange === "NSE"), [orders]);
  // Verbose option symbols look like "NIFTY 23200 CE 09 JUN 26" — they contain a
  // spaced CE/PE token and never end with "CE"/"PE", so endsWith misses every real
  // option. Match the spaced token (and guard against missing symbols).
  const isOptionSymbol = (sym) => {
    const s = String(sym || "");
    return s.includes(" CE ") || s.includes(" PE ") || /\bCE\b|\bPE\b/.test(s);
  };
  const foPositions = useMemo(() => positions.filter((p) => p.option_type || p.exchange === "NFO" || p.exchange === "BFO" || isOptionSymbol(p.symbol)), [positions]);
  const foStrategies = useMemo(() => strategies.filter((s) => s.asset_class === "option" || s.strategy_id.includes("Straddle") || s.strategy_id.includes("Scalper") || s.name.includes("Option")), [strategies]);
  const leaderboardRows = useMemo(() => strategyAnalytics?.leaderboard || [], [strategyAnalytics]);
  const bestStrategy = strategyAnalytics?.best_strategy;
  const worstStrategy = strategyAnalytics?.worst_strategy;

  const strategySummary = useMemo(() => {
    const counts = strategiesWithExecution.reduce((acc, row) => {
      const key = row.state || "IDLE";
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
    return [
      { label: "Open pos", value: openStrategyPositions.length, tone: "good" },
      { label: "Pending", value: pendingStrategyPositions.length + openOrders.length, tone: "warn" },
      { label: "No SL/TP", value: missingProtectionCount, tone: missingProtectionCount ? "bad" : "good" },
      { label: "Failed", value: failedOrders.length, tone: failedOrders.length ? "bad" : "good" },
      { label: "Flat", value: Math.max(0, strategiesWithExecution.length - openStrategyPositions.length), tone: "neutral" },
    ];
  }, [strategiesWithExecution, openStrategyPositions.length, pendingStrategyPositions.length, openOrders.length, missingProtectionCount, failedOrders.length]);

  const handleRefresh = async () => {
    setRefreshing(true);
    await load();
    setTimeout(() => setRefreshing(false), 450);
  };

  const killSwitch = async () => {
    if (!window.confirm("Trigger emergency kill switch? This pauses live strategies, switches to paper mode, and disables ledger re-entry.")) return;
    await api.post("/risk/kill-switch");
    await load();
  };

  const exitAllUpstox = async () => {
    if (!window.confirm("Exit all Upstox positions now? Tag-based exits can affect total instrument quantity when multiple strategies share the same instrument.")) return;
    await api.post("/upstox/exit-all");
    await load();
  };

  const toggleStrategy = async (id) => {
    setBusyStrategy(id);
    try {
      await api.post(`/strategies/${id}/toggle`);
      await load();
    } finally {
      setBusyStrategy(null);
    }
  };

  const exitStrategy = async (id) => {
    if (!window.confirm("Emergency Square Off: exit all open positions for this strategy?")) return;
    setBusyStrategy(id);
    try {
      await api.post(`/strategies/${id}/exit-all`);
      await load();
    } finally {
      setBusyStrategy(null);
    }
  };

  return (
    <div className="space-y-3 qd-dashboard-compact" data-testid="dashboard-page">
      <section className="qd-card overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-[var(--qd-border)] px-3 py-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-head text-base font-semibold text-[var(--qd-text)]">Dashboard</h1>
              <StatusPill tone={marketOpen ? "good" : "warn"}>{marketStatusLabel}</StatusPill>
              <StatusPill>{openPositions} Positions</StatusPill>
              <StatusPill>{liveStrategies}/{strategyCount} Live</StatusPill>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs md:grid-cols-4 xl:grid-cols-6">
              <Field label="Session P&L" value={money(pnl)} tone={toneClass(pnl)} />
              <Field label="Account Cash" value={money(funds?.available_cash)} />
              <Field label="Used Margin" value={money(funds?.used_margin)} />
              <Field label="Open Orders" value={openOrders.length} tone={openOrders.length ? "text-[var(--qd-warn)]" : "text-[var(--qd-text-2)]"} />
              <Field label="Failed" value={failedOrders.length} tone={failedOrders.length ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
              <Field label="Kill Gate" value={firstRisk.kill_switch_enabled ? "ARMED" : "CLEAR"} tone={firstRisk.kill_switch_enabled ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 lg:justify-end">
            <button
              type="button"
              onClick={handleRefresh}
              disabled={refreshing}
              className="flex h-8 items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 font-mono text-[11px] uppercase text-[var(--qd-text-2)] hover:text-[var(--qd-text)] disabled:opacity-50"
              title="Refresh data"
            >
              <RefreshCw size={13} className={refreshing ? "animate-spin" : ""} /> Refresh
            </button>
            <Link
              to="/ai-bot"
              className="flex h-8 items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 font-mono text-[11px] uppercase text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
            >
              <Bot size={13} /> AI
            </Link>
            <Link
              to="/strategies"
              className="qd-force-white flex h-8 items-center gap-2 rounded-[var(--qd-radius-sm)] bg-[var(--qd-accent)] px-3 font-mono text-[11px] font-semibold uppercase hover:bg-[var(--qd-accent-hover)]"
              data-testid="new-strategy-btn"
            >
              <Zap size={13} /> Strategy
            </Link>
            <button
              type="button"
              onClick={killSwitch}
              className="flex h-8 items-center gap-2 rounded bg-[var(--qd-loss)] px-3 font-mono text-[11px] font-semibold uppercase text-white hover:opacity-90"
            >
              <Power size={13} /> Kill
            </button>
            <button
              type="button"
              onClick={exitAllUpstox}
              className="flex h-8 items-center gap-2 rounded border border-[rgba(255,59,48,0.42)] px-3 font-mono text-[11px] font-semibold uppercase text-[var(--qd-loss)] hover:bg-[rgba(255,59,48,0.1)]"
            >
              <AlertTriangle size={13} /> Exit All
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-px bg-[var(--qd-border)] text-xs md:grid-cols-4">
          <div className="bg-[var(--qd-surface)] px-3 py-2">
            <Field label="Max Lot" value="1 contract" />
          </div>
          <div className="bg-[var(--qd-surface)] px-3 py-2">
            <Field label="Cooldown" value={`${strategies[0]?.cooldown_minutes ?? 25} min`} />
          </div>
          <div className="bg-[var(--qd-surface)] px-3 py-2">
            <Field label="Loss Cutoff" value={money(firstRisk.daily_loss_limit || 0)} />
          </div>
          <div className="bg-[var(--qd-surface)] px-3 py-2">
            <Field label="Protection" value={`${missingProtectionCount}/${openStrategyPositions.length} missing`} tone={missingProtectionCount ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
          </div>
        </div>
      </section>

      {loadError && (
        <div className="qd-card border-l-2 border-l-[var(--qd-warn)] p-4" data-testid="dashboard-load-error">
          <div className="flex items-start gap-3">
            <AlertTriangle size={18} className="mt-0.5 text-[var(--qd-warn)]" />
            <div>
              <div className="qd-section-title text-[var(--qd-warn)]">Data refresh issue</div>
              <div className="mt-1 text-sm text-[var(--qd-text-2)]">{loadError}</div>
            </div>
          </div>
        </div>
      )}

      {/* Modern Accented Glassmorphism Tabs */}
      <div className="flex gap-2 overflow-x-auto border-b border-[var(--qd-border)] pb-px">
        {[
          { id: "overview", label: "General Console" },
          { id: "equity", label: "Equity Spot" },          { id: "fo", label: "Derivatives (F&O)" },
        ].map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setActiveTab(t.id)}
            className={`whitespace-nowrap border-b-2 border-transparent px-3 py-2 font-head text-[11px] font-semibold uppercase transition-all ${
              activeTab === t.id
                ? "text-white border-[var(--qd-cyan)] qd-tab-active"
                : "text-[var(--qd-text-3)] hover:text-[var(--qd-text)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Panel Render Logic */}
      {activeTab === "overview" && (
        <div className="space-y-3">
          {/* Main summary grid */}
          <section className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            <KpiCard label="Account Balance" value={money(funds?.available_cash)} icon={Wallet} sub={funds?.source === "live" ? "Live Account Balance" : "Simulated Paper Cash"} />
            <KpiCard label="Utilized Margin" value={money(funds?.used_margin)} icon={Layers} sub={funds?.source === "live" ? "Live Blocked Margin" : "Paper Blocked Margin"} />
            <KpiCard label="Net P&L" value={money(pnl)} icon={pnl >= 0 ? TrendingUp : TrendingDown} tone={toneClass(pnl)} sub={`Gross ${money(grossPnl)} after charges ${money(charges)}`} />
            <KpiCard
              label="Trade Safety"
              value={`${missingProtectionCount}/${openStrategyPositions.length}`}
              icon={Activity}
              tone={missingProtectionCount ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"}
              sub={`${openOrders.length} pending orders, ${failedOrders.length} failed`}
            />
          </section>

          {/* Compact diagnostics strip — Data Health / Reconciliation / Skipped.
              Low-priority telemetry condensed into one slim bar to free up space. */}
          <section className="qd-card flex flex-wrap items-center gap-x-5 gap-y-2 px-3 py-2 text-[11px]">
            <div className="flex items-center gap-2">
              <span className="font-mono uppercase text-[var(--qd-text-3)]">Feed</span>
              <StatusPill tone={upstoxDataHealth?.readiness === "READY" ? "good" : "warn"}>{upstoxDataHealth?.readiness || "UNKNOWN"}</StatusPill>
              <span className={upstoxDataHealth?.connected ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>{upstoxDataHealth?.connected ? "WS" : "WS✕"}</span>
              <span className={upstoxDataHealth?.quote_stale ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"}>age {quoteAge(upstoxDataHealth?.quote_age_sec)}</span>
            </div>

            <span className="hidden h-3 w-px bg-[var(--qd-border)] sm:inline-block" />

            <div className="flex items-center gap-2">
              <span className="font-mono uppercase text-[var(--qd-text-3)]">Recon</span>
              <StatusPill tone={brokerReconciliation?.status === "OK" ? "good" : "warn"}>{brokerReconciliation?.status || "UNKNOWN"}</StatusPill>
              <span className={(brokerReconciliation?.mismatches?.pending_orders_without_broker_match || brokerReconciliation?.mismatches?.position_key_mismatches) ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"}>
                gaps {(brokerReconciliation?.mismatches?.pending_orders_without_broker_match ?? 0) + (brokerReconciliation?.mismatches?.position_key_mismatches ?? 0)}
              </span>
            </div>

            <span className="hidden h-3 w-px bg-[var(--qd-border)] sm:inline-block" />

            <div className="flex min-w-0 items-center gap-2">
              <span className="font-mono uppercase text-[var(--qd-text-3)]">Skipped</span>
              <StatusPill tone={skippedSignals.length ? "warn" : "good"}>{skippedSignals.length}</StatusPill>
              <span className="truncate text-[var(--qd-text-2)]">
                {skippedSignals.length === 0 ? "none" : (skippedSignals[0]?.reason_code || skippedSignals[0]?.reason || "skipped")}
              </span>
            </div>
          </section>

          <section className="grid grid-cols-1 gap-3 xl:grid-cols-4">
            <KpiCard
              label="Best Strategy"
              value={bestStrategy?.strategy_name || "-"}
              icon={TrendingUp}
              tone="text-[var(--qd-profit)]"
              sub={bestStrategy ? `${money(metric(bestStrategy, "lifetime.net_pnl"))} lifetime` : "Closed trade history required"}
            />
            <KpiCard
              label="Worst Strategy"
              value={worstStrategy?.strategy_name || "-"}
              icon={TrendingDown}
              tone={worstStrategy ? "text-[var(--qd-loss)]" : "text-white"}
              sub={worstStrategy ? `${money(metric(worstStrategy, "lifetime.net_pnl"))} lifetime` : "Closed trade history required"}
            />
            <KpiCard
              label="More Capital"
              value={strategyAnalytics?.capital_allocation?.[0]?.strategy_name || "-"}
              icon={PieChart}
              tone={strategyAnalytics?.capital_allocation?.[0] ? "text-[var(--qd-profit)]" : "text-white"}
              sub={strategyAnalytics?.capital_allocation?.[0] ? `${strategyAnalytics.capital_allocation[0].recommended_percent}% recommended only` : "No increase recommended"}
            />
            <KpiCard
              label="Pause Candidate"
              value={(leaderboardRows.find((row) => row.recommendation === "PAUSE_REVIEW") || worstStrategy)?.strategy_name || "-"}
              icon={AlertTriangle}
              tone={(leaderboardRows.find((row) => row.recommendation === "PAUSE_REVIEW") || worstStrategy) ? "text-[var(--qd-warn)]" : "text-white"}
              sub="Recommendation only"
            />
          </section>

          <details className="qd-dashboard-details">
            <summary>
              <span>
                <span className="qd-section-title">Review Desk</span>
                <strong>Strategy analytics and historical performance</strong>
              </span>
              <span className="font-mono text-xs text-[var(--qd-accent)]">Open</span>
            </summary>
            <section className="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-[1.4fr_0.6fr]">
              <div className="qd-card overflow-hidden">
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] p-3">
                <div>
                  <div className="qd-section-title">// Closed trade performance</div>
                  <h2 className="mt-1 font-head text-sm font-semibold text-white">Strategy Leaderboard</h2>
                </div>
                <StatusPill tone={strategyAnalytics?.data_quality?.included_closed_trades ? "good" : "neutral"}>
                  {strategyAnalytics?.data_quality?.included_closed_trades || 0} Trades
                </StatusPill>
              </div>
              <StrategyPerformanceTable rows={leaderboardRows} />
              {(strategyAnalytics?.data_quality?.excluded_missing_strategy || strategyAnalytics?.data_quality?.excluded_unknown_strategy || strategyAnalytics?.data_quality?.excluded_missing_close_time) ? (
                <div className="border-t border-[var(--qd-border)] px-3 py-2 font-mono text-[11px] uppercase text-[var(--qd-warn)]">
                  Excluded rows: {strategyAnalytics?.data_quality?.excluded_missing_strategy || 0} missing strategy, {strategyAnalytics?.data_quality?.excluded_unknown_strategy || 0} unknown strategy, {strategyAnalytics?.data_quality?.excluded_missing_close_time || 0} missing close time.
                </div>
              ) : null}
            </div>

              <div className="grid grid-cols-1 gap-3">
                <div className="qd-card p-3">
                <div className="mb-3 flex items-center justify-between">
                  <div>
                    <div className="qd-section-title">// Score 0-100</div>
                    <h2 className="mt-1 font-head text-sm font-semibold text-white">Health Scores</h2>
                  </div>
                  <Shield size={18} className="text-[var(--qd-text-3)]" />
                </div>
                <HealthScoreList rows={strategyAnalytics?.health_scores || []} />
              </div>

                <div className="qd-card p-3">
                <div className="mb-3 flex items-center justify-between">
                  <div>
                    <div className="qd-section-title">// Recommendation only</div>
                    <h2 className="mt-1 font-head text-sm font-semibold text-white">Capital Allocation</h2>
                  </div>
                  <Wallet size={18} className="text-[var(--qd-text-3)]" />
                </div>
                <AllocationList rows={strategyAnalytics?.capital_allocation || []} />
              </div>
              </div>
            </section>

            <section className="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-3">
            <div className="qd-card p-3">
              <div className="qd-section-title">// Winners</div>
              <h2 className="mt-1 font-head text-sm font-semibold text-white">Top Performers</h2>
              <div className="mt-3 space-y-2">
                {(strategyAnalytics?.top_performers || []).length === 0 ? <div className="text-xs text-[var(--qd-text-3)]">No closed winners yet.</div> : strategyAnalytics.top_performers.map((row) => (
                  <Field key={row.strategy_id} label={row.strategy_name} value={money(row.lifetime?.net_pnl)} tone={toneClass(row.lifetime?.net_pnl)} />
                ))}
              </div>
            </div>
            <div className="qd-card p-3">
              <div className="qd-section-title">// Losers</div>
              <h2 className="mt-1 font-head text-sm font-semibold text-white">Worst Performers</h2>
              <div className="mt-3 space-y-2">
                {(strategyAnalytics?.worst_performers || []).length === 0 ? <div className="text-xs text-[var(--qd-text-3)]">No closed losers yet.</div> : strategyAnalytics.worst_performers.map((row) => (
                  <Field key={row.strategy_id} label={row.strategy_name} value={money(row.lifetime?.net_pnl)} tone={toneClass(row.lifetime?.net_pnl)} />
                ))}
              </div>
            </div>
            <div className="qd-card p-3">
              <div className="qd-section-title">// Drawdown</div>
              <h2 className="mt-1 font-head text-sm font-semibold text-white">Drawdown Monitor</h2>
              <div className="mt-3 space-y-2">
                {(strategyAnalytics?.drawdown_monitor || []).length === 0 ? <div className="text-xs text-[var(--qd-text-3)]">No drawdown history yet.</div> : strategyAnalytics.drawdown_monitor.map((row) => (
                  <Field key={row.strategy_id} label={row.strategy_name} value={money(row.lifetime?.max_drawdown)} tone={row.lifetime?.max_drawdown > 0 ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
                ))}
              </div>
            </div>
            </section>
          </details>

          {/* Position Integrity Status */}
          <div className="qd-card p-3">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-2">
              <div>
                <div className="qd-section-title">// HEALTH CHECK</div>
                <h2 className="mt-1 font-head text-sm font-semibold text-white">Position Integrity</h2>
              </div>
              <span className={`inline-flex items-center rounded border px-2 py-0.5 font-mono text-[10px] font-semibold uppercase ${
                (executionSummary.summary?.position_integrity?.orphans || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.missing_sl || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.missing_tp || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.strategy_mismatches || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.failed_orders || 0) === 0
                  ? "border-[rgba(0,230,118,0.38)] bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]"
                  : "border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.1)] text-[var(--qd-loss)]"
              }`}>
                {(executionSummary.summary?.position_integrity?.orphans || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.missing_sl || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.missing_tp || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.strategy_mismatches || 0) === 0 &&
                (executionSummary.summary?.position_integrity?.failed_orders || 0) === 0
                  ? "Healthy"
                  : "Attention Required"}
              </span>
            </div>
            
            <div className="mt-3 grid grid-cols-2 gap-2 text-center font-mono md:grid-cols-5">
              <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2">
                <div className="text-[10px] uppercase text-[var(--qd-text-3)]">Orphans</div>
                <div className={`mt-1 text-base font-semibold ${(executionSummary.summary?.position_integrity?.orphans || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.orphans ?? 0}
                </div>
              </div>
              <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2">
                <div className="text-[10px] uppercase text-[var(--qd-text-3)]">Missing SL</div>
                <div className={`mt-1 text-base font-semibold ${(executionSummary.summary?.position_integrity?.missing_sl || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.missing_sl ?? 0}
                </div>
              </div>
              <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2">
                <div className="text-[10px] uppercase text-[var(--qd-text-3)]">Missing TP</div>
                <div className={`mt-1 text-base font-semibold ${(executionSummary.summary?.position_integrity?.missing_tp || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.missing_tp ?? 0}
                </div>
              </div>
              <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2">
                <div className="text-[10px] uppercase text-[var(--qd-text-3)]">Mismatches</div>
                <div className={`mt-1 text-base font-semibold ${(executionSummary.summary?.position_integrity?.strategy_mismatches || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.strategy_mismatches ?? 0}
                </div>
              </div>
              <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2">
                <div className="text-[10px] uppercase text-[var(--qd-text-3)]">Failed</div>
                <div className={`mt-1 text-base font-semibold ${(executionSummary.summary?.position_integrity?.failed_orders || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.failed_orders ?? 0}
                </div>
              </div>
            </div>
          </div>

          <section>
            {/* Strategy summaries and engines */}
            <div className="space-y-3">
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-2">
                <div>
                  <div className="qd-section-title">// Runtime ledger states</div>
                  <h2 className="mt-1 font-head text-sm font-semibold text-white">Strategy Position Ledger</h2>
                </div>
                <Link to="/strategies" className="font-mono text-[11px] uppercase text-[var(--qd-accent)] hover:text-[var(--qd-text)]">
                  Manage strategies
                </Link>
              </div>
              <div className="qd-card overflow-hidden">
                <div className="grid grid-cols-2 gap-px bg-[var(--qd-border)] md:grid-cols-5">
                  {strategySummary.map((item) => (
                    <div key={item.label} className="bg-[var(--qd-bg)] p-2.5">
                      <div className="qd-section-title">{item.label}</div>
                      <div className={`mt-1 font-mono text-lg font-semibold ${
                        item.tone === "good" ? "text-[var(--qd-profit)]" :
                        item.tone === "warn" ? "text-[var(--qd-warn)]" :
                        item.tone === "bad" ? "text-[var(--qd-loss)]" :
                        "text-white"
                      }`}>{item.value}</div>
                    </div>
                  ))}
                </div>

                <div>
                  {strategiesWithExecution.length === 0 ? (
                    <div className="px-4 py-5 text-center text-xs text-[var(--qd-text-3)]">No strategies loaded.</div>
                  ) : (
                    strategiesWithExecution.map((row) => (
                      <StrategyLedgerRow key={row.strategy_id} row={row} onToggle={toggleStrategy} onExit={exitStrategy} />
                    ))
                  )}
                </div>

              </div>
            </div>
          </section>

          {/* Master Open Positions table */}
          <section className="qd-card overflow-hidden">
            <div className="border-b border-[var(--qd-border)] px-4 py-3 flex items-center justify-between">
              <h2 className="font-head text-sm font-semibold text-white">Positions Monitor</h2>
              <div className="flex gap-2">
                <StatusPill>{positions.length} Active Positions</StatusPill>
                {missingProtectionCount > 0 && <StatusPill tone="bad">{missingProtectionCount} Missing SL/TP</StatusPill>}
              </div>
            </div>
            {positions.length === 0 ? (
              <div className="p-5 text-center">
                <Target className="mx-auto mb-2 text-[var(--qd-text-3)]" size={20} />
                <div className="text-xs text-[var(--qd-text-2)]">No active trades currently open.</div>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[11px] uppercase text-[var(--qd-text-3)]">
                      <th className="px-4 py-3">Strategy</th>
                      <th className="px-4 py-3">Symbol</th>
                      <th className="px-4 py-3">Qty</th>
                      <th className="px-4 py-3">Avg Buy</th>
                      <th className="px-4 py-3">LTP</th>
                      <th className="px-4 py-3">Target</th>
                      <th className="px-4 py-3">Stop Loss</th>
                      <th className="px-4 py-3">Protection</th>
                      <th className="px-4 py-3">State</th>
                      <th className="px-4 py-3 text-right">PnL</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {positions.map((p) => (
                      <tr key={p.symbol} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                        <td className="px-4 py-3 text-[var(--qd-text-2)]">{p.strategy_name || p.strategy_id || "broker"}</td>
                        <td className="px-4 py-3 font-semibold text-white">{p.symbol}</td>
                        <td className="px-4 py-3 text-[var(--qd-text-2)]">{p.qty}</td>
                        <td className="px-4 py-3 text-[var(--qd-text-3)]">{money(p.avg_price)}</td>
                        <td className="px-4 py-3 text-[var(--qd-text-2)]">{money(p.ltp)}</td>
                        <td className="px-4 py-3 text-[var(--qd-profit)]">{p.take_profit ? money(p.take_profit) : "—"}</td>
                        <td className="px-4 py-3 text-[var(--qd-loss)]">{p.stop_loss ? money(p.stop_loss) : "—"}</td>
                        <td className="px-4 py-3">
                          <StatusPill tone={p.stop_loss != null && p.take_profit != null ? "good" : "bad"}>
                            {p.stop_loss != null && p.take_profit != null ? "Protected" : "Missing"}
                          </StatusPill>
                        </td>
                        <td className="px-4 py-3">
                          <StatusPill tone={statusTone(p.execution_status || p.ledger_status)}>{p.execution_status || p.ledger_status || "ACTIVE"}</StatusPill>
                        </td>
                        <td className={`px-4 py-3 text-right font-semibold ${toneClass(p.pnl)}`}>{money(p.pnl)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}

      {activeTab === "equity" && (
        <div className="space-y-3">
          <section>
            {/* Left Stock Positions */}
            <div className="qd-card overflow-hidden">
              <div className="border-b border-[var(--qd-border)] px-4 py-3">
                <h2 className="font-head text-sm font-semibold text-white">Equity Positions</h2>
              </div>
              {equityPositions.length === 0 ? (
                <div className="p-8 text-center text-xs text-[var(--qd-text-3)]">No open stock positions.</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[11px] uppercase text-[var(--qd-text-3)]">
                        <th className="px-3 py-2">Symbol</th>
                        <th className="px-3 py-2">Qty</th>
                        <th className="px-3 py-2">LTP</th>
                        <th className="px-3 py-2 text-right">PnL</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono">
                      {equityPositions.map((p) => (
                        <tr key={p.symbol} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                          <td className="px-3 py-2 text-white font-semibold">{p.symbol}</td>
                          <td className="px-3 py-2 text-[var(--qd-text-2)]">{p.qty}</td>
                          <td className="px-3 py-2 text-[var(--qd-text-3)]">{money(p.ltp)}</td>
                          <td className={`px-3 py-2 text-right ${toneClass(p.pnl)}`}>{money(p.pnl)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>

          {/* Equity Orders */}
          <section className="qd-card overflow-hidden">
            <div className="border-b border-[var(--qd-border)] px-4 py-3">
              <h2 className="font-head text-sm font-semibold text-white">Equity Orders (Today)</h2>
            </div>
            {equityOrders.length === 0 ? (
              <div className="p-8 text-center text-xs text-[var(--qd-text-3)]">No stock trades placed today.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[11px] uppercase text-[var(--qd-text-3)]">
                      <th className="px-3 py-2">Symbol</th>
                      <th className="px-3 py-2">Side</th>
                      <th className="px-3 py-2">Qty</th>
                      <th className="px-3 py-2">Price</th>
                      <th className="px-3 py-2">Status</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {equityOrders.map((o) => (
                      <tr key={o.id} className="border-b border-[var(--qd-border)]">
                        <td className="px-3 py-2 text-white">{o.symbol}</td>
                        <td className={`px-3 py-2 font-semibold ${o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{o.side}</td>
                        <td className="px-3 py-2 text-[var(--qd-text-2)]">{o.qty}</td>
                        <td className="px-3 py-2 text-[var(--qd-text-3)]">{money(o.price)}</td>
                        <td className="px-3 py-2"><StatusPill tone={filledOrder(o.status) ? "good" : "warn"}>{o.status}</StatusPill></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}

      {activeTab === "fo" && (
        <div className="space-y-3">
          <section className="grid grid-cols-1 gap-3 xl:grid-cols-[1.2fr_0.8fr]">
            {/* F&O strategies */}
            <div className="space-y-4">
              <div className="border-b border-[var(--qd-border)] pb-2 flex items-center justify-between">
                <h2 className="font-head text-sm font-semibold text-white">Derivatives Strategies</h2>
                <StatusPill>{foStrategies.length} registered</StatusPill>
              </div>
              <div className="space-y-3">
                {foStrategies.map((row) => (
                  <div key={row.strategy_id} className="qd-card p-4">
                    <div className="flex items-center justify-between">
                      <div className="font-head text-sm font-semibold text-white">{row.name}</div>
                      <StatusPill tone={row.state === "OPEN" ? "good" : "neutral"}>{row.state}</StatusPill>
                    </div>
                    <div className="mt-3 grid grid-cols-3 gap-2 border-t border-[var(--qd-border)] pt-2 font-mono text-[11px] text-[var(--qd-text-2)]">
                      <div>Target: {row.target_pct}%</div>
                      <div>Stoploss: {row.stoploss_pct}%</div>
                      <div>required cap: {money(row.required_capital)}</div>
                    </div>
                  </div>
                ))}
                {foStrategies.length === 0 && (
                  <div className="qd-card p-6 text-center text-xs text-[var(--qd-text-3)]">No derivative strategies enabled.</div>
                )}
              </div>
            </div>

            {/* Option chain preview / options positions */}
            <div className="space-y-4">
              <div className="qd-card overflow-hidden">
                <div className="border-b border-[var(--qd-border)] px-4 py-3 flex items-center justify-between">
                  <h2 className="font-head text-sm font-semibold text-white">Option Chain Monitor</h2>
                  <StatusPill tone={optionChain?.row_count ? "good" : "warn"}>{optionChain?.row_count || 0} Rows</StatusPill>
                </div>
                {optionChain?.data?.length ? (
                  <div className="overflow-x-auto border-b border-[var(--qd-border)]">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[11px] uppercase text-[var(--qd-text-3)]">
                          <th className="px-3 py-2">Strike</th>
                          <th className="px-3 py-2">Call</th>
                          <th className="px-3 py-2">Put</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {optionChain.data.slice(0, 8).map((row) => (
                          <tr key={`${row.strike_price}-${row.expiry}`} className="border-b border-[var(--qd-border)] last:border-0">
                            <td className="px-3 py-2 text-white">{row.strike_price || row.strike}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-2)]">{row.call_options?.market_data?.ltp ?? row.call_options?.ltp ?? "-"}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-2)]">{row.put_options?.market_data?.ltp ?? row.put_options?.ltp ?? "-"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <div className="border-b border-[var(--qd-border)] p-4 text-xs text-[var(--qd-text-3)]">Upstox option chain unavailable or OAuth disconnected.</div>
                )}
                {foPositions.length === 0 ? (
                  <div className="p-8 text-center text-xs text-[var(--qd-text-3)]">No open derivatives / options positions.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[11px] uppercase text-[var(--qd-text-3)]">
                          <th className="px-3 py-2">Symbol</th>
                          <th className="px-3 py-2">Qty</th>
                          <th className="px-3 py-2 text-right">PnL</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {foPositions.map((p) => (
                          <tr key={p.symbol} className="border-b border-[var(--qd-border)]">
                            <td className="px-3 py-2 text-white font-semibold">{p.symbol}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-2)]">{p.qty}</td>
                            <td className={`px-3 py-2 text-right ${toneClass(p.pnl)}`}>{money(p.pnl)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
