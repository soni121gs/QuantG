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
const ACTIVE_POSITION_STATES = ["OPEN", "FILLED"];
const PENDING_POSITION_STATES = ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "EXITING"];
const BROKER_OPEN_ORDER_STATES = ["NEW", "PLACED", "OPEN", "PARTIAL_FILL", "PENDING", "PENDING_BROKER", "TRIGGER PENDING", "MODIFY PENDING", "VALIDATION PENDING", "EXIT_PENDING"];
const PROBLEM_ORDER_STATES = ["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE"];

const asStatus = (value) => String(value || "").toUpperCase();
const hasQty = (value) => Math.abs(parseInt(value || 0, 10)) > 0;

const statusTone = (status) => {
  const s = asStatus(status);
  if (ACTIVE_POSITION_STATES.includes(s)) return "good";
  if (PENDING_POSITION_STATES.includes(s)) return "warn";
  if (["FAILED", "REJECTED", "BROKER_NOT_FOUND", "ORPHAN", "STALE"].includes(s)) return "bad";
  return "neutral";
};

const shortId = (value) => {
  const text = String(value || "");
  return text.length > 10 ? `${text.slice(0, 6)}...${text.slice(-4)}` : text;
};

const quoteTime = (value) => {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString();
};

const quoteAge = (value) => (value == null ? "-" : `${Math.round(Number(value) || 0)}s`);

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

const MarketPulseGraphic = ({ pnl = 0, marketOpen, label }) => {
  const positive = Number(pnl || 0) >= 0;
  const path = positive
    ? "M6 96 C38 74 48 82 74 58 S124 24 160 38 S214 82 258 42 S314 18 354 30"
    : "M6 34 C38 54 50 46 76 72 S126 112 162 96 S214 52 258 88 S314 116 354 102";

  return (
    <div className="qd-market-pulse" aria-hidden="true">
      <div className="flex items-center justify-between">
        <div>
          <div className="qd-section-title">NIFTY Pulse</div>
          <div className="mt-1 font-mono text-xs font-bold text-[var(--qd-text)]">{label || "SESSION"}</div>
        </div>
        <span className={`qd-pulse-status ${marketOpen ? "is-open" : "is-closed"}`}>{marketOpen ? "Live" : "Closed"}</span>
      </div>
      <svg viewBox="0 0 360 132" className="mt-4 h-28 w-full">
        <defs>
          <linearGradient id="pulseLine" x1="0" x2="1" y1="0" y2="0">
            <stop offset="0%" stopColor="var(--qd-accent)" />
            <stop offset="55%" stopColor="var(--qd-cyan)" />
            <stop offset="100%" stopColor={positive ? "var(--qd-profit)" : "var(--qd-loss)"} />
          </linearGradient>
        </defs>
        <path d="M0 120 H360" stroke="var(--qd-border)" strokeWidth="1" />
        <path d="M0 78 H360" stroke="var(--qd-border)" strokeWidth="1" opacity="0.7" />
        <path d="M0 36 H360" stroke="var(--qd-border)" strokeWidth="1" opacity="0.45" />
        <path d={`${path} L354 126 L6 126 Z`} fill="var(--qd-accent)" opacity="0.08" />
        <path d={path} fill="none" stroke="url(#pulseLine)" strokeLinecap="round" strokeLinejoin="round" strokeWidth="5" />
        {[46, 118, 190, 262, 334].map((x, index) => (
          <circle key={x} cx={x} cy={index % 2 ? 82 : 42} r="3.5" fill="var(--qd-accent)" opacity="0.75" />
        ))}
      </svg>
    </div>
  );
};

const QualityPill = ({ score, readiness }) => {
  const n = Number(score || 0);
  const tone = readiness === "PASS" || n >= 70
    ? "border-[rgba(0,230,118,0.38)] bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]"
    : readiness === "BLOCK" || n < 45
      ? "border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.1)] text-[var(--qd-loss)]"
      : "border-[rgba(255,209,102,0.38)] bg-[rgba(255,209,102,0.1)] text-[var(--qd-warn)]";
  return <span className={`inline-flex items-center rounded border px-2 py-0.5 font-mono text-[10px] font-bold ${tone}`}>Q {n}</span>;
};

const metric = (row, path, fallback = 0) => {
  const value = path.split(".").reduce((acc, key) => (acc && acc[key] !== undefined ? acc[key] : undefined), row);
  return value ?? fallback;
};

