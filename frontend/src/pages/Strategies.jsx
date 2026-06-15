import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  Blocks,
  Bot,
  CheckCircle2,
  Code2,
  Filter,
  HelpCircle,
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
import { reasonLabel } from "../lib/reasonLabels";

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

const noticeFor = (s) => {
  if (s.last_filter_reason) return { text: reasonLabel(s.last_filter_reason).label, kind: "filter" };
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
  const [searchQuery, setSearchQuery] = useState("");
  const [aboutStrategy, setAboutStrategy] = useState(null);
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
    
    // Apply search filter
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter(
        (s) =>
          s.name?.toLowerCase().includes(q) ||
          s.description?.toLowerCase().includes(q) ||
          s.strategy_type?.toLowerCase().includes(q) ||
          s.instrument_group?.toLowerCase().includes(q)
      );
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
  }, [list, searchQuery, sortBy, scores]);

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

      <section className="qd-filter-bar flex flex-col gap-3 md:flex-row md:items-center md:justify-between p-3.5">
        <div className="relative flex-1 max-w-md w-full">
          <span className="absolute inset-y-0 left-0 flex items-center pl-3 pointer-events-none text-[var(--qd-text-3)]">
            <Filter size={16} />
          </span>
          <input
            type="text"
            placeholder="Search strategies by name or description..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-8 py-2 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] rounded text-xs text-[var(--qd-text)] outline-none placeholder-[var(--qd-text-3)] font-sans"
            data-testid="strategy-search-input"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery("")}
              className="absolute inset-y-0 right-0 flex items-center pr-3 text-[var(--qd-text-3)] hover:text-[var(--qd-text)]"
            >
              <X size={14} />
            </button>
          )}
        </div>

        <div className="flex items-center gap-3">
          <span className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold">Sort By</span>
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="cursor-pointer rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-1.5 font-mono text-xs text-[var(--qd-text)] outline-none transition-all hover:border-[var(--qd-border-strong)]"
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
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-4 transition-all hover:border-[var(--qd-border-strong)]">
                <div className="flex items-center gap-2 mb-1">
                  <Zap size={14} className="text-orange-400" />
                  <span className="font-mono text-xs font-semibold text-[var(--qd-text)]">Upstox HFT Low-Latency Scalper</span>
                </div>
                <p className="text-xs text-[var(--qd-text-3)] leading-relaxed">
                  Tick-based EMA crossover and ATR compression setups with direct order routing to `api-hft.upstox.com`.
                </p>
              </div>
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-4 transition-all hover:border-[var(--qd-border-strong)]">
                <div className="flex items-center gap-2 mb-1">
                  <Zap size={14} className="text-orange-400" />
                  <span className="font-mono text-xs font-semibold text-[var(--qd-text)]">Upstox HFT Multi-Leg Neutral Straddle</span>
                </div>
                <p className="text-xs text-[var(--qd-text-3)] leading-relaxed">
                  Options multi-leg delta-neutral entry/exit preset utilizing historical price band squeezes.
                </p>
              </div>
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-4 transition-all hover:border-[var(--qd-border-strong)]">
                <div className="flex items-center gap-2 mb-1">
                  <Zap size={14} className="text-indigo-400" />
                  <span className="font-mono text-xs font-semibold text-[var(--qd-text)]">Bank Nifty Volatility Breakout HFT</span>
                </div>
                <p className="text-xs text-[var(--qd-text-3)] leading-relaxed">
                  Fast Bank Nifty breakout model monitoring sudden volume surges and ATR threshold violations.
                </p>
              </div>
              <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-4 transition-all hover:border-[var(--qd-border-strong)]">
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
        <div className="qd-card p-16 text-center space-y-3">
          <Filter className="mx-auto mb-3 text-[var(--qd-text-3)]" />
          <p className="text-sm text-[var(--qd-text-2)]">No strategies match this search query.</p>
          <button
            onClick={() => setSearchQuery("")}
            className="inline-flex items-center gap-2 rounded border border-[var(--qd-border)] px-4 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-accent)] transition-colors"
          >
            Clear Search
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          {filtered.map((s) => (
            <StrategyCard
              key={s.id}
              s={s}
              score={scores[s.id]}
              toggle={toggle}
              del={del}
              onAbout={setAboutStrategy}
              manualOrder={manualOrder}
              exitAll={exitAll}
              load={load}
              upstoxStatus={upstoxStatus}
            />
          ))}
        </div>
      )}

      {aboutStrategy && (
        <AboutStrategyModal
          s={aboutStrategy}
          score={scores[aboutStrategy.id]}
          testing={testing}
          testResult={testResult}
          testRun={testRun}
          onClose={() => {
            setAboutStrategy(null);
            setTestResult(null);
          }}
        />
      )}
    </div>
  );
}

