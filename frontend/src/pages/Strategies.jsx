import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  Blocks,
  Bot,
  CheckCircle2,
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
import { Button } from "../components/ui/button";
import { PageHeader, StatusBadge as AppStatusBadge } from "../components/ui/app-shell";

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
  if (source.startsWith("mock-5minute")) return "Mock 5m";
  return source;
};

const filters = [
  { id: "all", label: "All Systems" },
  { id: "hft", label: "HFT / Upstox" },
  { id: "buying", label: "Option Buying" },
  { id: "selling", label: "Option Selling" },  { id: "live", label: "Live Auto-Traders" },
];

const noticeFor = (s) => {
  if (s.last_filter_reason) return { text: s.last_filter_reason, kind: "filter" };
  if (s.last_error?.startsWith("Signal filtered:")) return { text: s.last_error, kind: "filter" };
  if (s.last_error?.includes("entry blocked: cooldown-active")) return { text: "Entry skipped: cooldown active", kind: "filter" };
  if (s.last_error?.includes("entry blocked: duplicate-buy-dropped")) return { text: "Entry skipped: duplicate buy dropped", kind: "filter" };
  if (s.last_error?.includes("entry blocked: max-trades-day-reached")) return { text: "Entry skipped: max trades reached", kind: "filter" };
  if (s.last_error) return { text: s.last_error, kind: "error" };
  if (s.mode === "live" && s.last_data_source && !s.last_data_live) {
    return { text: `Data not fresh: ${s.last_data_reason || "waiting for current Upstox candle"}`, kind: "filter" };
  }
  return null;
};

