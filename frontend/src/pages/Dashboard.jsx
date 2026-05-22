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
    <article className="qd-card p-4">
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
  const { positions: execPositions, orders: execOrders, refresh: refreshExecution } = useExecutionState({ pollMs: 4000 });
  const [pf, setPf] = useState(null);
  const [watch, setWatch] = useState([]);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [funds, setFunds] = useState(null);
  const [telemetry, setTelemetry] = useState(null);
  const [commodities, setCommodities] = useState([]);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");

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
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);

  const pnl = pf?.total_pnl ?? 0;
  const openPositions = pf?.open_positions ?? positions.length;
  const strategies = useMemo(() => telemetry?.strategies_page_data || [], [telemetry?.strategies_page_data]);
  const marketOpen = telemetry?.market_status?.is_open;
  const firstRisk = strategies[0]?.risk_settings || {};
  const topWatch = useMemo(() => watch.slice(0, 8), [watch]);
  const commodityOrders = useMemo(
    () => orders.filter((o) => o.exchange === "MCX" || o.asset_type === "commodity").slice(0, 6),
    [orders],
  );
  const commodityPositions = useMemo(
    () => positions.filter((p) => p.exchange === "MCX" || p.asset_type === "commodity"),
    [positions],
  );
  const strategySummary = useMemo(() => {
    const counts = strategies.reduce((acc, row) => {
      const key = row.state || "IDLE";
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
    return [
      { label: "Open", value: counts.OPEN || 0, tone: "good" },
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

  return (
    <div className="space-y-5" data-testid="dashboard-page">
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.5fr_0.9fr]">
        <div className="qd-card overflow-hidden">
          <div className="border-b border-[var(--qd-border)] p-5">
            <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
              <div>
                <div className="qd-section-title">// Control Room</div>
                <h1 className="mt-2 font-head text-3xl font-bold tracking-tight text-white">Dashboard</h1>
                <p className="mt-2 max-w-2xl text-sm text-[var(--qd-text-2)]">
                  Live portfolio state, market telemetry, and runtime strategy controls in one scan.
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
              <Field label="Market" value={marketOpen ? "Open" : "Closed"} tone={marketOpen ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"} />
            </div>
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Open P&L" value={money(pnl)} tone={toneClass(pnl)} />
            </div>
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Strategies" value={`${pf?.live_strategies ?? 0}/${pf?.strategies ?? 0} live`} />
            </div>
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Positions" value={`${openPositions} open`} />
            </div>
          </div>
        </div>

        <div className="qd-card border-l-2 border-l-[var(--qd-loss)] p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="qd-section-title">Risk Control</div>
              <h2 className="mt-2 font-head text-xl font-semibold text-white">Emergency Stop</h2>
              <p className="mt-2 text-sm text-[var(--qd-text-2)]">
                Pauses live strategies, switches to paper mode, and blocks re-entry gates.
              </p>
            </div>
            <Shield size={20} className="text-[var(--qd-loss)]" />
          </div>
          <div className="mt-5 grid grid-cols-2 gap-3">
            <Field label="Max lot" value="1 locked" />
            <Field label="Cooldown" value={`${strategies[0]?.cooldown_minutes ?? 20} min`} />
            <Field label="Daily loss" value={money(firstRisk.daily_loss_limit || 0)} />
            <Field label="Kill gate" value={firstRisk.kill_switch_enabled ? "Armed" : "Clear"} tone={firstRisk.kill_switch_enabled ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
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

      <section className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <KpiCard label="Open P&L" value={money(pnl)} icon={pnl >= 0 ? TrendingUp : TrendingDown} tone={toneClass(pnl)} sub={openPositions ? `${openPositions} marked positions` : "No open positions"} testid="kpi-total-pnl" />
        <KpiCard label="Available Cash" value={money(funds?.available_cash)} icon={Wallet} sub={funds?.source === "live" ? "From Zerodha" : "Paper funds"} />
        <KpiCard label="Used Margin" value={money(funds?.used_margin)} icon={Layers} sub={funds?.source === "live" ? "Live broker margin" : `Open: ${money(funds?.opening_balance)}`} />
        <KpiCard label="Active Systems" value={`${pf?.active_strategies ?? 0}/${pf?.strategies ?? 0}`} icon={Activity} sub={`${pf?.paused_strategies ?? 0} paused`} />
      </section>

      <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <div className="space-y-4">
          <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3">
            <div>
              <div className="qd-section-title">// SQLite Option Engine</div>
              <h2 className="mt-1 font-head text-xl font-semibold text-white">Strategy Health</h2>
            </div>
            <Link to="/strategies" className="font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:text-white">
              Manage strategies
            </Link>
          </div>
          <div className="qd-card p-4">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {strategySummary.map((item) => (
                <div key={item.label} className="rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-3">
                  <div className="qd-section-title">{item.label}</div>
                  <div className="mt-2 font-mono text-2xl font-bold text-white">{item.value}</div>
                </div>
              ))}
            </div>
            <div className="mt-4 divide-y divide-[var(--qd-border)]">
              {strategies.filter((row) => row.active_position?.symbol).slice(0, 3).map((row) => (
                <EngineStrategyCard key={row.strategy_id} row={row} onSave={saveRuntimeSettings} />
              ))}
            </div>
            <div className="mt-2 divide-y divide-[var(--qd-border)]">
              {strategies.slice(0, 5).map((row) => {
                const pos = row.active_position || {};
                const pnl = pos.unrealized_pnl ?? row.daily_pnl?.realised_pnl ?? 0;
                return (
                  <div key={row.strategy_id} className="grid grid-cols-[1fr_auto_auto] items-center gap-3 py-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-semibold text-white">{row.name}</div>
                      <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">
                        {pos.symbol || "No active position"}
                        {pos.target_price ? ` · TP ${money(pos.target_price)}` : ""}
                        {pos.stoploss_price ? ` · SL ${money(pos.stoploss_price)}` : ""}
                      </div>
                    </div>
                    <StatusPill tone={row.state === "OPEN" ? "good" : row.state === "COOLDOWN" ? "warn" : row.state === "DISABLED" ? "bad" : "neutral"}>
                      {row.state || "Idle"}
                    </StatusPill>
                    <div className={`hidden min-w-[110px] text-right font-mono text-sm font-semibold sm:block ${toneClass(pnl)}`}>
                      {money(pnl)}
                    </div>
                  </div>
                );
              })}
              {!strategies.length && (
                <div className="py-8 text-center text-sm text-[var(--qd-text-2)]">
                  No strategies are registered in the runtime ledger yet.
                </div>
              )}
            </div>
            {strategies.length > 5 && (
              <div className="mt-3 border-t border-[var(--qd-border)] pt-3 text-right">
                <Link to="/strategies" className="font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:text-white">
                  View all {strategies.length}
                </Link>
              </div>
            )}
          </div>
        </div>

        <aside className="space-y-4">
          <div className="qd-card overflow-hidden">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
              <h2 className="flex items-center gap-2 font-head text-base font-semibold text-white">
                <Layers size={16} /> Commodities
              </h2>
              <Link to="/orders" className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-accent)] hover:text-white">
                Trade MCX
              </Link>
            </div>
            <div className="grid grid-cols-3 gap-px bg-[var(--qd-border)]">
              <div className="bg-[var(--qd-surface)] p-3">
                <Field label="Feed" value={commodities[0]?.source === "kotak_neo" ? "Kotak live" : "Mock"} />
              </div>
              <div className="bg-[var(--qd-surface)] p-3">
                <Field label="Positions" value={commodityPositions.length} />
              </div>
              <div className="bg-[var(--qd-surface)] p-3">
                <Field label="Orders" value={commodityOrders.length} />
              </div>
            </div>
            <div className="divide-y divide-[var(--qd-border)]">
              {commodities.map((item) => (
                <div key={item.symbol} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3">
                  <div className="min-w-0">
                    <div className="font-mono text-sm font-semibold text-white">{item.symbol}</div>
                    <div className="mt-1 truncate text-xs text-[var(--qd-text-3)]">{item.name}</div>
                  </div>
                  <div className="text-right">
                    <div className="font-mono text-sm font-bold text-white">{money(item.price)}</div>
                    <div className={`font-mono text-xs font-semibold ${toneClass(item.change)}`}>
                      {pctFmt(item.pct)}
                    </div>
                  </div>
                </div>
              ))}
              {commodityOrders.length ? commodityOrders.map((order) => (
                <div key={order.id} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3">
                  <div className="min-w-0">
                    <div className="truncate font-mono text-sm font-semibold text-white">{order.symbol}</div>
                    <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">
                      {order.exchange || "MCX"} / {order.product || "NRML"} / {order.status}
                    </div>
                  </div>
                  <div className={order.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>
                    {order.side} {order.qty}
                  </div>
                </div>
              )) : (
                <div className="p-5 text-sm text-[var(--qd-text-2)]">
                  No commodity orders yet. Use exchange MCX with exact Kotak trading symbols when placing orders.
                </div>
              )}
            </div>
          </div>

          <div className="qd-card overflow-hidden" data-testid="watchlist-card">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
              <h2 className="flex items-center gap-2 font-head text-base font-semibold text-white">
                <BarChart3 size={16} /> Market Watch
              </h2>
              <StatusPill tone={["real", "live", "kotak_neo"].includes(watch[0]?.source) ? "good" : watch[0]?.source === "kotak_pending" ? "warn" : "neutral"}>
                {watch[0]?.source === "real" ? "Real" : watch[0]?.source === "live" ? "Rest" : watch[0]?.source === "kotak_neo" ? "Kotak" : watch[0]?.source === "kotak_pending" ? "Kotak pending" : "Mock"}
              </StatusPill>
            </div>
            <div className="max-h-[420px] divide-y divide-[var(--qd-border)] overflow-auto">
              {topWatch.length ? topWatch.map((item) => <MarketRow key={item.symbol} item={item} />) : (
                <div className="p-6 text-center text-sm text-[var(--qd-text-2)]">Waiting for market symbols.</div>
              )}
            </div>
          </div>

          <div className="qd-card p-4">
            <div className="flex items-center justify-between">
              <h2 className="flex items-center gap-2 font-head text-base font-semibold text-white">
                <LineChart size={16} /> Live Telemetry
              </h2>
              <span className="qd-live-dot" />
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <Field label="NIFTY" value={telemetry?.market_status?.nifty?.ltp ? money(telemetry.market_status.nifty.ltp) : "Waiting"} />
              <Field label="SENSEX" value={telemetry?.market_status?.sensex?.ltp ? money(telemetry.market_status.sensex.ltp) : "Waiting"} />
              <Field label="Last tick" value={telemetry?.market_status?.last_tick_time ? new Date(telemetry.market_status.last_tick_time).toLocaleTimeString() : "-"} />
              <Field label="Source" value={telemetry?.market_status?.data_source || watch[0]?.source || "-"} />
            </div>
          </div>
        </aside>
      </section>

      <section className="qd-card overflow-hidden" data-testid="positions-card">
        <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
          <h2 className="flex items-center gap-2 font-head text-base font-semibold text-white">
            <PieChart size={16} /> Open Positions
          </h2>
          <StatusPill>{positions.length} active</StatusPill>
        </div>
        {positions.length === 0 ? (
          <div className="p-10 text-center">
            <Target className="mx-auto mb-3 text-[var(--qd-text-3)] opacity-60" size={24} />
            <p className="text-sm text-[var(--qd-text-2)]">No open positions. Open P&L stays INR 0.00.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">
                  <th className="px-4 py-3">Symbol</th>
                  <th className="px-4 py-3">Qty</th>
                  <th className="px-4 py-3">Avg</th>
                  <th className="px-4 py-3">LTP</th>
                  <th className="px-4 py-3">Target</th>
                  <th className="px-4 py-3">Stop</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3 text-right">P&L</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {positions.filter((p) => p.qty).map((p) => (
                  <tr key={`${p.symbol}-${p.strategy_id || ""}`} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                    <td className="px-4 py-3 font-semibold text-white">{p.symbol}</td>
                    <td className="px-4 py-3 text-[var(--qd-text-2)]">{p.qty}</td>
                    <td className="px-4 py-3 text-[var(--qd-text-2)]">{money(p.avg_price)}</td>
                    <td className="px-4 py-3 text-[var(--qd-text-2)]">{money(p.ltp)}</td>
                    <td className="px-4 py-3 text-[var(--qd-profit)]">{p.take_profit != null ? money(p.take_profit) : "—"}</td>
                    <td className="px-4 py-3 text-[var(--qd-loss)]">{p.stop_loss != null ? money(p.stop_loss) : "—"}</td>
                    <td className="px-4 py-3 text-[10px] uppercase text-[var(--qd-warn)]">{p.execution_status || p.ledger_status || "—"}</td>
                    <td className={`px-4 py-3 text-right font-semibold ${toneClass(p.pnl)}`}>{money(p.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
