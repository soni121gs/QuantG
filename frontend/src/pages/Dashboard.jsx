import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  Layers,
  LineChart,
  PieChart,
  Play,
  Pause,
  Power,
  RefreshCw,
  Save,
  Settings,
  Shield,
  Target,
  TrendingDown,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-react";
import { api, formatINR, pctFmt } from "../lib/api";
import { useExecutionState } from "../hooks/useExecutionState";

const money = (value) => `INR ${formatINR(value ?? 0)}`;

const toneClass = (value) => ((value ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]");
const filledOrder = (status) => ["FILLED", "CLOSED", "COMPLETE"].includes((status || "").toUpperCase());

const Field = ({ label, value, tone }) => (
  <div className="min-w-0">
    <div className="qd-section-title text-[9px]">{label}</div>
    <div className={`mt-1 truncate font-mono text-sm font-semibold ${tone || "text-white"}`}>{value}</div>
  </div>
);

const KpiCard = ({ label, value, sub, icon: Icon, tone, testid }) => (
  <div className="qd-stat-panel p-4" data-testid={testid || `kpi-${label.replace(/\s+/g, "-").toLowerCase()}`}>
    <div className="flex items-center justify-between gap-3">
      <span className="qd-section-title">{label}</span>
      <Icon size={16} className="text-[var(--qd-text-3)]" strokeWidth={1.5} />
    </div>
    <div className={`mt-3 font-mono text-2xl font-bold tracking-tight ${tone || "text-white"}`}>{value}</div>
    {sub && <div className="mt-2 text-xs text-[var(--qd-text-2)]">{sub}</div>}
  </div>
);

const StatusPill = ({ children, tone = "neutral" }) => {
  const tones = {
    good: "border-[rgba(0,230,118,0.38)] bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]",
    bad: "border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.1)] text-[var(--qd-loss)]",
    warn: "border-[rgba(255,159,10,0.4)] bg-[rgba(255,159,10,0.1)] text-[var(--qd-warn)]",
    neutral: "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]",
  };
  return (
    <span className={`inline-flex items-center rounded border px-2 py-1 font-mono text-[10px] uppercase tracking-wider ${tones[tone]}`}>
      {children}
    </span>
  );
};

const RuntimeInput = ({ label, value, onChange, disabled }) => (
  <label className="space-y-1">
    <span className="qd-section-title text-[9px]">{label}</span>
    <input
      type="number"
      step="0.01"
      disabled={disabled}
      value={value}
      onChange={(e) => onChange?.(Number(e.target.value))}
      className="w-full rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-2 py-2 font-mono text-xs text-white disabled:opacity-60"
    />
  </label>
);

const EngineStrategyCard = ({ row, onSave }) => {
  const pos = row.active_position || {};
  const risk = row.risk_settings || {};
  const pnl = pos.unrealized_pnl ?? 0;
  const [form, setForm] = useState({
    target_pct: row.target_pct ?? 45,
    stoploss_pct: row.stoploss_pct ?? 22,
    trailing_sl_enabled: row.trailing_sl_enabled ?? true,
    trail_trigger_pct: row.trail_trigger_pct ?? 25,
    trail_step_pct: row.trail_step_pct ?? 10,
    cooldown_minutes: row.cooldown_minutes ?? 20,
    max_trades_day: row.max_trades_day ?? 2,
    daily_loss_limit: risk.daily_loss_limit ?? 0,
    required_capital: row.required_capital ?? 0,
    time_exit_minutes: row.time_exit_minutes ?? risk.time_exit_minutes ?? 45,
    indicator_exit_enabled: row.indicator_exit_enabled ?? risk.indicator_exit_enabled ?? true,
    exit_mode: row.exit_mode ?? risk.exit_mode ?? "tp_sl_tsl_or_signal",
  });

  const update = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));
  const stateTone = row.state === "OPEN" ? "good" : row.state === "DISABLED" ? "bad" : row.state === "COOLDOWN" ? "warn" : "neutral";

  return (
    <article className="qd-card p-4 my-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="qd-section-title">{row.strategy_id}</div>
          <h3 className="mt-1 truncate font-head text-lg font-semibold text-white">{row.name}</h3>
        </div>
        <StatusPill tone={stateTone}>{row.state || "Idle"}</StatusPill>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 border-y border-[var(--qd-border)] py-3 md:grid-cols-4">
        <Field label="Position" value={pos.symbol || "-"} />
        <Field label="Entry" value={pos.entry_price ? money(pos.entry_price) : "-"} />
        <Field label="LTP" value={pos.ltp ? money(pos.ltp) : "-"} />
        <Field label="Open P&L" value={money(pnl)} tone={toneClass(pnl)} />
        <Field label="Target" value={pos.target_price ? money(pos.target_price) : "-"} />
        <Field label="Stoploss" value={pos.stoploss_price ? money(pos.stoploss_price) : "-"} />
        <Field label="Trades" value={`${row.daily_pnl?.trades || 0}/${row.max_trades_day || 0}`} />
        <Field label="Re-entry" value={row.re_entry_allowed ? "Allowed" : "Blocked"} tone={row.re_entry_allowed ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"} />
      </div>

      <details className="mt-3">
        <summary className="flex cursor-pointer list-none items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-2)]">
          <Settings size={13} /> Runtime Settings
        </summary>
        <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">
          <RuntimeInput label="Target %" value={form.target_pct} onChange={(v) => update("target_pct", v)} />
          <RuntimeInput label="Stoploss %" value={form.stoploss_pct} onChange={(v) => update("stoploss_pct", v)} />
          <RuntimeInput label="Trail trigger" value={form.trail_trigger_pct} onChange={(v) => update("trail_trigger_pct", v)} />
          <RuntimeInput label="Trail step" value={form.trail_step_pct} onChange={(v) => update("trail_step_pct", v)} />
          <RuntimeInput label="Cooldown" value={form.cooldown_minutes} onChange={(v) => update("cooldown_minutes", v)} />
          <RuntimeInput label="Max trades" value={form.max_trades_day} onChange={(v) => update("max_trades_day", v)} />
          <RuntimeInput label="Daily loss" value={form.daily_loss_limit} onChange={(v) => update("daily_loss_limit", v)} />
          <RuntimeInput label="Time exit" value={form.time_exit_minutes} onChange={(v) => update("time_exit_minutes", v)} />
          <label className="col-span-2 space-y-1 md:col-span-2">
            <span className="qd-section-title text-[9px]">Exit mode</span>
            <select
              value={form.exit_mode}
              onChange={(e) => update("exit_mode", e.target.value)}
              className="w-full rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-2 py-2 font-mono text-xs text-white"
            >
              <option value="tp_sl_tsl_or_signal">TP/SL/Trail + Signal</option>
              <option value="tp_sl_tsl_only">TP/SL/Trail only</option>
              <option value="signal_only">Signal only</option>
            </select>
          </label>
          <label className="flex items-center gap-2 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-3 py-2 text-xs text-[var(--qd-text-2)]">
            <input type="checkbox" checked={form.trailing_sl_enabled} onChange={(e) => update("trailing_sl_enabled", e.target.checked)} />
            Trailing SL
          </label>
          <label className="flex items-center gap-2 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-3 py-2 text-xs text-[var(--qd-text-2)]">
            <input type="checkbox" checked={form.indicator_exit_enabled} onChange={(e) => update("indicator_exit_enabled", e.target.checked)} />
            Signal exit
          </label>
          <button
            type="button"
            onClick={() => onSave(row.strategy_id, form)}
            className="col-span-2 flex items-center justify-center gap-2 rounded bg-[var(--qd-accent)] px-3 py-2 font-mono text-xs font-semibold uppercase tracking-wider text-white hover:bg-[var(--qd-accent-hover)] md:col-span-4"
          >
            <Save size={13} /> Save Runtime Settings
          </button>
        </div>
      </details>
    </article>
  );
};