export default function Strategies() {
  const [list, setList] = useState([]);
  const [scores, setScores] = useState({});
  const [testing, setTesting] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [selectedFilter, setSelectedFilter] = useState("all");
  const [loading, setLoading] = useState(false);
  const [sortBy, setSortBy] = useState("score");

  // Broker state
  const [upstoxStatus, setUpstoxStatus] = useState({ connected: false });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [strategiesRes, scoresRes, uRes] = await Promise.all([
        api.get("/strategies"),
        api.get("/ai/strategy-scores").catch(() => ({ data: { scores: [] } })),
        api.get("/upstox/status").catch(() => ({ data: { connected: false } })),
      ]);
      setList(strategiesRes.data || []);
      setScores(Object.fromEntries((scoresRes.data?.scores || []).map((row) => [row.strategy_id, row])));
      setUpstoxStatus(uRes.data || { connected: false });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const filtered = useMemo(() => {
    let result = [...list];
    if (selectedFilter === "hft") {
      result = list.filter((s) =>
        s.name?.toLowerCase().includes("upstox") ||
        s.name?.toLowerCase().includes("hft") ||
        s.description?.toLowerCase().includes("hft") ||
        s.description?.toLowerCase().includes("upstox")
      );
    } else if (selectedFilter === "buying") {
      result = list.filter((s) => s.strategy_type === "Option Buying");
    } else if (selectedFilter === "selling") {
      result = list.filter((s) => s.strategy_type === "Option Selling");    } else if (selectedFilter === "live") {
      result = list.filter((s) => s.status === "live");
    }

    // Apply sorting
    if (sortBy === "score") {
      result.sort((a, b) => {
        const scoreA = scores[a.id]?.score ?? a.ai_confidence_score ?? 0;
        const scoreB = scores[b.id]?.score ?? b.ai_confidence_score ?? 0;
        return scoreB - scoreA;
      });
    } else if (sortBy === "capital") {
      result.sort((a, b) => (a.required_capital ?? 0) - (b.required_capital ?? 0));
    } else if (sortBy === "signals") {
      result.sort((a, b) => (b.signals_fired ?? 0) - (a.signals_fired ?? 0));
    } else if (sortBy === "scans") {
      result.sort((a, b) => (b.evaluations ?? 0) - (a.evaluations ?? 0));
    }

    return result;
  }, [list, selectedFilter, sortBy, scores]);

  const counts = useMemo(() => ({
    all: list.length,
    hft: list.filter((s) =>
      s.name?.toLowerCase().includes("upstox") ||
      s.name?.toLowerCase().includes("hft") ||
      s.description?.toLowerCase().includes("hft") ||
      s.description?.toLowerCase().includes("upstox")
    ).length,
    buying: list.filter((s) => s.strategy_type === "Option Buying").length,
    selling: list.filter((s) => s.strategy_type === "Option Selling").length,    live: list.filter((s) => s.status === "live").length,
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
    <div className="space-y-3" data-testid="strategies-page">
      <PageHeader
        eyebrow="Strategy Catalog"
        title="Standardized Strategies"
        subtitle="Filterable option systems with explicit capital, strategy type, broker state, and live AI confidence."
        badge={<AppStatusBadge tone={upstoxStatus.connected ? "healthy" : "warning"}>{upstoxStatus.connected ? "Upstox Connected" : "Upstox Check"}</AppStatusBadge>}
        actions={
          <>
            <Button onClick={load} variant="outline" size="sm" data-testid="refresh-strategies">
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh
            </Button>
            <Button onClick={installPresets} variant="secondary" size="sm" data-testid="install-presets-btn">
              <Zap size={14} /> Presets
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to="/python" data-testid="new-python-btn"><Code2 size={14} /> Python</Link>
            </Button>
            <Button asChild variant="primary" size="sm">
              <Link to="/visual" data-testid="new-visual-btn"><Blocks size={14} /> Builder</Link>
            </Button>
            <Button asChild variant="secondary" size="sm">
              <Link to="/ai-bot" data-testid="ask-agent-btn"><Bot size={14} /> Ask AI</Link>
            </Button>
          </>
        }
      />

      <section className="qd-filter-bar flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-2 pr-1 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold">
            <Filter size={14} className="text-[var(--qd-accent)]" /> Categories
          </div>
          {filters.map((item) => (
            <button
              key={item.id}
              onClick={() => setSelectedFilter(item.id)}
              className={`rounded px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-wider transition-all duration-300 ${
                selectedFilter === item.id
                  ? "qd-force-white border border-[var(--qd-accent)] bg-[var(--qd-accent)] shadow-sm"
                  : "border border-[var(--qd-border)] bg-white/70 text-[var(--qd-text-2)] hover:border-[var(--qd-border-strong)] hover:text-[var(--qd-text)]"
              }`}
              data-testid={`filter-${item.id}`}
            >
              {item.label} <span className="text-[var(--qd-text-3)] font-bold">({counts[item.id]})</span>
            </button>
          ))}
        </div>

        <div className="flex items-center gap-3">
          <span className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold">Sort By</span>
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="cursor-pointer rounded border border-[var(--qd-border)] bg-white/85 px-3 py-1.5 font-mono text-xs text-[var(--qd-text)] outline-none transition-all hover:border-[var(--qd-border-strong)]"
            data-testid="sort-selector"
          >
            <option value="score">AI Confidence Score</option>
            <option value="capital">Capital Required</option>
            <option value="signals">Signals Fired</option>
            <option value="scans">Scans Count</option>
          </select>
        </div>
      </section>

      {!list.length ? (
        <div className="qd-card mx-auto max-w-4xl p-8 md:p-12">
          <div className="text-center relative z-10">
            <Zap className="mx-auto mb-4 text-[var(--qd-accent)]" size={42} />
            <h2 className="mb-2 font-head text-2xl font-bold text-[var(--qd-text)]">No Option Strategies Found</h2>
            <p className="text-sm text-[var(--qd-text-2)] max-w-lg mx-auto mb-8">
              Initialize your QuantG terminal with HFT and low-latency option-trading presets tailored specifically for Upstox.
            </p>
            
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-left mb-8">
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-white/70 p-4 transition-all hover:border-[var(--qd-border-strong)]">
                <div className="flex items-center gap-2 mb-1">
                  <Zap size={14} className="text-orange-400" />
                  <span className="font-mono text-xs font-semibold text-[var(--qd-text)]">Upstox HFT Low-Latency Scalper</span>
                </div>
                <p className="text-xs text-[var(--qd-text-3)] leading-relaxed">
                  Tick-based EMA crossover and ATR compression setups with direct order routing to `api-hft.upstox.com`.
                </p>
              </div>
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-white/70 p-4 transition-all hover:border-[var(--qd-border-strong)]">
                <div className="flex items-center gap-2 mb-1">
                  <Zap size={14} className="text-orange-400" />
                  <span className="font-mono text-xs font-semibold text-[var(--qd-text)]">Upstox HFT Multi-Leg Neutral Straddle</span>
                </div>
                <p className="text-xs text-[var(--qd-text-3)] leading-relaxed">
                  Options multi-leg delta-neutral entry/exit preset utilizing historical price band squeezes.
                </p>
              </div>
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-white/70 p-4 transition-all hover:border-[var(--qd-border-strong)]">
                <div className="flex items-center gap-2 mb-1">
                  <Zap size={14} className="text-indigo-400" />
                  <span className="font-mono text-xs font-semibold text-[var(--qd-text)]">Bank Nifty Volatility Breakout HFT</span>
                </div>
                <p className="text-xs text-[var(--qd-text-3)] leading-relaxed">
                  Fast Bank Nifty breakout model monitoring sudden volume surges and ATR threshold violations.
                </p>
              </div>
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-white/70 p-4 transition-all hover:border-[var(--qd-border-strong)]">
                <div className="flex items-center gap-2 mb-1">
                  <Zap size={14} className="text-cyan-400" />
                  <span className="font-mono text-xs font-semibold text-[var(--qd-text)]">NIFTY Low-Latency Scalper</span>
                </div>
                <p className="text-xs text-[var(--qd-text-3)] leading-relaxed">
                  High-frequency options buying relying on short-term VWAP standard deviation envelope breakouts.
                </p>
              </div>
            </div>
            
            <button
              onClick={installPresets}
              className="qd-force-white inline-flex items-center gap-2 rounded bg-[var(--qd-accent)] px-6 py-3 font-mono text-sm font-semibold uppercase tracking-wider shadow-lg transition-all hover:bg-[var(--qd-accent-hover)] active:scale-95"
            >
              <Zap size={16} /> Seed Default Presets
            </button>
          </div>
        </div>
      ) : !filtered.length ? (
        <div className="qd-card p-16 text-center">
          <Filter className="mx-auto mb-3 text-[var(--qd-text-3)]" />
          <p className="text-sm text-[var(--qd-text-2)]">No strategies match this filter.</p>
        </div>
      ) : (
        <div className="space-y-2">
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
              load={load}
              upstoxStatus={upstoxStatus}
            />
          ))}
        </div>
      )}

      {testResult && <TestResultModal testResult={testResult} onClose={() => setTestResult(null)} />}
    </div>
  );
}

