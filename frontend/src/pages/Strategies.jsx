import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  Blocks,
  Bot,
  Code2,
  Filter,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Shield,
  Trash2,
  TrendingDown,
  TrendingUp,
  X,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import { api, formatINR } from "../lib/api";

const money = (value) => `INR ${formatINR(value ?? 0)}`;

const timeAgo = (iso) => {
  if (!iso) return "-";
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};

const sourceLabel = (source) => {
  if (!source) return "-";
  if (source.startsWith("zerodha-kite-5minute")) return source.includes("tick-live") ? "Real ticker" : "Kite 5m";
  if (source.startsWith("mock-5minute")) return "Mock 5m";
  return source;
};

const filters = [
  { id: "all", label: "All" },
  { id: "options", label: "Options" },
  { id: "futures", label: "Futures" },
  { id: "buying", label: "Option Buying" },
  { id: "selling", label: "Option Selling" },
  { id: "commodity", label: "Oil and Gas" },
  { id: "live", label: "Live" },
];

const noticeFor = (s) => {
  if (s.last_filter_reason) return { text: s.last_filter_reason, kind: "filter" };
  if (s.last_error?.startsWith("Signal filtered:")) return { text: s.last_error, kind: "filter" };
  if (s.last_error?.includes("entry blocked: cooldown-active")) return { text: "Entry skipped: cooldown active", kind: "filter" };
  if (s.last_error?.includes("entry blocked: duplicate-buy-dropped")) return { text: "Entry skipped: duplicate buy dropped", kind: "filter" };
  if (s.last_error?.includes("entry blocked: max-trades-day-reached")) return { text: "Entry skipped: max trades reached", kind: "filter" };
  if (s.last_error) return { text: s.last_error, kind: "error" };
  return null;
};