const MarketRow = ({ item }) => (
  <div className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3 hover:bg-[var(--qd-surface-2)]">
    <div className="min-w-0">
      <div className="font-mono text-sm font-semibold text-white">{item.symbol}</div>
      <div className="truncate text-xs text-[var(--qd-text-3)]">{item.name}</div>
    </div>
    <div className="text-right">
      <div className="font-mono text-sm font-bold text-white">{money(item.price)}</div>
      <div className={`font-mono text-xs font-semibold ${toneClass(item.change)}`}>
        {item.change >= 0 ? "UP" : "DN"} {pctFmt(item.pct)}
      </div>
    </div>
  </div>
);

export default function Dashboard() {
  const { positions: execPositions, orders: execOrders, refresh: refreshExecution } = useExecutionState({ pollMs: 15000 });
  const [pf, setPf] = useState(null);
  const [watch, setWatch] = useState([]);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [funds, setFunds] = useState(null);
  const [telemetry, setTelemetry] = useState(null);
  const [commodities, setCommodities] = useState([]);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [activeTab, setActiveTab] = useState("overview");

  const load = useCallback(async () => {
    try {
      const [p, w, f, t, c] = await Promise.all([
        api.get("/portfolio"),
        api.get("/market/watchlist"),
        api.get("/funds"),
        api.get("/v1/dashboard/telemetry"),
        api.get("/market/commodities"),
      ]);
      await refreshExecution();
      setPf(p.data);
      setWatch(w.data);
      setPositions(execPositions);
      setOrders(execOrders);
      setFunds(f.data);
      setTelemetry(t.data);
      setCommodities(c.data || []);
      setLoadError("");
    } catch (e) {
      setLoadError(e?.response?.data?.detail || e.message || "Dashboard data could not be loaded");
    }
  }, [execPositions, execOrders, refreshExecution]);

  useEffect(() => {
    setPositions(execPositions);
    setOrders(execOrders);
  }, [execPositions, execOrders]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  const pnl = pf?.total_pnl ?? 0;
  const openPositions = pf?.open_positions ?? positions.length;
  const strategies = useMemo(() => telemetry?.strategies_page_data || [], [telemetry?.strategies_page_data]);
  const marketOpen = telemetry?.market_status?.is_open;
  const firstRisk = strategies[0]?.risk_settings || {};
  const topWatch = useMemo(() => watch.slice(0, 8), [watch]);

  // Tab Filtering
  const equityPositions = useMemo(() => positions.filter((p) => p.asset_type === "equity" || p.exchange === "NSE"), [positions]);
  const equityOrders = useMemo(() => orders.filter((o) => o.exchange === "NSE"), [orders]);
  
  const commodityPositions = useMemo(() => positions.filter((p) => p.exchange === "MCX" || p.asset_type === "commodity" || p.symbol.includes("CRUDE") || p.symbol.includes("NATURAL")), [positions]);
  const commodityOrders = useMemo(() => orders.filter((o) => o.exchange === "MCX" || o.asset_type === "commodity" || o.symbol.includes("CRUDE") || o.symbol.includes("NATURAL")), [orders]);

  const foPositions = useMemo(() => positions.filter((p) => p.option_type || p.symbol.endsWith("CE") || p.symbol.endsWith("PE") || p.exchange === "NFO" || p.exchange === "BFO"), [positions]);
  const foStrategies = useMemo(() => strategies.filter((s) => s.asset_class === "option" || s.strategy_id.includes("Straddle") || s.strategy_id.includes("Scalper") || s.name.includes("Option")), [strategies]);

  const strategySummary = useMemo(() => {
    const counts = strategies.reduce((acc, row) => {
      const key = row.state || "IDLE";
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
    return [
      { label: "Open", value: counts.OPEN || 0, tone: "good" },
      { label: "Scanning", value: counts.SCANNING || 0, tone: "good" },
      { label: "Cooldown", value: counts.COOLDOWN || 0, tone: "warn" },
      { label: "Disabled", value: counts.DISABLED || 0, tone: "bad" },
      { label: "Idle", value: counts.IDLE || counts.READY || 0, tone: "neutral" },
    ];
  }, [strategies]);

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

  const saveRuntimeSettings = async (strategyId, form) => {
    await api.put(`/strategies/${strategyId}/runtime-settings`, form);
    await load();
  };

  const toggleStrategy = async (id) => {
    await api.post(`/strategies/${id}/toggle`);
    await load();
  };

  const exitStrategy = async (id) => {
    if (!window.confirm("Emergency Square Off: exit all open positions for this strategy?")) return;
    await api.post(`/strategies/${id}/exit-all`);
    await load();
  };

  return (
    <div className="space-y-5" data-testid="dashboard-page">
      {/* Top Header Card */}
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.5fr_0.9fr]">
        <div className="qd-card overflow-hidden">
          <div className="border-b border-[var(--qd-border)] p-5">
            <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
              <div>
                <div className="qd-section-title">// Control Room</div>
                <h1 className="mt-2 font-head text-3xl font-bold tracking-tight text-white">Dashboard</h1>
                <p className="mt-2 max-w-2xl text-sm text-[var(--qd-text-2)]">
                  Live portfolio state, market telemetry, and custom-tailored multi-asset controls.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={handleRefresh}
                  disabled={refreshing}
                  className="flex items-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-white disabled:opacity-50"
                  title="Refresh data"
                >
                  <RefreshCw size={15} className={refreshing ? "animate-spin" : ""} /> Refresh
                </button>
                <Link
                  to="/ai-bot"
                  className="flex items-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-white"
                >
                  <Bot size={15} /> AI Bot
                </Link>
                <Link
                  to="/strategies"
                  className="flex items-center gap-2 rounded bg-[var(--qd-accent)] px-3 py-2 font-mono text-xs font-semibold uppercase tracking-wider text-white hover:bg-[var(--qd-accent-hover)]"
                  data-testid="new-strategy-btn"
                >
                  <Zap size={15} /> Strategy
                </Link>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-px bg-[var(--qd-border)] md:grid-cols-4">
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Market Status" value={marketOpen ? "OPEN" : "CLOSED"} tone={marketOpen ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"} />
            </div>
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Net Unrealized P&L" value={money(pnl)} tone={toneClass(pnl)} />
            </div>
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Active Automations" value={`${pf?.live_strategies ?? 0}/${pf?.strategies ?? 0} systems`} />
            </div>
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Total Positions" value={`${openPositions} active`} />
            </div>
          </div>
        </div>

        {/* Emergency Stop card */}
        <div className="qd-card border-l-2 border-l-[var(--qd-loss)] p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="qd-section-title">Risk Control</div>
              <h2 className="mt-2 font-head text-xl font-semibold text-white">Emergency Stop</h2>
              <p className="mt-2 text-sm text-[var(--qd-text-2)]">
                Flipping this immediately halts all active strategy loops, switches executing accounts to PAPER, and locks safety gates.
              </p>
            </div>
            <Shield size={20} className="text-[var(--qd-loss)]" />
          </div>
          <div className="mt-5 grid grid-cols-2 gap-3">
            <Field label="Max lot limit" value="1 contract" />
            <Field label="Cooldown delay" value={`${strategies[0]?.cooldown_minutes ?? 20} min`} />
            <Field label="Loss cutoff" value={money(firstRisk.daily_loss_limit || 0)} />
            <Field label="Kill gate" value={firstRisk.kill_switch_enabled ? "ARMED" : "CLEAR"} tone={firstRisk.kill_switch_enabled ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
          </div>
          <button
            type="button"
            onClick={killSwitch}
            className="mt-5 flex w-full items-center justify-center gap-2 rounded bg-[var(--qd-loss)] px-4 py-3 font-mono text-xs font-bold uppercase tracking-wider text-white hover:opacity-90"
          >
            <Power size={15} /> Trigger Kill Switch
          </button>
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
      <div className="flex border-b border-[var(--qd-border)] gap-2 overflow-x-auto pb-px">
        {[
          { id: "overview", label: "General Console" },
          { id: "equity", label: "Equity Spot" },
          { id: "commodities", label: "MCX Commodities" },
          { id: "fo", label: "Derivatives (F&O)" },
        ].map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setActiveTab(t.id)}
            className={`px-4 py-2.5 font-head font-semibold text-xs transition-all border-b-2 border-transparent uppercase tracking-widest whitespace-nowrap ${
              activeTab === t.id
                ? "text-white border-[var(--qd-cyan)] qd-tab-active"
                : "text-[var(--qd-text-3)] hover:text-white"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Panel Render Logic */}
      {activeTab === "overview" && (
        <div className="space-y-5">
          {/* Main summary grid */}
          <section className="grid grid-cols-2 gap-4 xl:grid-cols-4">
            <KpiCard label="Account Balance" value={money(funds?.available_cash)} icon={Wallet} sub={funds?.source === "live" ? "Live Account Balance" : "Simulated Paper Cash"} />
            <KpiCard label="Utilized Margin" value={money(funds?.used_margin)} icon={Layers} sub={funds?.source === "live" ? "Live Blocked Margin" : "Paper Blocked Margin"} />
            <KpiCard label="Open Profit" value={money(pnl)} icon={pnl >= 0 ? TrendingUp : TrendingDown} tone={toneClass(pnl)} sub={`${openPositions} Active positions`} />
            <KpiCard label="Subsystem Health" value={`${pf?.active_strategies ?? 0}/${pf?.strategies ?? 0}`} icon={Activity} sub={`${pf?.paused_strategies ?? 0} paused engines`} />
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
            {/* Strategy summaries and engines */}
            <div className="space-y-4">
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3">
                <div>
                  <div className="qd-section-title">// Runtime ledger states</div>
                  <h2 className="mt-1 font-head text-xl font-semibold text-white">Active System Blocks</h2>
                </div>
                <Link to="/strategies" className="font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:text-white">
                  Manage strategies
                </Link>
              </div>
              <div className="qd-card p-4">
                <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                  {strategySummary.map((item) => (
                    <div key={item.label} className="rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-3">
                      <div className="qd-section-title">{item.label}</div>
                      <div className="mt-2 font-mono text-2xl font-bold text-white">{item.value}</div>
                    </div>
                  ))}
                </div>
                
                {/* Active strategy logs */}
                <div className="mt-4 divide-y divide-[var(--qd-border)]">
                  {strategies.filter((s) => s.state === "OPEN").map((row) => (
                    <EngineStrategyCard key={row.strategy_id} row={row} onSave={saveRuntimeSettings} />
                  ))}
                  {strategies.slice(0, 5).map((row) => {
                    const pos = row.active_position || {};
                    const spnl = pos.unrealized_pnl ?? row.daily_pnl?.realised_pnl ?? 0;
                    const live = row.status === "live";
                    return (
                      <div key={row.strategy_id} className="grid grid-cols-[1fr_auto_auto] items-center gap-3 py-3 hover:bg-[var(--qd-surface)]/20 px-2 rounded">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-semibold text-white">{row.name}</div>
                          <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">
                            {pos.symbol || "No open trades"}
                            {pos.target_price ? ` · TP ${money(pos.target_price)}` : ""}
                            {pos.stoploss_price ? ` · SL ${money(pos.stoploss_price)}` : ""}
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <StatusPill tone={row.state === "OPEN" ? "good" : row.state === "COOLDOWN" ? "warn" : row.state === "DISABLED" ? "bad" : "neutral"}>
                            {row.state || "Idle"}
                          </StatusPill>
                          
                          {/* Live Running controls */}
                          <button
                            onClick={() => toggleStrategy(row.strategy_id)}
                            className={`flex items-center justify-center p-1.5 rounded border transition-all ${
                              live 
                                ? "border-[rgba(255,159,10,0.4)] text-[var(--qd-warn)] hover:bg-[rgba(255,159,10,0.1)]" 
                                : "border-[rgba(0,230,118,0.4)] text-[var(--qd-profit)] hover:bg-[rgba(0,230,118,0.1)]"
                            }`}
                            title={live ? "Pause Strategy" : "Go Live / Resume Strategy"}
                            data-testid={`dashboard-toggle-${row.strategy_id}`}
                          >
                            {live ? <Pause size={12} /> : <Play size={12} />}
                          </button>

                          {/* Emergency Square Off */}
                          <button
                            onClick={() => exitStrategy(row.strategy_id)}
                            className="flex items-center justify-center p-1.5 rounded border border-[var(--qd-warn)] text-[var(--qd-warn)] hover:bg-[var(--qd-warn)] hover:text-black transition-all"
                            title="Square Off Strategy Positions"
                            data-testid={`dashboard-exit-${row.strategy_id}`}
                          >
                            <Shield size={12} />
                          </button>
                        </div>
                        <div className={`min-w-[80px] text-right font-mono text-xs font-semibold ${toneClass(spnl)}`}>
                          {money(spnl)}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* Sidebar columns */}
            <aside className="space-y-4">
              {/* Watchlist */}
              <div className="qd-card overflow-hidden">
                <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
                  <h2 className="flex items-center gap-2 font-head text-sm font-semibold text-white">
                    <BarChart3 size={15} /> Primary Watchlist
                  </h2>
                  <span className="qd-live-dot" />
                </div>
                <div className="max-h-[300px] divide-y divide-[var(--qd-border)] overflow-auto">
                  {topWatch.map((item) => <MarketRow key={item.symbol} item={item} />)}
                </div>
              </div>

              {/* Telemetry */}
              <div className="qd-card p-4">
                <h2 className="flex items-center gap-2 font-head text-sm font-semibold text-white mb-3">
                  <LineChart size={15} /> Live Feeds
                </h2>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="NIFTY Index" value={telemetry?.market_status?.nifty?.ltp ? money(telemetry.market_status.nifty.ltp) : "Waiting"} />
                  <Field label="SENSEX Index" value={telemetry?.market_status?.sensex?.ltp ? money(telemetry.market_status.sensex.ltp) : "Waiting"} />
                  <Field label="Last Tick Time" value={telemetry?.market_status?.last_tick_time ? new Date(telemetry.market_status.last_tick_time).toLocaleTimeString() : "-"} />
                  <Field label="Telemetry Source" value={telemetry?.market_status?.data_source || "Upstox Feed"} />
                </div>
              </div>
            </aside>
          </section>

          {/* Master Open Positions table */}
          <section className="qd-card overflow-hidden">
            <div className="border-b border-[var(--qd-border)] px-4 py-3 flex items-center justify-between">
              <h2 className="font-head text-sm font-semibold text-white">Positions Monitor</h2>
              <StatusPill>{positions.length} Active Positions</StatusPill>
            </div>
            {positions.length === 0 ? (
              <div className="p-10 text-center">
                <Target className="mx-auto mb-2 text-[var(--qd-text-3)]" size={20} />
                <div className="text-xs text-[var(--qd-text-2)]">No active trades currently open.</div>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">
                      <th className="px-4 py-3">Symbol</th>
                      <th className="px-4 py-3">Qty</th>
                      <th className="px-4 py-3">Avg Buy</th>
                      <th className="px-4 py-3">LTP</th>
                      <th className="px-4 py-3">Target</th>
                      <th className="px-4 py-3">Stop Loss</th>
                      <th className="px-4 py-3">State</th>
                      <th className="px-4 py-3 text-right">PnL</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {positions.map((p) => (
                      <tr key={p.symbol} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                        <td className="px-4 py-3 font-semibold text-white">{p.symbol}</td>
                        <td className="px-4 py-3 text-[var(--qd-text-2)]">{p.qty}</td>
                        <td className="px-4 py-3 text-[var(--qd-text-3)]">{money(p.avg_price)}</td>
                        <td className="px-4 py-3 text-[var(--qd-text-2)]">{money(p.ltp)}</td>
                        <td className="px-4 py-3 text-[var(--qd-profit)]">{p.take_profit ? money(p.take_profit) : "—"}</td>
                        <td className="px-4 py-3 text-[var(--qd-loss)]">{p.stop_loss ? money(p.stop_loss) : "—"}</td>
                        <td className="px-4 py-3 text-[var(--qd-warn)] uppercase text-[10px]">{p.execution_status || "ACTIVE"}</td>
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
        <div className="space-y-5">
          <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
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
                      <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">
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

            {/* Right stock watch */}
            <div className="qd-card overflow-hidden">
              <div className="border-b border-[var(--qd-border)] px-4 py-3 flex items-center justify-between">
                <h2 className="font-head text-sm font-semibold text-white">Stock Watchlist</h2>
                <span className="qd-live-dot" />
              </div>
              <div className="max-h-[300px] divide-y divide-[var(--qd-border)] overflow-auto">
                {topWatch.filter(w => !w.symbol.includes("CRUDE") && !w.symbol.includes("NATURAL")).map((item) => (
                  <MarketRow key={item.symbol} item={item} />
                ))}
              </div>
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
                    <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">
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
                        <td className={`px-3 py-2 font-bold ${o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{o.side}</td>
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

      {activeTab === "commodities" && (
        <div className="space-y-5">
          {/* Commodity metrics */}
          <section className="grid grid-cols-2 gap-4 xl:grid-cols-4">
            <KpiCard label="Commodity Feed" value="UPSTOX LIVE" icon={Activity} sub="Realtime High-Frequency Feed" />
            <KpiCard label="MCX Positions" value={commodityPositions.length} icon={PieChart} sub="Active MCX Contracts" />
            <KpiCard label="MCX Orders" value={commodityOrders.length} icon={Power} sub="MCX Orders Filled Today" />
            <KpiCard label="MCX Status" value="Market Open" icon={Shield} tone="text-[var(--qd-profit)]" sub="Session trades 09:00 - 23:30" />
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.1fr_0.9fr]">
            {/* Commodity Positions & Orders */}
            <div className="space-y-4">
              <div className="qd-card overflow-hidden">
                <div className="border-b border-[var(--qd-border)] px-4 py-3 flex items-center justify-between">
                  <h2 className="font-head text-sm font-semibold text-white">Commodity Open Positions</h2>
                  <StatusPill>{commodityPositions.length} active</StatusPill>
                </div>
                {commodityPositions.length === 0 ? (
                  <div className="p-8 text-center text-xs text-[var(--qd-text-3)]">No open MCX commodity positions.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">
                          <th className="px-3 py-2">Symbol</th>
                          <th className="px-3 py-2">Qty</th>
                          <th className="px-3 py-2">Avg Buy</th>
                          <th className="px-3 py-2">LTP</th>
                          <th className="px-3 py-2 text-right">PnL</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {commodityPositions.map((p) => (
                          <tr key={p.symbol} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                            <td className="px-3 py-2 text-white font-semibold">{p.symbol}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-2)]">{p.qty}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-3)]">{money(p.avg_price)}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-2)]">{money(p.ltp)}</td>
                            <td className={`px-3 py-2 text-right font-semibold ${toneClass(p.pnl)}`}>{money(p.pnl)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* Commodity Orders */}
              <div className="qd-card overflow-hidden">
                <div className="border-b border-[var(--qd-border)] px-4 py-3">
                  <h2 className="font-head text-sm font-semibold text-white">Commodity Orders History</h2>
                </div>
                {commodityOrders.length === 0 ? (
                  <div className="p-8 text-center text-xs text-[var(--qd-text-3)]">No MCX commodity trades submitted today.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">
                          <th className="px-3 py-2">Symbol</th>
                          <th className="px-3 py-2">Side</th>
                          <th className="px-3 py-2">Qty</th>
                          <th className="px-3 py-2">Fill Price</th>
                          <th className="px-3 py-2">Status</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {commodityOrders.map((o) => (
                          <tr key={o.id} className="border-b border-[var(--qd-border)]">
                            <td className="px-3 py-2 text-white">{o.symbol}</td>
                            <td className={`px-3 py-2 font-bold ${o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{o.side}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-2)]">{o.qty}</td>
                            <td className="px-3 py-2 text-[var(--qd-text-3)]">{money(o.price)}</td>
                            <td className="px-3 py-2"><StatusPill tone={filledOrder(o.status) ? "good" : "warn"}>{o.status}</StatusPill></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>

            {/* Commodity live quote feeds */}
            <aside className="space-y-4">
              <div className="qd-card overflow-hidden">
                <div className="border-b border-[var(--qd-border)] px-4 py-3 flex items-center justify-between">
                  <h2 className="font-head text-sm font-semibold text-white">MCX Live Quotes</h2>
                  <span className="qd-live-dot" />
                </div>
                <div className="divide-y divide-[var(--qd-border)]">
                  {commodities.map((item) => (
                    <div key={item.symbol} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3 hover:bg-[var(--qd-surface-2)]">
                      <div className="min-w-0">
                        <div className="font-mono text-sm font-semibold text-white">{item.symbol}</div>
                        <div className="mt-0.5 truncate text-xs text-[var(--qd-text-3)]">{item.name}</div>
                      </div>
                      <div className="text-right">
                        <div className="font-mono text-sm font-bold text-white">{money(item.price)}</div>
                        <div className={`font-mono text-xs font-semibold ${toneClass(item.change)}`}>
                          {item.change >= 0 ? "UP" : "DN"} {pctFmt(item.pct)}
                        </div>
                      </div>
                    </div>
                  ))}
                  {commodities.length === 0 && (
                    <div className="p-8 text-center text-xs text-[var(--qd-text-3)]">Waiting for MCX tickers.</div>
                  )}
                </div>
              </div>

              {/* Seed instructions */}
              <div className="qd-card p-4 bg-gradient-to-br from-indigo-500/5 to-cyan-500/5">
                <h3 className="font-head text-sm font-semibold text-white flex items-center gap-2 mb-2">
                  <Zap size={14} className="text-[var(--qd-cyan)]" /> Low Cost Option Scalping
                </h3>
                <p className="text-xs text-[var(--qd-text-2)] leading-relaxed">
                  We have successfully integrated <strong>Crude Oil Mini options (CRUDEOILM)</strong>. By deploying the new <strong>Crude Oil Mini EMA Momentum</strong> or <strong>RSI Reversion</strong> strategies, you can trade live commodity options on Upstox with extremely low margins (lot size of 10 instead of 100 on standard crude).
                </p>
                <div className="mt-3 text-[10px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider">
                  Lot Size: 10 barrels · Margin required: ~₹5,000
                </div>
              </div>
            </aside>
          </section>
        </div>
      )}

      {activeTab === "fo" && (
        <div className="space-y-5">
          <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.2fr_0.8fr]">
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
                      <div className="font-head text-sm font-bold text-white">{row.name}</div>
                      <StatusPill tone={row.state === "OPEN" ? "good" : "neutral"}>{row.state}</StatusPill>
                    </div>
                    <div className="mt-3 grid grid-cols-3 gap-2 border-t border-[var(--qd-border)] pt-2 font-mono text-[10px] text-[var(--qd-text-2)]">
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
                  <span className="qd-live-dot" />
                </div>
                {foPositions.length === 0 ? (
                  <div className="p-8 text-center text-xs text-[var(--qd-text-3)]">No open derivatives / options positions.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">
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