function StrategyCard({ s, score, testing, toggle, del, testRun, manualOrder, exitAll, load, upstoxStatus }) {
  const live = s.status === "live";
  const paused = s.status === "paused";
  const notice = noticeFor(s);
  const editPath = s.kind === "python" ? `/python?id=${s.id}` : `/visual?id=${s.id}`;
  const scoreValue = score?.score ?? s.ai_confidence_score ?? 0;  
  const risk = s.visual_config?.risk || {};
  
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({
    target_pct: risk.target_pct ?? "",
    stoploss_pct: risk.stoploss_pct ?? "",
    trailing_sl_enabled: risk.trailing_sl_enabled ?? false,
    trail_trigger_pct: risk.trail_trigger_pct ?? "",
    trail_step_pct: risk.trail_step_pct ?? "",
    cooldown_minutes: risk.cooldown_minutes ?? "",
    max_trades_day: risk.max_trades_day ?? "",
    daily_loss_limit: risk.daily_loss_limit ?? "",
    required_capital: s.required_capital ?? risk.required_capital ?? "",
    time_exit_minutes: risk.time_exit_minutes ?? "",
    indicator_exit_enabled: risk.indicator_exit_enabled ?? false,
    exit_mode: risk.exit_mode ?? "SQUARE_OFF",
    broker: s.broker ?? "upstox",
    mode: s.mode ?? "paper",
  });

  useEffect(() => {
    const freshRisk = s.visual_config?.risk || {};
    setForm({
      target_pct: freshRisk.target_pct ?? "",
      stoploss_pct: freshRisk.stoploss_pct ?? "",
      trailing_sl_enabled: freshRisk.trailing_sl_enabled ?? false,
      trail_trigger_pct: freshRisk.trail_trigger_pct ?? "",
      trail_step_pct: freshRisk.trail_step_pct ?? "",
      cooldown_minutes: freshRisk.cooldown_minutes ?? "",
      max_trades_day: freshRisk.max_trades_day ?? "",
      daily_loss_limit: freshRisk.daily_loss_limit ?? "",
      required_capital: s.required_capital ?? freshRisk.required_capital ?? "",
      time_exit_minutes: freshRisk.time_exit_minutes ?? "",
      indicator_exit_enabled: freshRisk.indicator_exit_enabled ?? false,
      exit_mode: freshRisk.exit_mode ?? "SQUARE_OFF",
      broker: s.broker ?? "upstox",
      mode: s.mode ?? "paper",
    });
  }, [s]);

  const saveSettings = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const payload = {
        target_pct: form.target_pct !== "" ? parseFloat(form.target_pct) : null,
        stoploss_pct: form.stoploss_pct !== "" ? parseFloat(form.stoploss_pct) : null,
        trailing_sl_enabled: !!form.trailing_sl_enabled,
        trail_trigger_pct: form.trail_trigger_pct !== "" ? parseFloat(form.trail_trigger_pct) : null,
        trail_step_pct: form.trail_step_pct !== "" ? parseFloat(form.trail_step_pct) : null,
        cooldown_minutes: form.cooldown_minutes !== "" ? parseInt(form.cooldown_minutes) : null,
        max_trades_day: form.max_trades_day !== "" ? parseInt(form.max_trades_day) : null,
        daily_loss_limit: form.daily_loss_limit !== "" ? parseFloat(form.daily_loss_limit) : null,
        required_capital: form.required_capital !== "" ? parseFloat(form.required_capital) : null,
        time_exit_minutes: form.time_exit_minutes !== "" ? parseInt(form.time_exit_minutes) : null,
        indicator_exit_enabled: !!form.indicator_exit_enabled,
        exit_mode: form.exit_mode,
        broker: form.broker,
        mode: form.mode,
      };
      await api.put(`/strategies/${s.id}/runtime-settings`, payload);
      toast.success("Strategy risk settings synced successfully");
      if (load) load();
    } catch (err) {
      toast.error(err.response?.data?.detail || "Failed to save risk settings");
    } finally {
      setSaving(false);
    }
  };

  const isHft = s.name?.toLowerCase().includes("hft") || s.name?.toLowerCase().includes("upstox") || s.description?.toLowerCase().includes("hft") || s.description?.toLowerCase().includes("upstox");

  const speedLabel = () => {
    if (s.name?.toLowerCase().includes("hft") || s.description?.toLowerCase().includes("hft")) {
      return "⚡ HFT (1-Sec Tick)";
    }
    if (s.name?.toLowerCase().includes("scalper") || s.description?.toLowerCase().includes("scalper")) {
      return "⚡ Scalper (30-Sec)";
    }
    if (s.last_data_source && s.last_data_source.includes("5minute")) {
      return "⏱️ Intraday (5-Min)";
    }
    return "📈 Swing (Daily)";
  };

  const getBrokerStatus = () => {
    if (s.name?.toLowerCase().includes("upstox") || s.description?.toLowerCase().includes("upstox")) {
      return { name: "Upstox HFT", connected: upstoxStatus?.connected };
    }
    return { name: "Upstox HFT", connected: upstoxStatus?.connected };
  };

  const broker = getBrokerStatus();

  return (
    <article 
      className={`qd-card qd-strategy-card flex flex-col p-3 transition-all duration-300 relative overflow-hidden ${
        isHft 
          ? "border-[var(--qd-border-strong)]"
          : "hover:border-[var(--qd-border-strong)]"
      }`} 
      data-testid={`strategy-${s.id}`}
    >
      {false && isHft && (
        <div className="absolute top-0 right-0 w-32 h-32 bg-gradient-to-bl from-indigo-500/10 via-cyan-500/5 to-transparent pointer-events-none rounded-bl-full animate-pulse" />
      )}
      
      <div className="flex flex-col gap-3 relative z-10 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          {isHft && (
            <div className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded border border-amber-500/35 bg-amber-500/10 text-[var(--qd-warn)] font-mono text-[9px] uppercase tracking-wider font-semibold mb-1 w-max">
              <Zap size={9} /> Upstox
            </div>
          )}
          <div className="qd-section-title flex items-center gap-1.5">
            <span>{s.instrument_group || "NSE"} / {s.kind}</span>
            <span className="w-1 h-1 rounded-full bg-[var(--qd-text-3)]" />
            <span className="text-[var(--qd-text-2)]">{speedLabel()}</span>
          </div>
          <h2 className="mt-1 truncate font-head text-base font-semibold text-[var(--qd-text)]">{s.name}</h2>
          <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
            <span className={`px-2 py-0.5 rounded text-[8px] font-mono font-bold uppercase tracking-wider ${s.mode === "live" ? "bg-rose-500/10 border border-rose-500/30 text-[var(--qd-loss)] animate-pulse" : "bg-cyan-500/10 border border-cyan-500/30 text-[var(--qd-cyan)]"}`}>
              {s.mode === "live" ? "PRODUCTION LIVE" : "PAPER SIMULATED"}
            </span>
            <span className="px-2 py-0.5 rounded text-[8px] font-mono font-bold uppercase tracking-wider bg-white/70 border border-[var(--qd-border)] text-[var(--qd-text)]">
              BROKER: {s.broker?.replace("_", " ") || "UPSTOX"}
            </span>
          </div>
        </div>
        <div className="grid min-w-[280px] grid-cols-3 gap-2 rounded border border-[var(--qd-border)] bg-white/70 p-2 sm:grid-cols-6 lg:grid-cols-3 xl:grid-cols-6">
          <Metric label="Capital" value={money(s.required_capital)} compact />
          <Metric label="Type" value={s.strategy_type || "Option Buying"} compact />
          <Metric label="Score" value={scoreValue ? `${scoreValue}%` : "-"} compact />
          <Metric label="Scans" value={s.evaluations ?? 0} compact />
          <Metric label="Orders" value={s.signals_fired ?? 0} compact tone={(s.signals_fired ?? 0) > 0 ? "text-[var(--qd-profit)]" : ""} />
          <Metric label="Last" value={timeAgo(s.last_evaluated_at)} compact />
        </div>
        <div className="flex flex-wrap items-center gap-2 lg:justify-end">
          <StatusBadge status={s.status} />
          <div className="flex items-center gap-1.5 font-mono text-[9px] text-[var(--qd-text-3)]">
            <span>{broker.name}</span>
            <span className={`w-1.5 h-1.5 rounded-full transition-all duration-300 ${broker.connected ? "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)] animate-pulse" : "bg-rose-500 shadow-[0_0_6px_rgba(244,63,94,0.8)]"}`} />
            <span className="text-[8px] tracking-wider uppercase">{broker.connected ? "online" : "offline"}</span>
          </div>
        </div>
      </div>

      <p className="hidden">{s.description || "No description"}</p>

      <div className="hidden">
        <Metric label="Total Capital Required" value={money(s.required_capital)} />
        <Metric label="Strategy Type" value={s.strategy_type || "Option Buying"} />
        <Metric label="Asset" value={s.asset_class || "equity"} />
        <Metric label="Data" value={sourceLabel(s.last_data_source)} tone={s.last_data_live ? "text-[var(--qd-profit)]" : ""} />
        <div className="col-span-2 border-t border-[rgba(255,255,255,0.06)] pt-2.5 mt-0.5">
          <Metric label="AI Market Suitability" value={s.market_suitability || "Any Market Condition"} tone="text-indigo-400 font-bold font-mono" />
        </div>
      </div>

      <div className="hidden">
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

      <div className="hidden">
        <Metric label="Scans" value={s.evaluations ?? 0} compact />
        <Metric label="Orders" value={s.signals_fired ?? 0} compact tone={(s.signals_fired ?? 0) > 0 ? "text-[var(--qd-profit)]" : ""} />
        <Metric label="Last Signals" value={s.last_signals_count ?? 0} compact tone={(s.last_signals_count ?? 0) > 0 ? "text-[var(--qd-profit)]" : ""} />
        <Metric label="Last Scan" value={timeAgo(s.last_evaluated_at)} compact />
      </div>

      {notice && (
        <div className={`mt-2 rounded border px-3 py-2 text-xs ${
          notice.kind === "filter"
            ? "border-[rgba(255,159,10,0.35)] text-[var(--qd-warn)]"
            : "border-[rgba(255,59,48,0.35)] text-[var(--qd-loss)]"
        }`}>
          {notice.text}
        </div>
      )}

      {/* EXPANDABLE RISK & EXIT SETTINGS */}
      <div className="mt-2 border-t border-[rgba(255,255,255,0.06)] pt-2">
        <button 
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center justify-between py-1.5 px-2 bg-white/70 border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] rounded font-mono text-[10px] uppercase tracking-wide text-[var(--qd-text)] transition-all active:scale-[0.99]"
        >
          <span className="flex items-center gap-1.5">
            <Shield size={12} className="text-indigo-400" /> 
            Risk & Exit Bounds
          </span>
          <span className="text-indigo-400 font-bold">{expanded ? "Hide ▲" : "Configure ▼"}</span>
        </button>
        
        {expanded && (
          <form onSubmit={saveSettings} className="mt-3 bg-white/75 border border-[var(--qd-border)] rounded-md p-3.5 space-y-3.5">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Target profit (TP %)</label>
                <input 
                  type="number" 
                  step="0.01" 
                  value={form.target_pct} 
                  onChange={(e) => setForm({ ...form, target_pct: e.target.value })}
                  placeholder="e.g. 2.5" 
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] focus:border-indigo-500 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Stop loss (SL %)</label>
                <input 
                  type="number" 
                  step="0.01" 
                  value={form.stoploss_pct} 
                  onChange={(e) => setForm({ ...form, stoploss_pct: e.target.value })}
                  placeholder="e.g. 1.2" 
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] focus:border-indigo-500 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>
            </div>

            <div className="rounded-md border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2.5 space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-2)]">Trailing Stop Loss</span>
                <input 
                  type="checkbox" 
                  checked={form.trailing_sl_enabled} 
                  onChange={(e) => setForm({ ...form, trailing_sl_enabled: e.target.checked })}
                  className="w-3.5 h-3.5 accent-indigo-500 bg-[var(--qd-surface-2)] border-[var(--qd-border)] cursor-pointer"
                />
              </div>
              
              {form.trailing_sl_enabled && (
                <div className="grid grid-cols-2 gap-2 pt-1">
                  <div>
                    <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Trail Trigger %</label>
                    <input 
                      type="number" 
                      step="0.01" 
                      value={form.trail_trigger_pct} 
                      onChange={(e) => setForm({ ...form, trail_trigger_pct: e.target.value })}
                      placeholder="e.g. 1.0" 
                      className="w-full bg-[var(--qd-surface-3)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                    />
                  </div>
                  <div>
                    <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Trail Step %</label>
                    <input 
                      type="number" 
                      step="0.01" 
                      value={form.trail_step_pct} 
                      onChange={(e) => setForm({ ...form, trail_step_pct: e.target.value })}
                      placeholder="e.g. 0.2" 
                      className="w-full bg-[var(--qd-surface-3)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                    />
                  </div>
                </div>
              )}
            </div>

            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Cooldown (Min)</label>
                <input 
                  type="number" 
                  value={form.cooldown_minutes} 
                  onChange={(e) => setForm({ ...form, cooldown_minutes: e.target.value })}
                  placeholder="30" 
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                />
              </div>
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Max Trades/Day</label>
                <input 
                  type="number" 
                  value={form.max_trades_day} 
                  onChange={(e) => setForm({ ...form, max_trades_day: e.target.value })}
                  placeholder="5" 
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                />
              </div>
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Loss Limit (INR)</label>
                <input 
                  type="number" 
                  value={form.daily_loss_limit} 
                  onChange={(e) => setForm({ ...form, daily_loss_limit: e.target.value })}
                  placeholder="5000" 
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 border-t border-white/5 pt-2.5">
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Time Exit (Min)</label>
                <input 
                  type="number" 
                  value={form.time_exit_minutes} 
                  onChange={(e) => setForm({ ...form, time_exit_minutes: e.target.value })}
                  placeholder="360" 
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white"
                />
              </div>
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Exit Mode</label>
                <select 
                  value={form.exit_mode} 
                  onChange={(e) => setForm({ ...form, exit_mode: e.target.value })}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  <option value="SQUARE_OFF">SQUARE OFF</option>
                  <option value="REVERSE">REVERSE</option>
                  <option value="NONE">NONE</option>
                </select>
              </div>
            </div>

            {/* Broker & Mode deployment configuration overrides */}
            <div className="grid grid-cols-2 gap-2 border-t border-white/5 pt-2.5">
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Execution Broker</label>
                <select 
                  value={form.broker} 
                  onChange={(e) => setForm({ ...form, broker: e.target.value })}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  <option value="upstox">Upstox (HFT Enabled)</option>
                </select>
              </div>
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Deployment Mode</label>
                <select 
                  value={form.mode} 
                  onChange={(e) => setForm({ ...form, mode: e.target.value })}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  <option value="paper">Paper Trading (Simulated)</option>
                  <option value="live">Live Trading (Production)</option>
                </select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 pt-1">
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Allocated Capital (INR)</label>
                <input 
                  type="number" 
                  value={form.required_capital} 
                  onChange={(e) => setForm({ ...form, required_capital: e.target.value })}
                  placeholder="50000" 
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                />
              </div>
              <div className="flex flex-col justify-end pb-1.5">
                <div className="flex items-center justify-between px-1">
                  <span className="font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)]">Indicator Exit</span>
                  <input 
                    type="checkbox" 
                    checked={form.indicator_exit_enabled} 
                    onChange={(e) => setForm({ ...form, indicator_exit_enabled: e.target.checked })}
                    className="w-3.5 h-3.5 accent-indigo-500 cursor-pointer"
                  />
                </div>
              </div>
            </div>

            <button 
              type="submit" 
              disabled={saving}
              className="w-full py-2 bg-gradient-to-r from-indigo-600 to-blue-600 hover:from-indigo-500 hover:to-blue-500 text-white font-mono text-[10px] uppercase font-bold tracking-wider rounded border border-indigo-400/20 shadow-md active:scale-95 transition-all flex items-center justify-center gap-1.5 disabled:opacity-50"
            >
              {saving ? (
                <RefreshCw size={12} className="animate-spin" />
              ) : (
                <CheckCircle2 size={12} />
              )}
              {saving ? "Syncing Bounds..." : "Sync Risk Bounds"}
            </button>
          </form>
        )}
      </div>

      <div className="mt-2">
        <div className="grid grid-cols-1 gap-2 sm:hidden">
          <button
            onClick={() => exitAll(s.id)}
            className="flex items-center justify-center gap-2 rounded border border-[var(--qd-warn)] text-[var(--qd-warn)] hover:bg-[var(--qd-warn)] hover:text-black py-2.5 font-mono text-xs font-bold uppercase tracking-wider transition-all"
            data-testid={`exit-all-${s.id}`}
          >
            <Shield size={14} /> Square Off / Exit Position
          </button>
        </div>
        <div className="mt-2 grid grid-cols-[1fr_1fr_1fr_auto_auto] gap-2">
          <button
            onClick={() => exitAll(s.id)}
            className="hidden items-center justify-center gap-2 rounded border border-[var(--qd-warn)] px-3 py-2 font-mono text-xs font-bold uppercase tracking-wider text-[var(--qd-warn)] transition-all hover:bg-[var(--qd-warn)] hover:text-black sm:flex"
            data-testid={`exit-all-${s.id}`}
          >
            <Shield size={13} /> Exit
          </button>
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