export default function Strategies() {
  const [list, setList] = useState([]);
  const [scores, setScores] = useState({});
  const [testing, setTesting] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [selectedFilter, setSelectedFilter] = useState("all");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [strategiesRes, scoresRes] = await Promise.all([
        api.get("/strategies"),
        api.get("/ai/strategy-scores").catch(() => ({ data: { scores: [] } })),
      ]);
      setList(strategiesRes.data || []);
      setScores(Object.fromEntries((scoresRes.data?.scores || []).map((row) => [row.strategy_id, row])));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const filtered = useMemo(() => {
    if (selectedFilter === "all") return list;
    if (selectedFilter === "options") return list.filter((s) => s.asset_class === "options");
    if (selectedFilter === "futures") return list.filter((s) => s.asset_class === "futures");
    if (selectedFilter === "buying") return list.filter((s) => s.strategy_type === "Option Buying");
    if (selectedFilter === "selling") return list.filter((s) => s.strategy_type === "Option Selling");
    if (selectedFilter === "commodity") return list.filter((s) => s.asset_class === "commodity" || s.instrument_group === "MCX");
    if (selectedFilter === "live") return list.filter((s) => s.status === "live");
    return list;
  }, [list, selectedFilter]);

  const counts = useMemo(() => ({
    all: list.length,
    options: list.filter((s) => s.asset_class === "options").length,
    futures: list.filter((s) => s.asset_class === "futures").length,
    buying: list.filter((s) => s.strategy_type === "Option Buying").length,
    selling: list.filter((s) => s.strategy_type === "Option Selling").length,
    commodity: list.filter((s) => s.asset_class === "commodity" || s.instrument_group === "MCX").length,
    live: list.filter((s) => s.status === "live").length,
  }), [list]);

  const toggle = async (id) => {
    await api.post(`/strategies/${id}/toggle`);
    load();
  };

  const del = async (id) => {
    if (!window.confirm("Delete strategy?")) return;
    await api.delete(`/strategies/${id}`);
    load();
  };

  const testRun = async (id) => {
    setTesting(id);
    setTestResult(null);
    try {
      const r = await api.post(`/strategies/${id}/test-run`);
      setTestResult(r.data);
    } catch (e) {
      setTestResult({ ok: false, error: e?.response?.data?.detail || e.message });
    } finally {
      setTesting(null);
      load();
    }
  };

  const manualOrder = async (id, action) => {
    const verb = action === "BUY" ? "Buy" : "Sell";
    if (!window.confirm(`${verb} now using this strategy's symbol and default quantity?`)) return;
    try {
      const r = await api.post(`/strategies/${id}/manual-order`, { action });
      const o = r.data?.order || {};
      toast.success(`${action} placed: ${o.qty} ${o.symbol} @ ${money(o.price)}`);
      load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || `${verb} failed`);
    }
  };

  const exitAll = async (id) => {
    if (!window.confirm("Square off all open positions from this strategy?")) return;
    try {
      const r = await api.post(`/strategies/${id}/exit-all`);
      const closed = r.data?.closed_positions || [];
      if (!closed.length) toast.info("No open positions to close from this strategy");
      else toast.success(`Closed ${closed.filter((c) => c.status === "ok").length} position(s)`);
      load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Exit failed");
    }
  };

  const installPresets = async () => {
    try {
      const r = await api.post("/strategies/seed-defaults");
      const inserted = r.data?.inserted || 0;
      toast[inserted ? "success" : "info"](inserted ? `Installed ${inserted} standardized presets` : "Standardized presets are already installed");
      load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Preset install failed");
    }
  };

  return (
    <div className="space-y-5" data-testid="strategies-page">
      <section className="flex flex-col gap-4 border-b border-[var(--qd-border)] pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="qd-section-title">Strategy Catalog</div>
          <h1 className="mt-1 font-head text-3xl font-bold text-white">Standardized Strategies</h1>
          <p className="mt-2 max-w-2xl text-sm text-[var(--qd-text-2)]">
            Filterable option systems with explicit capital, strategy type, and live AI confidence.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={load} className="flex items-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-white" data-testid="refresh-strategies">
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
          <button onClick={installPresets} className="flex items-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-white hover:border-[var(--qd-accent)]" data-testid="install-presets-btn">
            <Zap size={14} /> Presets
          </button>
          <Link to="/python" className="flex items-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-white hover:border-white" data-testid="new-python-btn">
            <Code2 size={14} /> Python
          </Link>
          <Link to="/visual" className="flex items-center gap-2 rounded bg-[var(--qd-accent)] px-3 py-2 font-mono text-xs font-semibold uppercase tracking-wider text-white hover:bg-[var(--qd-accent-hover)]" data-testid="new-visual-btn">
            <Blocks size={14} /> Builder
          </Link>
        </div>
      </section>

      <section className="qd-card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-2 pr-1 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)]">
            <Filter size={13} /> Filter
          </div>
          {filters.map((item) => (
            <button
              key={item.id}
              onClick={() => setSelectedFilter(item.id)}
              className={`rounded border px-3 py-2 font-mono text-xs uppercase tracking-wider ${
                selectedFilter === item.id
                  ? "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-white"
                  : "border-[var(--qd-border)] text-[var(--qd-text-2)] hover:text-white"
              }`}
              data-testid={`filter-${item.id}`}
            >
              {item.label} <span className="text-[var(--qd-text-3)]">({counts[item.id]})</span>
            </button>
          ))}
        </div>
      </section>

      {!filtered.length ? (
        <div className="qd-card p-16 text-center">
          <Plus className="mx-auto mb-3 text-[var(--qd-text-3)]" />
          <p className="text-sm text-[var(--qd-text-2)]">No strategies match this filter.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2 2xl:grid-cols-3">
          {filtered.map((s) => (
            <StrategyCard
              key={s.id}
              s={s}
              score={scores[s.id]}
              testing={testing}
              toggle={toggle}
              del={del}
              testRun={testRun}
              manualOrder={manualOrder}
              exitAll={exitAll}
            />
          ))}
        </div>
      )}

      {testResult && <TestResultModal testResult={testResult} onClose={() => setTestResult(null)} />}
    </div>
  );
}

