import React, { useCallback, useMemo, useState } from "react";
import { usePolling } from "../hooks/usePolling";
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

import StrategyCard from "../components/strategies/StrategyCard";
import AboutStrategyModal from "../components/strategies/AboutStrategyModal";

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
  const [marginEstimates, setMarginEstimates] = useState({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [strategiesRes, scoresRes, uRes, marginRes] = await Promise.all([
        api.get("/strategies"),
        api.get("/ai/strategy-scores").catch(() => ({ data: { scores: [] } })),
        api.get("/upstox/status").catch(() => ({ data: { connected: false } })),
        api.get("/strategies/margin-estimates").catch(() => ({ data: { estimates: {} } })),
      ]);
      setList(strategiesRes.data || []);
      setScores(Object.fromEntries((scoresRes.data?.scores || []).map((row) => [row.strategy_id, row])));
      setUpstoxStatus(uRes.data || { connected: false });
      setMarginEstimates(marginRes.data?.estimates || {});
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(load, 30000, { hiddenMs: 0 });

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
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
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
              marginEstimate={marginEstimates[s.id]}
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