const profitFactorLabel = (value) => (value == null ? "No losses" : Number(value).toFixed(2));

const StrategyPerformanceTable = ({ rows }) => (
  <div className="overflow-x-auto">
    <table className="w-full text-left text-xs">
      <thead className="bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] uppercase font-mono">
        <tr>
          <th className="px-4 py-3">Rank</th>
          <th className="px-4 py-3">Strategy</th>
          <th className="px-4 py-3 text-right">Trades</th>
          <th className="px-4 py-3 text-right">Win</th>
          <th className="px-4 py-3 text-right">7D</th>
          <th className="px-4 py-3 text-right">30D</th>
          <th className="px-4 py-3 text-right">Net P&L</th>
          <th className="px-4 py-3 text-right">PF</th>
          <th className="px-4 py-3 text-right">Risk Adj</th>
          <th className="px-4 py-3 text-right">DD</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-[var(--qd-border)]">
        {rows.length === 0 ? (
          <tr><td colSpan="10" className="px-4 py-8 text-center text-[var(--qd-text-3)]">No closed strategy trades yet.</td></tr>
        ) : rows.map((row) => {
          const lifetime = row.lifetime || {};
          const last7 = row.last_7_days || {};
          const last30 = row.last_30_days || {};
          return (
            <tr key={row.strategy_id} className="hover:bg-[var(--qd-surface)]/30">
              <td className="px-4 py-3 font-mono text-[var(--qd-text-2)]">#{row.rank}</td>
              <td className="px-4 py-3">
                <div className="font-semibold text-white">{row.strategy_name}</div>
                <div className="mt-1 font-mono text-[10px] uppercase text-[var(--qd-text-3)]">{row.recommendation || "NO_DATA"}</div>
              </td>
              <td className="px-4 py-3 text-right font-mono text-white">{lifetime.total_trades || 0}</td>
              <td className="px-4 py-3 text-right font-mono text-white">{Number(lifetime.win_rate || 0).toFixed(1)}%</td>
              <td className={`px-4 py-3 text-right font-mono ${toneClass(last7.net_pnl)}`}>{money(last7.net_pnl)}</td>
              <td className={`px-4 py-3 text-right font-mono ${toneClass(last30.net_pnl)}`}>{money(last30.net_pnl)}</td>
              <td className={`px-4 py-3 text-right font-mono font-bold ${toneClass(lifetime.net_pnl)}`}>{money(lifetime.net_pnl)}</td>
              <td className="px-4 py-3 text-right font-mono text-white">{profitFactorLabel(lifetime.profit_factor)}</td>
              <td className={`px-4 py-3 text-right font-mono ${toneClass(row.risk_adjusted_return)}`}>{Number(row.risk_adjusted_return || 0).toFixed(2)}</td>
              <td className="px-4 py-3 text-right font-mono text-[var(--qd-loss)]">{money(lifetime.max_drawdown)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  </div>
);

const HealthScoreList = ({ rows }) => (
  <div className="space-y-3">
    {rows.length === 0 ? (
      <div className="py-6 text-center text-xs text-[var(--qd-text-3)]">No health scores yet.</div>
    ) : rows.slice(0, 8).map((row) => {
      const score = Number(row.health_score || 0);
      const tone = score >= 70 ? "bg-[var(--qd-profit)]" : score >= 45 ? "bg-[var(--qd-warn)]" : "bg-[var(--qd-loss)]";
      return (
        <div key={row.strategy_id}>
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-white">{row.strategy_name}</div>
              <div className="font-mono text-[10px] uppercase text-[var(--qd-text-3)]">{row.recommendation}</div>
            </div>
            <div className="font-mono text-lg font-bold text-white">{score}</div>
          </div>
          <div className="mt-2 h-2 rounded bg-[var(--qd-surface-2)]">
            <div className={`h-2 rounded ${tone}`} style={{ width: `${Math.min(100, Math.max(0, score))}%` }} />
          </div>
        </div>
      );
    })}
  </div>
);

const AllocationList = ({ rows }) => (
  <div className="space-y-3">
    {rows.length === 0 ? (
      <div className="py-6 text-center text-xs text-[var(--qd-text-3)]">No capital increase recommended from current closed-trade history.</div>
    ) : rows.map((row) => (
      <div key={row.strategy_id}>
        <div className="flex items-center justify-between gap-3">
          <div className="truncate text-sm font-semibold text-white">{row.strategy_name}</div>
          <div className="font-mono text-lg font-bold text-[var(--qd-profit)]">{row.recommended_percent}%</div>
        </div>
        <div className="mt-2 h-2 rounded bg-[var(--qd-surface-2)]">
          <div className="h-2 rounded bg-[var(--qd-profit)]" style={{ width: `${Math.min(100, Math.max(0, row.recommended_percent))}%` }} />
        </div>
      </div>
    ))}
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
    target_pct: row.target_pct ?? 28,
    stoploss_pct: row.stoploss_pct ?? 14,
    trailing_sl_enabled: row.trailing_sl_enabled ?? true,
    trail_trigger_pct: row.trail_trigger_pct ?? 12,
    trail_step_pct: row.trail_step_pct ?? 7,
    cooldown_minutes: row.cooldown_minutes ?? 25,
    max_trades_day: row.max_trades_day ?? 2,
    daily_loss_limit: risk.daily_loss_limit ?? 0,
    required_capital: row.required_capital ?? 0,
    time_exit_minutes: row.time_exit_minutes ?? risk.time_exit_minutes ?? 35,
    indicator_exit_enabled: row.indicator_exit_enabled ?? risk.indicator_exit_enabled ?? true,
    exit_mode: row.exit_mode ?? risk.exit_mode ?? "tp_sl_tsl_or_signal",
    risk_style: row.risk_style ?? risk.risk_style ?? "balanced",
    adaptive_exits_enabled: row.adaptive_exits_enabled ?? risk.adaptive_exits_enabled ?? true,
    target_r_multiple: row.target_r_multiple ?? risk.target_r_multiple ?? 1.45,
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
          <RuntimeInput label="Target R" value={form.target_r_multiple} onChange={(v) => update("target_r_multiple", v)} />
          <label className="col-span-2 space-y-1 md:col-span-1">
            <span className="qd-section-title text-[9px]">Risk style</span>
            <select
              value={form.risk_style}
              onChange={(e) => update("risk_style", e.target.value)}
              className="w-full rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-2 py-2 font-mono text-xs text-white"
            >
              <option value="micro_scalp">Micro scalp</option>
              <option value="momentum">Momentum</option>
              <option value="breakout">Breakout</option>
              <option value="volatile_breakout">Volatile breakout</option>
              <option value="pullback">Pullback</option>
              <option value="balanced">Balanced</option>
            </select>
          </label>
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
          <label className="flex items-center gap-2 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-3 py-2 text-xs text-[var(--qd-text-2)]">
            <input type="checkbox" checked={form.adaptive_exits_enabled} onChange={(e) => update("adaptive_exits_enabled", e.target.checked)} />
            Adaptive exits
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

const StrategyLedgerRow = ({ row, onToggle, onExit }) => {
  const position = row.position;
  const pendingOrder = row.pendingOrder;
  const failedOrder = row.failedOrder;
  const quality = pendingOrder?.quality_score ?? failedOrder?.quality_score ?? position?.quality_score;
  const qualityReadiness = pendingOrder?.quality_readiness ?? failedOrder?.quality_readiness ?? position?.quality_readiness;
  const status = position?.execution_status || position?.ledger_status || (pendingOrder ? pendingOrder.status : row.state || "FLAT");
  const positionOpen = position && hasQty(position.qty);
  const problem = failedOrder || asStatus(position?.execution_status) === "BROKER_NOT_FOUND";
  const warning = !problem && (pendingOrder || PENDING_POSITION_STATES.includes(asStatus(status)));
  const tone = problem ? "bad" : warning ? "warn" : positionOpen ? "good" : row.status === "live" ? "neutral" : "neutral";
  const pnl = position?.pnl ?? row.active_position?.unrealized_pnl ?? row.daily_pnl?.realised_pnl ?? 0;
  const slMissing = positionOpen && position?.stop_loss == null;
  const tpMissing = positionOpen && position?.take_profit == null;
  const live = row.status === "live";
  const idleLabel = live ? (row.state === "SCANNING" ? "Scanning" : row.state || "Live") : "Flat";
  const telemetry = row.telemetry || {};
  const detail = problem
    ? failedOrder?.status_message || telemetry.last_error
    : pendingOrder
      ? pendingOrder.status_message || "Waiting for broker order book sync"
      : telemetry.last_error || telemetry.last_filter_reason || telemetry.last_data_reason || telemetry.last_data_source;

  return (
    <div className="grid gap-3 border-t border-[var(--qd-border)] px-4 py-3 lg:grid-cols-[minmax(220px,1.1fr)_minmax(260px,1.35fr)_minmax(180px,0.9fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-white">{row.name}</div>
        <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">{shortId(row.strategy_id)}</div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label="Position" value={positionOpen ? position.symbol : "Flat"} tone={positionOpen ? "text-white" : "text-[var(--qd-text-3)]"} />
        <Field label="Qty" value={positionOpen ? position.qty : "0"} tone={positionOpen ? "text-[var(--qd-profit)]" : "text-[var(--qd-text-3)]"} />
        <Field label="SL" value={position?.stop_loss != null ? money(position.stop_loss) : "-"} tone={slMissing ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"} />
        <Field label="TP" value={position?.take_profit != null ? money(position.take_profit) : "-"} tone={tpMissing ? "text-[var(--qd-warn)]" : "text-[var(--qd-text-2)]"} />
      </div>

      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={tone}>{positionOpen ? status : pendingOrder ? "Order pending" : problem ? "Needs check" : idleLabel}</StatusPill>
          {quality != null && <QualityPill score={quality} readiness={qualityReadiness} />}
          {slMissing && <StatusPill tone="bad">No SL</StatusPill>}
          {pendingOrder && <StatusPill tone="warn">Broker sync</StatusPill>}
        </div>
        <div className={`mt-2 font-mono text-xs font-semibold ${toneClass(pnl)}`}>{money(pnl)}</div>
        {detail && <div className="mt-1 max-w-[260px] truncate text-[10px] text-[var(--qd-text-3)]" title={detail}>{detail}</div>}
      </div>

      <div className="flex items-center justify-end gap-2">
        <button
          onClick={() => onToggle(row.strategy_id)}
          className={`flex h-8 w-8 items-center justify-center rounded border transition-all ${
            live
              ? "border-[rgba(255,159,10,0.4)] text-[var(--qd-warn)] hover:bg-[rgba(255,159,10,0.1)]"
              : "border-[rgba(0,230,118,0.4)] text-[var(--qd-profit)] hover:bg-[rgba(0,230,118,0.1)]"
          }`}
          title={live ? "Pause Strategy" : "Resume Strategy"}
          data-testid={`dashboard-toggle-${row.strategy_id}`}
        >
          {live ? <Pause size={13} /> : <Play size={13} />}
        </button>
        <button
          onClick={() => onExit(row.strategy_id)}
          disabled={!positionOpen}
          className="flex h-8 w-8 items-center justify-center rounded border border-[var(--qd-warn)] text-[var(--qd-warn)] transition-all hover:bg-[var(--qd-warn)] hover:text-black disabled:cursor-not-allowed disabled:opacity-35"
          title={positionOpen ? "Square Off Strategy Positions" : "No open position for this strategy"}
          data-testid={`dashboard-exit-${row.strategy_id}`}
        >
          <Shield size={13} />
        </button>
      </div>
    </div>
  );
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
    refresh: refreshExecution,
  } = useExecutionState({ pollMs: 15000 });
  const [pf, setPf] = useState(null);
  const [watch, setWatch] = useState([]);
  const positions = execPositions;
  const orders = execOrders;
  const [funds, setFunds] = useState(null);
  const [telemetry, setTelemetry] = useState(null);
  const [marketSession, setMarketSession] = useState(null);
  const [strategyAnalytics, setStrategyAnalytics] = useState(null);
  const [optionChain, setOptionChain] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [activeTab, setActiveTab] = useState("overview");

  const load = useCallback(async () => {
    try {
      const [p, w, f, t, c, s, leaderboard] = await Promise.all([
        api.get("/portfolio"),
        api.get("/market/watchlist"),
        api.get("/funds"),
        api.get("/v1/dashboard/telemetry"),
        api.get("/market/session-status"),
        api.get("/strategies/leaderboard"),
        api.get("/upstox/option-chain", { params: { underlying: "NIFTY" } }).catch(() => ({ data: null })),
      ]);
      await refreshExecution();
      setPf(p.data);
      setWatch(w.data);
      setFunds(f.data);
      setTelemetry(t.data);
      setMarketSession(c.data);
      setStrategyAnalytics(s.data);
      setOptionChain(leaderboard.data);
      setLoadError("");
    } catch (e) {
      setLoadError(e?.response?.data?.detail || e.message || "Dashboard data could not be loaded");
    }
  }, [refreshExecution]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  const pnl = executionSummary.net_pnl || pf?.net_pnl || pf?.total_pnl || 0;
  const grossPnl = executionSummary.gross_pnl || pf?.gross_pnl || pnl;
  const charges = executionSummary.charges || pf?.charges || 0;
  const openPositions = executionSummary.open_positions ?? pf?.open_positions ?? positions.length;
  const strategies = useMemo(() => telemetry?.strategies_page_data || [], [telemetry?.strategies_page_data]);
  const marketOpen = marketSession ? marketSession.global_status === "OPEN" : telemetry?.market_status?.is_open;
  const marketStatusLabel = marketSession?.global_status || (marketOpen ? "OPEN" : "CLOSED");
  const firstRisk = strategies[0]?.risk_settings || {};
  const topWatch = useMemo(() => watch.slice(0, 8), [watch]);
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
  const foPositions = useMemo(() => positions.filter((p) => p.option_type || p.symbol.endsWith("CE") || p.symbol.endsWith("PE") || p.exchange === "NFO" || p.exchange === "BFO"), [positions]);
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
        <div className="qd-card qd-hero-panel qd-dashboard-hero overflow-hidden">
          <div className="border-b border-[var(--qd-border)] p-5">
            <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px] lg:items-center">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="qd-chip">Control Room</span>
                  <span className="qd-chip">{marketStatusLabel}</span>
                  <span className="qd-chip">{openPositions} Active</span>
                </div>
                <h1 className="mt-4 font-head text-3xl font-extrabold tracking-tight text-[var(--qd-text)] md:text-4xl">QuantG Dashboard</h1>
                <p className="mt-2 max-w-2xl text-sm text-[var(--qd-text-2)]">
                  Live portfolio state, NIFTY telemetry, and focused controls for strategy execution.
                </p>
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={handleRefresh}
                    disabled={refreshing}
                    className="flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)] disabled:opacity-50"
                    title="Refresh data"
                  >
                    <RefreshCw size={15} className={refreshing ? "animate-spin" : ""} /> Refresh
                  </button>
                  <Link
                    to="/ai-bot"
                    className="flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
                  >
                    <Bot size={15} /> AI Bot
                  </Link>
                  <Link
                    to="/strategies"
                    className="qd-force-white flex items-center gap-2 rounded-[var(--qd-radius-sm)] bg-[var(--qd-accent)] px-3 py-2 font-mono text-xs font-semibold uppercase tracking-wider hover:bg-[var(--qd-accent-hover)]"
                    data-testid="new-strategy-btn"
                  >
                    <Zap size={15} /> Strategy
                  </Link>
                </div>
              </div>
              <MarketPulseGraphic pnl={pnl} marketOpen={marketOpen} label={marketStatusLabel} />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-px bg-[var(--qd-border)] md:grid-cols-4">
            <div className="bg-[var(--qd-surface)] p-4">
              <Field label="Market Status" value={marketStatusLabel} tone={marketOpen ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"} />
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
            <Field label="Cooldown delay" value={`${strategies[0]?.cooldown_minutes ?? 25} min`} />
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
          <button
            type="button"
            onClick={exitAllUpstox}
            className="mt-3 flex w-full items-center justify-center gap-2 rounded border border-[rgba(255,59,48,0.42)] px-4 py-3 font-mono text-xs font-bold uppercase tracking-wider text-[var(--qd-loss)] hover:bg-[rgba(255,59,48,0.1)]"
          >
            <AlertTriangle size={15} /> Upstox Exit All
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
          { id: "equity", label: "Equity Spot" },          { id: "fo", label: "Derivatives (F&O)" },
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
            <KpiCard label="Net P&L" value={money(pnl)} icon={pnl >= 0 ? TrendingUp : TrendingDown} tone={toneClass(pnl)} sub={`Gross ${money(grossPnl)} after charges ${money(charges)}`} />
            <KpiCard
              label="Trade Safety"
              value={`${missingProtectionCount}/${openStrategyPositions.length}`}
              icon={Activity}
              tone={missingProtectionCount ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"}
              sub={`${openOrders.length} pending orders, ${failedOrders.length} failed`}
            />
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
            <div className="qd-card p-5">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <div className="qd-section-title">// Upstox</div>
                  <h2 className="mt-1 font-head text-lg font-semibold text-white">Data Health</h2>
                </div>
                <StatusPill tone={upstoxDataHealth?.readiness === "READY" ? "good" : "warn"}>{upstoxDataHealth?.readiness || "UNKNOWN"}</StatusPill>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Websocket" value={upstoxDataHealth?.connected ? "Connected" : "Disconnected"} tone={upstoxDataHealth?.connected ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"} />
                <Field label="Snapshot" value={upstoxDataHealth?.snapshot_received ? "Received" : "Waiting"} tone={upstoxDataHealth?.snapshot_received ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"} />
                <Field label="Quote Age" value={quoteAge(upstoxDataHealth?.quote_age_sec)} tone={upstoxDataHealth?.quote_stale ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
                <Field label="Latest Tick" value={quoteTime(upstoxDataHealth?.latest_tick_time)} />
              </div>
            </div>

            <div className="qd-card p-5">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <div className="qd-section-title">// Broker truth</div>
                  <h2 className="mt-1 font-head text-lg font-semibold text-white">Reconciliation</h2>
                </div>
                <StatusPill tone={brokerReconciliation?.status === "OK" ? "good" : "warn"}>{brokerReconciliation?.status || "UNKNOWN"}</StatusPill>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Pending Gaps" value={brokerReconciliation?.mismatches?.pending_orders_without_broker_match ?? 0} tone={(brokerReconciliation?.mismatches?.pending_orders_without_broker_match || 0) ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
                <Field label="Position Gaps" value={brokerReconciliation?.mismatches?.position_key_mismatches ?? 0} tone={(brokerReconciliation?.mismatches?.position_key_mismatches || 0) ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
                <Field label="Local Live" value={brokerReconciliation?.mismatches?.local_positions ?? 0} />
                <Field label="Broker Live" value={brokerReconciliation?.mismatches?.broker_positions ?? 0} />
              </div>
            </div>

            <div className="qd-card p-5">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <div className="qd-section-title">// Skipped</div>
                  <h2 className="mt-1 font-head text-lg font-semibold text-white">Signal Reasons</h2>
                </div>
                <StatusPill tone={skippedSignals.length ? "warn" : "good"}>{skippedSignals.length}</StatusPill>
              </div>
              <div className="space-y-3">
                {skippedSignals.length === 0 ? (
                  <div className="text-xs text-[var(--qd-text-3)]">No skipped signals recorded.</div>
                ) : skippedSignals.slice(0, 4).map((row) => (
                  <div key={row.id || row.dedupe_key} className="border-b border-[var(--qd-border)] pb-2 last:border-0">
                    <div className="font-mono text-xs text-white">{row.reason_code || row.reason || "SKIPPED"}</div>
                    <div className="mt-1 text-[11px] text-[var(--qd-text-2)]">{row.symbol || row.strategy_id || "-"} · {row.count || 1}x</div>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-4">
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
            <section className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-[1.4fr_0.6fr]">
              <div className="qd-card overflow-hidden">
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] p-5">
                <div>
                  <div className="qd-section-title">// Closed trade performance</div>
                  <h2 className="mt-1 font-head text-xl font-semibold text-white">Strategy Leaderboard</h2>
                </div>
                <StatusPill tone={strategyAnalytics?.data_quality?.included_closed_trades ? "good" : "neutral"}>
                  {strategyAnalytics?.data_quality?.included_closed_trades || 0} Trades
                </StatusPill>
              </div>
              <StrategyPerformanceTable rows={leaderboardRows} />
              {(strategyAnalytics?.data_quality?.excluded_missing_strategy || strategyAnalytics?.data_quality?.excluded_unknown_strategy || strategyAnalytics?.data_quality?.excluded_missing_close_time) ? (
                <div className="border-t border-[var(--qd-border)] px-5 py-3 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-warn)]">
                  Excluded rows: {strategyAnalytics?.data_quality?.excluded_missing_strategy || 0} missing strategy, {strategyAnalytics?.data_quality?.excluded_unknown_strategy || 0} unknown strategy, {strategyAnalytics?.data_quality?.excluded_missing_close_time || 0} missing close time.
                </div>
              ) : null}
            </div>

              <div className="grid grid-cols-1 gap-4">
                <div className="qd-card p-5">
                <div className="mb-4 flex items-center justify-between">
                  <div>
                    <div className="qd-section-title">// Score 0-100</div>
                    <h2 className="mt-1 font-head text-lg font-semibold text-white">Health Scores</h2>
                  </div>
                  <Shield size={18} className="text-[var(--qd-text-3)]" />
                </div>
                <HealthScoreList rows={strategyAnalytics?.health_scores || []} />
              </div>

                <div className="qd-card p-5">
                <div className="mb-4 flex items-center justify-between">
                  <div>
                    <div className="qd-section-title">// Recommendation only</div>
                    <h2 className="mt-1 font-head text-lg font-semibold text-white">Capital Allocation</h2>
                  </div>
                  <Wallet size={18} className="text-[var(--qd-text-3)]" />
                </div>
                <AllocationList rows={strategyAnalytics?.capital_allocation || []} />
              </div>
              </div>
            </section>

            <section className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
            <div className="qd-card p-5">
              <div className="qd-section-title">// Winners</div>
              <h2 className="mt-1 font-head text-lg font-semibold text-white">Top Performers</h2>
              <div className="mt-4 space-y-3">
                {(strategyAnalytics?.top_performers || []).length === 0 ? <div className="text-xs text-[var(--qd-text-3)]">No closed winners yet.</div> : strategyAnalytics.top_performers.map((row) => (
                  <Field key={row.strategy_id} label={row.strategy_name} value={money(row.lifetime?.net_pnl)} tone={toneClass(row.lifetime?.net_pnl)} />
                ))}
              </div>
            </div>
            <div className="qd-card p-5">
              <div className="qd-section-title">// Losers</div>
              <h2 className="mt-1 font-head text-lg font-semibold text-white">Worst Performers</h2>
              <div className="mt-4 space-y-3">
                {(strategyAnalytics?.worst_performers || []).length === 0 ? <div className="text-xs text-[var(--qd-text-3)]">No closed losers yet.</div> : strategyAnalytics.worst_performers.map((row) => (
                  <Field key={row.strategy_id} label={row.strategy_name} value={money(row.lifetime?.net_pnl)} tone={toneClass(row.lifetime?.net_pnl)} />
                ))}
              </div>
            </div>
            <div className="qd-card p-5">
              <div className="qd-section-title">// Drawdown</div>
              <h2 className="mt-1 font-head text-lg font-semibold text-white">Drawdown Monitor</h2>
              <div className="mt-4 space-y-3">
                {(strategyAnalytics?.drawdown_monitor || []).length === 0 ? <div className="text-xs text-[var(--qd-text-3)]">No drawdown history yet.</div> : strategyAnalytics.drawdown_monitor.map((row) => (
                  <Field key={row.strategy_id} label={row.strategy_name} value={money(row.lifetime?.max_drawdown)} tone={row.lifetime?.max_drawdown > 0 ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"} />
                ))}
              </div>
            </div>
            </section>
          </details>

          {/* Position Integrity Status */}
          <div className="qd-card p-5">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3">
              <div>
                <div className="qd-section-title">// HEALTH CHECK</div>
                <h2 className="mt-1 font-head text-xl font-semibold text-white">Position Integrity</h2>
              </div>
              <span className={`inline-flex items-center rounded border px-2.5 py-1 font-mono text-xs font-bold uppercase tracking-wider ${
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
            
            <div className="grid grid-cols-2 gap-4 mt-4 md:grid-cols-5 text-center font-mono">
              <div className="bg-[var(--qd-surface-2)] p-3 rounded border border-[var(--qd-border)]">
                <div className="text-[10px] text-[var(--qd-text-3)] uppercase tracking-wider">Orphans</div>
                <div className={`text-2xl font-bold mt-2 ${(executionSummary.summary?.position_integrity?.orphans || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.orphans ?? 0}
                </div>
                <div className="text-[9px] text-[var(--qd-text-3)] mt-1">Target: 0</div>
              </div>
              <div className="bg-[var(--qd-surface-2)] p-3 rounded border border-[var(--qd-border)]">
                <div className="text-[10px] text-[var(--qd-text-3)] uppercase tracking-wider">Missing SL</div>
                <div className={`text-2xl font-bold mt-2 ${(executionSummary.summary?.position_integrity?.missing_sl || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.missing_sl ?? 0}
                </div>
                <div className="text-[9px] text-[var(--qd-text-3)] mt-1">Target: 0</div>
              </div>
              <div className="bg-[var(--qd-surface-2)] p-3 rounded border border-[var(--qd-border)]">
                <div className="text-[10px] text-[var(--qd-text-3)] uppercase tracking-wider">Missing TP</div>
                <div className={`text-2xl font-bold mt-2 ${(executionSummary.summary?.position_integrity?.missing_tp || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.missing_tp ?? 0}
                </div>
                <div className="text-[9px] text-[var(--qd-text-3)] mt-1">Target: 0</div>
              </div>
              <div className="bg-[var(--qd-surface-2)] p-3 rounded border border-[var(--qd-border)]">
                <div className="text-[10px] text-[var(--qd-text-3)] uppercase tracking-wider">Ledger Mismatches</div>
                <div className={`text-2xl font-bold mt-2 ${(executionSummary.summary?.position_integrity?.strategy_mismatches || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.strategy_mismatches ?? 0}
                </div>
                <div className="text-[9px] text-[var(--qd-text-3)] mt-1">Target: 0</div>
              </div>
              <div className="bg-[var(--qd-surface-2)] p-3 rounded border border-[var(--qd-border)]">
                <div className="text-[10px] text-[var(--qd-text-3)] uppercase tracking-wider">Failed Orders</div>
                <div className={`text-2xl font-bold mt-2 ${(executionSummary.summary?.position_integrity?.failed_orders || 0) === 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {executionSummary.summary?.position_integrity?.failed_orders ?? 0}
                </div>
                <div className="text-[9px] text-[var(--qd-text-3)] mt-1">Target: 0</div>
              </div>
            </div>
          </div>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
            {/* Strategy summaries and engines */}
            <div className="space-y-4">
              <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3">
                <div>
                  <div className="qd-section-title">// Runtime ledger states</div>
                  <h2 className="mt-1 font-head text-xl font-semibold text-white">Strategy Position Ledger</h2>
                </div>
                <Link to="/strategies" className="font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:text-white">
                  Manage strategies
                </Link>
              </div>
              <div className="qd-card overflow-hidden">
                <div className="grid grid-cols-2 gap-px bg-[var(--qd-border)] md:grid-cols-5">
                  {strategySummary.map((item) => (
                    <div key={item.label} className="bg-[var(--qd-bg)] p-4">
                      <div className="qd-section-title">{item.label}</div>
                      <div className={`mt-2 font-mono text-2xl font-bold ${
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
                    <div className="px-4 py-8 text-center text-xs text-[var(--qd-text-3)]">No strategies loaded.</div>
                  ) : (
                    strategiesWithExecution.map((row) => (
                      <StrategyLedgerRow key={row.strategy_id} row={row} onToggle={toggleStrategy} onExit={exitStrategy} />
                    ))
                  )}
                </div>

                {/* Active strategy logs */}
                <div className="hidden">
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
                  <Field label="Telemetry Source" value={telemetry?.market_status?.feed_source_label || telemetry?.market_status?.data_source || "Upstox Feed"} />
                </div>
                {telemetry?.market_status?.simulated_warning && (
                  <div className="mt-3 rounded border border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.08)] px-3 py-2 text-xs font-mono text-[var(--qd-loss)]">
                    Simulated feed active - paper results are not market-valid.
                  </div>
                )}
              </div>
            </aside>
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
              <div className="p-10 text-center">
                <Target className="mx-auto mb-2 text-[var(--qd-text-3)]" size={20} />
                <div className="text-xs text-[var(--qd-text-2)]">No active trades currently open.</div>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">
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
                {topWatch.map((item) => (
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
                  <StatusPill tone={optionChain?.row_count ? "good" : "warn"}>{optionChain?.row_count || 0} Rows</StatusPill>
                </div>
                {optionChain?.data?.length ? (
                  <div className="overflow-x-auto border-b border-[var(--qd-border)]">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-[var(--qd-border)] text-left font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">
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