function StrategyCard({ s, score, testing, toggle, del, testRun, manualOrder, exitAll }) {
  const live = s.status === "live";
  const paused = s.status === "paused";
  const notice = noticeFor(s);
  const editPath = s.kind === "python" ? `/python?id=${s.id}` : `/visual?id=${s.id}`;
  const scoreValue = score?.score ?? s.ai_confidence_score ?? 0;

  return (
    <article className="qd-card flex min-h-[390px] flex-col p-4" data-testid={`strategy-${s.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="qd-section-title">{s.instrument_group || "NSE"} / {s.kind}</div>
          <h2 className="mt-1 line-clamp-2 font-head text-lg font-semibold text-white">{s.name}</h2>
        </div>
        <StatusBadge status={s.status} />
      </div>

      <p className="mt-3 min-h-[40px] text-sm leading-relaxed text-[var(--qd-text-2)]">{s.description || "No description"}</p>

      <div className="mt-4 grid grid-cols-2 gap-3">
        <Metric label="Total Capital Required" value={money(s.required_capital)} />
        <Metric label="Strategy Type" value={s.strategy_type || "Option Buying"} />
        <Metric label="Asset" value={s.asset_class === "commodity" ? "Oil and Gas" : s.asset_class || "equity"} />
        <Metric label="Data" value={sourceLabel(s.last_data_source)} tone={s.last_data_live ? "text-[var(--qd-profit)]" : ""} />
      </div>

      <div className="mt-4 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Bot size={15} className="text-[var(--qd-accent)]" />
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-2)]">AI Confidence Score</span>
          </div>
          <span className="font-mono text-lg font-bold text-white">{scoreValue ? `${scoreValue}%` : "-"}</span>
        </div>
        <div className="mt-2 h-2 overflow-hidden rounded bg-[var(--qd-surface-2)]">
          <div className="h-full bg-[var(--qd-accent)]" style={{ width: `${Math.max(0, Math.min(100, scoreValue))}%` }} />
        </div>
        <p className="mt-2 line-clamp-2 text-xs text-[var(--qd-text-3)]">{score?.reason || s.ai_confidence_reason || "Waiting for live market structure."}</p>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-2 border-y border-[var(--qd-border)] py-3">
        <Metric label="Scans" value={s.evaluations ?? 0} compact />
        <Metric label="Signals" value={s.signals_fired ?? 0} compact tone={(s.signals_fired ?? 0) > 0 ? "text-[var(--qd-profit)]" : ""} />
        <Metric label="Last Scan" value={timeAgo(s.last_evaluated_at)} compact />
      </div>

      {notice && (
        <div className={`mt-3 rounded border px-3 py-2 text-xs ${
          notice.kind === "filter"
            ? "border-[rgba(255,159,10,0.35)] text-[var(--qd-warn)]"
            : "border-[rgba(255,59,48,0.35)] text-[var(--qd-loss)]"
        }`}>
          {notice.text}
        </div>
      )}

      <div className="mt-auto pt-4">
        <div className="grid grid-cols-3 gap-2">
          <ActionButton onClick={() => manualOrder(s.id, "BUY")} tone="buy" icon={TrendingUp} label="Buy" />
          <ActionButton onClick={() => manualOrder(s.id, "SELL")} tone="sell" icon={TrendingDown} label="Sell" />
          <ActionButton onClick={() => exitAll(s.id)} tone="warn" icon={Shield} label="Exit" />
        </div>
        <div className="mt-2 grid grid-cols-[1fr_1fr_auto_auto] gap-2">
          <button onClick={() => testRun(s.id)} disabled={testing === s.id} className="flex items-center justify-center gap-2 rounded border border-[var(--qd-accent)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:bg-[var(--qd-accent)] hover:text-white disabled:opacity-50" data-testid={`test-run-${s.id}`}>
            <Activity size={13} /> {testing === s.id ? "Running" : "Test"}
          </button>
          <button onClick={() => toggle(s.id)} className="flex items-center justify-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-white hover:border-white" data-testid={`toggle-${s.id}`}>
            {live ? <><Pause size={13} /> Pause</> : <><Play size={13} /> {paused ? "Resume" : "Live"}</>}
          </button>
          <Link to={editPath} className="rounded border border-[var(--qd-border)] px-3 py-2 text-center font-mono text-xs uppercase tracking-wider text-white hover:border-white" data-testid={`edit-${s.id}`}>
            Edit
          </Link>
          <button onClick={() => del(s.id)} className="rounded border border-[var(--qd-border)] px-3 py-2 text-[var(--qd-loss)] hover:border-[var(--qd-loss)]" data-testid={`delete-${s.id}`} aria-label="Delete strategy">
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </article>
  );
}

function StatusBadge({ status }) {
  const tone = status === "live"
    ? "border-[rgba(0,230,118,0.38)] bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]"
    : status === "paused"
      ? "border-[rgba(255,159,10,0.38)] bg-[rgba(255,159,10,0.1)] text-[var(--qd-warn)]"
      : "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]";
  return <span className={`rounded border px-2 py-1 font-mono text-[10px] uppercase tracking-wider ${tone}`}>{status || "draft"}</span>;
}

function Metric({ label, value, tone, compact = false }) {
  return (
    <div className="min-w-0">
      <div className="font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">{label}</div>
      <div className={`${compact ? "text-xs" : "text-sm"} mt-1 truncate font-mono font-semibold ${tone || "text-white"}`}>{value}</div>
    </div>
  );
}

function ActionButton({ onClick, icon: Icon, label, tone }) {
  const styles = {
    buy: "border-[var(--qd-profit)] text-[var(--qd-profit)] hover:bg-[var(--qd-profit)] hover:text-black",
    sell: "border-[var(--qd-loss)] text-[var(--qd-loss)] hover:bg-[var(--qd-loss)] hover:text-white",
    warn: "border-[var(--qd-warn)] text-[var(--qd-warn)] hover:bg-[var(--qd-warn)] hover:text-black",
  };
  return (
    <button onClick={onClick} className={`flex items-center justify-center gap-1 rounded border px-2 py-2 font-mono text-xs uppercase tracking-wider ${styles[tone]}`}>
      <Icon size={13} /> {label}
    </button>
  );
}

function TestResultModal({ testResult, onClose }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4" onClick={onClose} data-testid="test-result-modal">
      <div className="qd-card max-h-[85vh] w-full max-w-2xl overflow-y-auto p-6" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <div className="qd-section-title">Test Run Results</div>
            <h2 className="mt-1 font-head text-xl text-white">{testResult.symbol || "Strategy"}</h2>
          </div>
          <button onClick={onClose} className="text-[var(--qd-text-2)] hover:text-white" data-testid="close-test-modal" aria-label="Close test result">
            <X size={18} />
          </button>
        </div>
        {testResult.error && (
          <div className="mb-3 rounded border border-[var(--qd-loss)] bg-[rgba(255,59,48,0.1)] p-3 text-xs text-[var(--qd-loss)]">
            {testResult.error}
          </div>
        )}
        {testResult.ok && (
          <div className="space-y-4 text-xs">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
              <Metric label="Total P&L" value={money(testResult.summary?.total_pnl)} tone={(testResult.summary?.total_pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"} />
              <Metric label="Return" value={`${testResult.summary?.return_pct?.toFixed(2) || 0}%`} />
              <Metric label="Win Rate" value={`${testResult.summary?.win_rate?.toFixed(1) || 0}%`} />
              <Metric label="Trades" value={testResult.summary?.trades || 0} />
              <Metric label="Wins" value={testResult.summary?.wins || 0} tone="text-[var(--qd-profit)]" />
              <Metric label="Losses" value={testResult.summary?.losses || 0} tone="text-[var(--qd-loss)]" />
            </div>
            <div className="border-t border-[var(--qd-border)] pt-3">
              <Metric label="Data Source" value={testResult.data_source || "-"} />
            </div>
            {testResult.signal_validation && (
              <div className="rounded border border-[var(--qd-border)] p-3">
                <div className="grid grid-cols-2 gap-3">
                  <Metric label="Confidence" value={`${testResult.signal_validation.confidence}%`} />
                  <Metric label="Threshold" value={`${testResult.signal_validation.threshold}%`} />
                  <Metric label="Trend" value={testResult.signal_validation.trend?.trend || "-"} />
                  <Metric label="RSI" value={testResult.signal_validation.trend?.rsi ?? "-"} />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