function StrategyCard({ s, score, toggle, del, onAbout, manualOrder, exitAll, load, upstoxStatus }) {
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
    strategy_category: risk.strategy_category ?? "intraday",
    broker: s.broker ?? "upstox",
    mode: s.mode ?? "paper",
    product: s.visual_config?.options?.product ?? s.product ?? "MIS",
    structure: s.visual_config?.options?.structure ?? "single_leg",
    spread_width: s.visual_config?.options?.spread_width ?? 2,
    short_delta: s.visual_config?.options?.short_delta ?? 0.30,
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
      strategy_category: freshRisk.strategy_category ?? "intraday",
      broker: s.broker ?? "upstox",
      mode: s.mode ?? "paper",
      product: s.visual_config?.options?.product ?? s.product ?? "MIS",
      structure: s.visual_config?.options?.structure ?? "single_leg",
      spread_width: s.visual_config?.options?.spread_width ?? 2,
      short_delta: s.visual_config?.options?.short_delta ?? 0.30,
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
        strategy_category: form.strategy_category || null,
        broker: form.broker,
        mode: form.mode,
        product: form.product,
        structure: form.structure,
        spread_width: (form.spread_width !== "" && form.spread_width != null) ? parseInt(form.spread_width) : null,
        short_delta: (form.short_delta !== "" && form.short_delta != null) ? parseFloat(form.short_delta) : null,
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
            <span className="px-2 py-0.5 rounded text-[8px] font-mono font-bold uppercase tracking-wider bg-[var(--qd-surface-2)] border border-[var(--qd-border)] text-[var(--qd-text)]">
              BROKER: {s.broker?.replace("_", " ") || "UPSTOX"}
            </span>
          </div>
        </div>
        <div className="grid min-w-[280px] grid-cols-3 gap-2 rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2 sm:grid-cols-6 lg:grid-cols-3 xl:grid-cols-6">
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
      <div className="mt-2 border-t border-[var(--qd-border)] pt-2">
        <button 
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center justify-between py-1.5 px-2 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] rounded font-mono text-[10px] uppercase tracking-wide text-[var(--qd-text)] transition-all active:scale-[0.99]"
        >
          <span className="flex items-center gap-1.5">
            <Shield size={12} className="text-indigo-400" /> 
            Risk & Exit Bounds
          </span>
          <span className="text-indigo-400 font-bold">{expanded ? "Hide ▲" : "Configure ▼"}</span>
        </button>
        
        {expanded && (
          <form onSubmit={saveSettings} className="mt-3 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-md p-3.5 space-y-3.5">
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

            <div className="border-t border-[var(--qd-border)] pt-2.5">
              <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Strategy Category</label>
              <select
                value={form.strategy_category}
                onChange={(e) => {
                  const cat = e.target.value;
                  if (cat === "scalper") {
                    // Apply scalper frequency preset so it can actually scalp.
                    setForm({ ...form, strategy_category: cat, cooldown_minutes: "1", max_trades_day: "20", daily_loss_limit: "3000" });
                  } else {
                    setForm({ ...form, strategy_category: cat });
                  }
                }}
                className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
              >
                <option value="scalper">SCALPER — high frequency (cooldown ≤ 3m, ≥ 5 trades/day)</option>
                <option value="intraday">INTRADAY — moderate frequency</option>
                <option value="swing">SWING — low frequency</option>
              </select>
              {form.strategy_category === "scalper" && (parseInt(form.cooldown_minutes) > 3 || parseInt(form.max_trades_day) < 5) && (
                <p className="mt-1 text-[10px] text-amber-400">
                  ⚠ Scalper limits will be auto-corrected on save (cooldown capped at 3m, min 5 trades/day).
                </p>
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

            <div className="grid grid-cols-2 gap-2 border-t border-[var(--qd-border)] pt-2.5">
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
            <div className="grid grid-cols-3 gap-2 border-t border-[var(--qd-border)] pt-2.5">
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
              <div>
                <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Product Type</label>
                <select 
                  value={form.product} 
                  onChange={(e) => setForm({ ...form, product: e.target.value })}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  <option value="MIS">MIS (Intraday)</option>
                  <option value="CNC">CNC (Delivery)</option>
                  <option value="NRML">NRML (Normal)</option>
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

            {/* Phase 2 #5: credit-spread structure (options strategies only) */}
            {s.visual_config?.options?.enabled && (
              <div className="grid grid-cols-3 gap-2 pt-1">
                <div>
                  <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Structure</label>
                  <select
                    value={form.structure}
                    onChange={(e) => setForm({ ...form, structure: e.target.value })}
                    className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    data-testid="settings-structure"
                  >
                    <option value="single_leg">Single Leg</option>
                    <option value="credit_spread">Credit Spread</option>
                  </select>
                </div>
                {form.structure === "credit_spread" && (
                  <>
                    <div>
                      <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Short Δ</label>
                      <input
                        type="number" step="0.05" min="0.05" max="0.95"
                        value={form.short_delta}
                        onChange={(e) => setForm({ ...form, short_delta: e.target.value })}
                        className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                        data-testid="settings-short-delta"
                      />
                    </div>
                    <div>
                      <label className="block font-mono text-[8px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Width (strikes)</label>
                      <input
                        type="number" min="1" max="20"
                        value={form.spread_width}
                        onChange={(e) => setForm({ ...form, spread_width: e.target.value })}
                        className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                        data-testid="settings-spread-width"
                      />
                    </div>
                  </>
                )}
              </div>
            )}

            <button
              type="submit"
              disabled={saving}
              className="w-full py-2 bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-[var(--qd-accent-contrast)] font-mono text-[10px] uppercase font-bold tracking-wider rounded border border-[var(--qd-border-strong)] shadow-md active:scale-95 transition-all flex items-center justify-center gap-1.5 disabled:opacity-50"
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
          <button
            onClick={() => onAbout(s)}
            className="flex items-center justify-center gap-2 rounded border border-[var(--qd-accent)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:bg-[var(--qd-accent)] hover:text-white"
            data-testid={`about-${s.id}`}
          >
            <HelpCircle size={13} /> About
          </button>
          <button onClick={() => toggle(s.id)} className="flex items-center justify-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text)] hover:border-[var(--qd-border-strong)]" data-testid={`toggle-${s.id}`}>
            {live ? <><Pause size={13} /> Pause</> : <><Play size={13} /> {paused ? "Resume" : "Live"}</>}
          </button>
          <Link to={editPath} className="rounded border border-[var(--qd-border)] px-3 py-3 text-center font-mono text-xs uppercase tracking-wider text-[var(--qd-text)] hover:border-[var(--qd-border-strong)]" data-testid={`edit-${s.id}`}>
            Edit
          </Link>
          <button onClick={() => del(s.id)} className="flex h-11 w-11 items-center justify-center rounded border border-[var(--qd-border)] text-[var(--qd-loss)] hover:border-[var(--qd-loss)]" data-testid={`delete-${s.id}`} aria-label="Delete strategy">
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

function AboutStrategyModal({ s, score, testing, testResult, testRun, onClose }) {
  const live = s.status === "live";
  const paused = s.status === "paused";
  const risk = s.visual_config?.risk || {};
  const options = s.visual_config?.options || {};
  
  // AI score values
  const aiScore = score?.score ?? s.ai_confidence_score;
  const aiReason = score?.reason ?? s.ai_confidence_reason;
  const marketInfo = score?.market;

  // Split reasons
  const telemetryReasons = aiReason ? aiReason.split(";").map(r => r.trim()).filter(Boolean) : [];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4 overflow-y-auto" onClick={onClose} data-testid="about-strategy-modal">
      <div className="qd-card w-full max-w-4xl max-h-[90vh] overflow-y-auto p-6 md:p-8 flex flex-col gap-6" onClick={(e) => e.stopPropagation()}>
        
        {/* Header */}
        <div className="flex items-start justify-between border-b border-[var(--qd-border)] pb-4">
          <div>
            <div className="qd-section-title flex items-center gap-1.5">
              <span>{s.instrument_group || "NSE"} / {s.kind}</span>
              <span className="w-1 h-1 rounded-full bg-[var(--qd-text-3)]" />
              <span>{s.strategy_type || "Option Buying"}</span>
            </div>
            <h2 className="mt-1 font-head text-2xl font-bold text-white">{s.name}</h2>
            <div className="flex items-center gap-2 mt-2">
              <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider ${s.mode === "live" ? "bg-rose-500/10 border border-rose-500/30 text-[var(--qd-loss)]" : "bg-cyan-500/10 border border-cyan-500/30 text-[var(--qd-cyan)]"}`}>
                {s.mode === "live" ? "LIVE PRODUCTION" : "PAPER SIMULATED"}
              </span>
              <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider ${live ? "bg-emerald-500/10 border border-emerald-500/30 text-[var(--qd-profit)]" : "bg-amber-500/10 border border-amber-500/30 text-[var(--qd-warn)]"}`}>
                {s.status?.toUpperCase() || "DRAFT"}
              </span>
            </div>
          </div>
          <button onClick={onClose} className="text-[var(--qd-text-3)] hover:text-white transition-colors" aria-label="Close modal">
            <X size={22} />
          </button>
        </div>

        {/* Responsive Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 min-h-0 overflow-y-visible">
          
          {/* Left Column: Info & AI Telemetry */}
          <div className="space-y-6">
            {/* Description */}
            <div className="space-y-2">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold">Strategy Description</h3>
              <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-lg p-4 text-sm text-[var(--qd-text)] leading-relaxed">
                {s.description || "No description provided for this strategy. Configure descriptions in the builder or editor."}
              </div>
            </div>

            {/* Suitability Badge */}
            <div className="flex items-center gap-3">
              <span className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-3)]">Market Suitability:</span>
              <span className="px-3 py-1 rounded-full bg-[var(--qd-surface-3)] border border-[var(--qd-border-strong)] text-xs text-white font-semibold">
                {s.market_suitability || "Any Market Condition"}
              </span>
            </div>

            {/* AI Telemetry System */}
            <div className="space-y-3 border-t border-[var(--qd-border)] pt-4">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold flex items-center gap-1.5">
                <Bot size={14} className="text-[var(--qd-accent)]" /> Live AI Telemetry & Confidence
              </h3>
              
              {aiScore != null ? (
                <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border-strong)] rounded-lg p-4 space-y-4">
                  {/* Gauge */}
                  <div className="flex items-center justify-between gap-4">
                    <div className="space-y-1">
                      <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">AI Confidence Score</span>
                      <div className="font-head text-3xl font-bold text-white flex items-baseline gap-1">
                        {aiScore}%
                        <span className="text-xs text-[var(--qd-text-3)] font-mono">confidence</span>
                      </div>
                    </div>
                    {/* Progress Bar (mini gauge) */}
                    <div className="w-24 bg-[var(--qd-surface-3)] rounded-full h-3 overflow-hidden border border-[var(--qd-border)]">
                      <div 
                        className={`h-full rounded-full transition-all duration-500 ${
                          aiScore >= 70 ? "bg-[var(--qd-profit)]" : aiScore >= 40 ? "bg-[var(--qd-accent)]" : "bg-[var(--qd-loss)]"
                        }`}
                        style={{ width: `${aiScore}%` }}
                      />
                    </div>
                  </div>

                  {/* Reasons split */}
                  {telemetryReasons.length > 0 && (
                    <div className="space-y-2">
                      <div className="font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]">Telemetry Signals Checked</div>
                      <div className="flex flex-wrap gap-1.5">
                        {telemetryReasons.map((reasonText, idx) => {
                          const isWarning = reasonText.toLowerCase().includes("error") || reasonText.toLowerCase().includes("paused");
                          return (
                            <span 
                              key={idx} 
                              className={`px-2 py-0.5 rounded text-[10px] font-mono flex items-center gap-1 border ${
                                isWarning 
                                  ? "bg-rose-500/10 border-rose-500/30 text-[var(--qd-loss)]" 
                                  : "bg-emerald-500/10 border-emerald-500/30 text-[var(--qd-profit)]"
                              }`}
                            >
                              {isWarning ? "⚠" : "✓"} {reasonText}
                            </span>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* Market Quote */}
                  {marketInfo && (
                    <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <span className="block font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">Spot Reference</span>
                        <span className="font-mono font-semibold text-white">
                          {score.symbol}: {money(marketInfo.price)}
                        </span>
                      </div>
                      <div>
                        <span className="block font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">24h Change</span>
                        <span className={`font-mono font-semibold ${marketInfo.pct >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                          {marketInfo.pct >= 0 ? "+" : ""}{marketInfo.pct?.toFixed(2)}%
                        </span>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-lg p-4 text-xs text-[var(--qd-text-3)] text-center">
                  No live AI telemetry computed yet. Save runtime limits and enable the strategy to start telemetry scoring.
                </div>
              )}
            </div>
          </div>

          {/* Right Column: Spec & Exits & Relocated Backtest */}
          <div className="space-y-6">
            {/* Specs & Risk Bounds */}
            <div className="space-y-3">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold">Trading Specs & Exit Parameters</h3>
              <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-lg p-4 space-y-3 text-xs">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Allocated Capital</span>
                    <strong className="block text-sm text-white font-mono mt-0.5">{money(s.required_capital)}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Product / Class</span>
                    <strong className="block text-sm text-white font-mono mt-0.5">{risk.product || s.product || "MIS"} / {s.asset_class?.toUpperCase() || "OPTIONS"}</strong>
                  </div>
                </div>

                {options.enabled && (
                  <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-3 gap-2">
                    <div>
                      <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Structure</span>
                      <strong className="block text-white font-mono mt-0.5">{options.structure === "credit_spread" ? "Credit Spread" : "Single Leg"}</strong>
                    </div>
                    {options.structure === "credit_spread" && (
                      <>
                        <div>
                          <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Short Δ</span>
                          <strong className="block text-white font-mono mt-0.5">{options.short_delta ?? 0.3}</strong>
                        </div>
                        <div>
                          <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Width (Strikes)</span>
                          <strong className="block text-white font-mono mt-0.5">{options.spread_width ?? 2}</strong>
                        </div>
                      </>
                    )}
                  </div>
                )}

                <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-3 gap-2">
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Target Profit</span>
                    <strong className="block text-white font-mono mt-0.5">{risk.target_pct != null ? `${risk.target_pct}%` : "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Stop Loss</span>
                    <strong className="block text-white font-mono mt-0.5">{risk.stoploss_pct != null ? `${risk.stoploss_pct}%` : "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Trailing SL</span>
                    <strong className="block text-white font-mono mt-0.5">{risk.trailing_sl_enabled ? `Yes (Trig: ${risk.trail_trigger_pct}%)` : "Disabled"}</strong>
                  </div>
                </div>

                <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-3 gap-2">
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Cooldown</span>
                    <strong className="block text-white font-mono mt-0.5">{risk.cooldown_minutes ? `${risk.cooldown_minutes}m` : "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Max Trades/Day</span>
                    <strong className="block text-white font-mono mt-0.5">{risk.max_trades_day ?? "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[9px]">Daily Loss Limit</span>
                    <strong className="block text-white font-mono mt-0.5">{risk.daily_loss_limit ? money(risk.daily_loss_limit) : "None"}</strong>
                  </div>
                </div>
              </div>
            </div>

            {/* Relocated Performance Test Section */}
            <div className="space-y-3 border-t border-[var(--qd-border)] pt-4">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold flex items-center gap-1.5">
                <Activity size={14} className="text-[var(--qd-accent)]" /> Performance & Backtest Test
              </h3>
              
              <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-lg p-4 space-y-4">
                <div className="flex items-center justify-between gap-4">
                  <p className="text-xs text-[var(--qd-text-3)] max-w-[200px]">
                    Run a 60-day historical analysis on index data to verify this strategy's profitability and metrics.
                  </p>
                  <Button 
                    onClick={() => testRun(s.id)} 
                    disabled={testing === s.id} 
                    variant="outline" 
                    size="sm"
                    className="flex-shrink-0"
                  >
                    {testing === s.id ? (
                      <>
                        <RefreshCw size={13} className="animate-spin" /> Running...
                      </>
                    ) : (
                      <>
                        <Activity size={13} /> Run Performance Test
                      </>
                    )}
                  </Button>
                </div>

                {/* Backtest Test Results rendering inline */}
                {testResult && (
                  <div className="border-t border-[var(--qd-border)] pt-3 space-y-3">
                    {testResult.error ? (
                      <div className="rounded border border-[var(--qd-loss)] bg-[rgba(255,59,48,0.1)] p-3 text-xs text-[var(--qd-loss)] font-mono font-bold">
                        {testResult.error}
                      </div>
                    ) : testResult.ok ? (
                      <div className="space-y-3 text-xs">
                        <div className="grid grid-cols-3 gap-2">
                          <Metric label="Total P&L" value={money(testResult.summary?.total_pnl)} tone={(testResult.summary?.total_pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"} compact />
                          <Metric label="Return" value={`${testResult.summary?.return_pct?.toFixed(2) || 0}%`} compact />
                          <Metric label="Win Rate" value={`${testResult.summary?.win_rate?.toFixed(1) || 0}%`} compact />
                        </div>
                        <div className="grid grid-cols-3 gap-2 pt-1 border-t border-[rgba(255,255,255,0.05)]">
                          <Metric label="Trades" value={testResult.summary?.trades || 0} compact />
                          <Metric label="Wins" value={testResult.summary?.wins || 0} tone="text-[var(--qd-profit)]" compact />
                          <Metric label="Losses" value={testResult.summary?.losses || 0} tone="text-[var(--qd-loss)]" compact />
                        </div>
                        <div className="text-[10px] text-[var(--qd-text-3)] font-mono flex justify-between">
                          <span>Data: {testResult.data_source || "-"}</span>
                          <span>Ref: {testResult.symbol_analysed || "-"}</span>
                        </div>
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
            </div>

          </div>

        </div>

      </div>
    </div>
  );
}
