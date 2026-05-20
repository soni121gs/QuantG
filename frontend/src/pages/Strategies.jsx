import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import {
  Code2,
  Blocks,
  Play,
  Pause,
  Trash2,
  Plus,
  RefreshCw,
  Activity,
  Zap,
  X,
  TrendingUp,
  TrendingDown,
  LogOut,
  Filter,
} from "lucide-react";
import { toast } from "sonner";

const timeAgo = (iso) => {
  if (!iso) return "—";
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};

const formatDataSource = (source) => {
  if (!source) return "—";
  if (source.startsWith("zerodha-kite-5minute")) return source.includes("tick-live") ? "REAL ticker 5m" : "REAL Kite 5m";
  if (source.startsWith("zerodha-kite-day")) return "REAL Kite day";
  if (source.startsWith("mock-5minute")) return "MOCK 5m";
  if (source.startsWith("mock-day")) return "MOCK day";
  return source;
};

const strategyNotice = (s) => {
  if (s.last_filter_reason) return { text: s.last_filter_reason, kind: "filter" };
  if (s.last_error?.startsWith("Signal filtered:")) return { text: s.last_error, kind: "filter" };
  if (s.last_error) return { text: s.last_error, kind: "error" };
  return null;
};

const ASSET_CLASSES = [
  { id: "all", label: "All Strategies", icon: "📊" },
  { id: "equity", label: "Equity", icon: "📈" },
  { id: "options", label: "Options", icon: "⚡" },
  { id: "futures", label: "Futures", icon: "🎯" },
];

const getAssetClass = (strategy) => {
  if (strategy.asset_class) return strategy.asset_class;
  if (strategy.visual_config?.options?.enabled) return "options";
  return "equity";
};

export default function Strategies() {
  const [list, setList] = useState([]);
  const [testing, setTesting] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [selectedAsset, setSelectedAsset] = useState("all");

  const load = useCallback(() => api.get("/strategies").then((r) => setList(r.data)), []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

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
    if (!window.confirm(`${verb} now using this strategy's symbol & default qty?\n\nThis bypasses the python logic.`)) return;
    try {
      const r = await api.post(`/strategies/${id}/manual-order`, { action });
      const o = r.data?.order || {};
      toast.success(`${action} placed: ${o.qty} ${o.symbol} @ ₹${o.price}`);
      load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || `${verb} failed`);
    }
  };

  const exitAll = async (id) => {
    if (!window.confirm("Square off ALL open positions from this strategy?")) return;
    try {
      const r = await api.post(`/strategies/${id}/exit-all`);
      const closed = r.data?.closed_positions || [];
      if (closed.length === 0) {
        toast.info("No open positions to close from this strategy");
      } else {
        const ok = closed.filter((c) => c.status === "ok").length;
        const fail = closed.filter((c) => c.status !== "ok").length;
        toast.success(`Closed ${ok} position${ok !== 1 ? "s" : ""}${fail ? ` (${fail} failed)` : ""}`);
      }
      load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Exit failed");
    }
  };

  // Filter strategies by asset class
  const filteredList = selectedAsset === "all" ? list : list.filter((s) => getAssetClass(s) === selectedAsset);

  // Group by status
  const groups = [
    {
      key: "live",
      title: "Live Strategies",
      subtitle: "Automation is running and can place trades",
      tone: "live",
      empty: "No strategies are trading right now.",
      items: filteredList.filter((s) => s.status === "live"),
    },
    {
      key: "paused",
      title: "Paused Strategies",
      subtitle: "Automation is stopped; manual controls still work",
      tone: "paused",
      empty: "No paused strategies.",
      items: filteredList.filter((s) => s.status === "paused"),
    },
    {
      key: "active",
      title: "Active Strategies",
      subtitle: "Saved and ready, but not trading until you go live",
      tone: "active",
      empty: "No active draft strategies.",
      items: filteredList.filter((s) => !["live", "paused"].includes(s.status)),
    },
  ];

  const assetCounts = {
    all: list.length,
    equity: list.filter((s) => getAssetClass(s) === "equity").length,
    options: list.filter((s) => getAssetClass(s) === "options").length,
    futures: list.filter((s) => getAssetClass(s) === "futures").length,
  };

  return (
    <div className="space-y-4" data-testid="strategies-page">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// STRATEGIES</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1">My Strategies</h1>
          <p className="text-xs text-[var(--qd-text-2)] mt-1 font-mono flex items-center gap-1">
            <Activity size={11} />
            Scanner ticks every 30s · Page auto-refreshes
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={load} className="border border-[var(--qd-border)] hover:border-white text-[var(--qd-text-2)] text-xs font-mono uppercase tracking-wider px-3 py-2 rounded-sm flex items-center gap-2" data-testid="refresh-strategies">
            <RefreshCw size={14} />
          </button>
          <Link to="/python" className="border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2" data-testid="new-python-btn">
            <Code2 size={14} /> Python
          </Link>
          <Link to="/visual" className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2" data-testid="new-visual-btn">
            <Blocks size={14} /> Visual Builder
          </Link>
        </div>
      </div>

      {/* Asset Class Filter */}
      <div className="qd-card p-3 flex flex-wrap gap-2">
        <div className="flex items-center gap-2 text-xs text-[var(--qd-text-2)] font-mono uppercase tracking-wider">
          <Filter size={12} /> Filter:
        </div>
        {ASSET_CLASSES.map((ac) => (
          <button
            key={ac.id}
            onClick={() => setSelectedAsset(ac.id)}
            className={`text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded-sm border transition-colors ${
              selectedAsset === ac.id
                ? "bg-[var(--qd-accent)] border-[var(--qd-accent)] text-white"
                : "border-[var(--qd-border)] hover:border-white text-[var(--qd-text-2)] hover:text-white"
            }`}
            data-testid={`filter-${ac.id}`}
          >
            {ac.icon} {ac.label} <span className="text-[10px] text-[var(--qd-text-3)] ml-1">({assetCounts[ac.id]})</span>
          </button>
        ))}
      </div>

      {filteredList.length === 0 && selectedAsset === "all" ? (
        <div className="qd-card p-16 text-center">
          <Plus className="mx-auto text-[var(--qd-text-3)] mb-3" />
          <p className="font-mono text-sm text-[var(--qd-text-2)]">No strategies yet. Build your first one.</p>
        </div>
      ) : filteredList.length === 0 ? (
        <div className="qd-card p-16 text-center">
          <Filter className="mx-auto text-[var(--qd-text-3)] mb-3" />
          <p className="font-mono text-sm text-[var(--qd-text-2)]">No {selectedAsset} strategies yet.</p>
        </div>
      ) : (
        <div className="space-y-5">
          {groups.map((g) => (
            <StrategySection key={g.key} title={g.title} subtitle={g.subtitle} tone={g.tone} count={g.items.length} empty={g.empty}>
              {g.items.map((s) => (
                <StrategyCard
                  key={s.id}
                  s={s}
                  testing={testing}
                  toggle={toggle}
                  del={del}
                  testRun={testRun}
                  manualOrder={manualOrder}
                  exitAll={exitAll}
                />
              ))}
            </StrategySection>
          ))}
        </div>
      )}

      {testResult && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setTestResult(null)} data-testid="test-result-modal">
          <div className="qd-card max-w-2xl w-full max-h-[85vh] overflow-y-auto p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between mb-4">
              <div>
                <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// TEST RUN RESULTS</div>
                <h2 className="font-head text-xl text-white mt-1">{testResult.symbol || "Strategy"}</h2>
              </div>
              <button onClick={() => setTestResult(null)} className="text-[var(--qd-text-2)] hover:text-white" data-testid="close-test-modal">
                <X size={18} />
              </button>
            </div>

            {testResult.error && (
              <div className="bg-[rgba(255,59,48,0.1)] border border-[var(--qd-loss)] text-[var(--qd-loss)] text-xs font-mono p-3 rounded-sm mb-3">
                ! {testResult.error}
              </div>
            )}

            {testResult.ok && (
              <div className="space-y-4 text-xs font-mono">
                {/* Summary Stats */}
                <div className="grid grid-cols-3 gap-2 bg-[var(--qd-surface-2)] p-3 rounded-sm">
                  <Cell k="Total P&L" v={`₹${testResult.summary?.total_pnl?.toLocaleString("en-IN") || 0}`} tone={(testResult.summary?.total_pnl || 0) >= 0 ? "p" : "l"} />
                  <Cell k="Return %" v={`${testResult.summary?.return_pct?.toFixed(2) || 0}%`} tone={(testResult.summary?.return_pct || 0) >= 0 ? "p" : "l"} />
                  <Cell k="Win Rate" v={`${testResult.summary?.win_rate?.toFixed(1) || 0}%`} />
                  <Cell k="Trades" v={testResult.summary?.trades || 0} />
                  <Cell k="Wins" v={testResult.summary?.wins || 0} tone="p" />
                  <Cell k="Losses" v={testResult.summary?.losses || 0} tone="l" />
                </div>

                {/* Data Source */}
                <div className="border-t border-[var(--qd-border)] pt-3">
                  <div className="text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-2">DATA SOURCE</div>
                  <div className="grid grid-cols-2 gap-3">
                    <Row k="Source" v={testResult.data_source} />
                    <Row k="Candles" v={testResult.last_5_closes?.length || 0} />
                    <Row k="First candle" v={testResult.first_candle?.date?.slice(0, 10)} />
                    <Row k="Last candle" v={testResult.last_candle?.date?.slice(0, 10)} />
                  </div>
                </div>

                {/* Price Movement */}
                {testResult.last_5_closes?.length > 0 && (
                  <div className="border-t border-[var(--qd-border)] pt-3">
                    <div className="text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-2">LAST 5 CLOSES</div>
                    <div className="text-white flex items-center justify-center gap-2">
                      {testResult.last_5_closes.map((p, i) => (
                        <div key={i} className="text-center">
                          <div className="text-[11px] text-white">₹{p}</div>
                          {i < testResult.last_5_closes.length - 1 && (
                            testResult.last_5_closes[i + 1] > p ? (
                              <TrendingUp size={12} className="text-[var(--qd-profit)] mx-auto mt-0.5" />
                            ) : (
                              <TrendingDown size={12} className="text-[var(--qd-loss)] mx-auto mt-0.5" />
                            )
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Signals */}
                {testResult.signals?.length > 0 && (
                  <div className="border-t border-[var(--qd-border)] pt-3">
                    <div className="text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-2">SIGNALS ({testResult.signals.length})</div>
                    <div className="space-y-1 max-h-[150px] overflow-y-auto">
                      {testResult.signals.slice(-5).map((sig, i) => (
                        <div key={i} className={`px-2 py-1 rounded-sm text-[10px] font-mono ${sig.action === "BUY" ? "bg-[var(--qd-profit)]/20 text-[var(--qd-profit)]" : "bg-[var(--qd-loss)]/20 text-[var(--qd-loss)]"}`}>
                          {sig.action} @ {sig.date?.slice(0, 16)}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {testResult.signal_validation && (
                  <div className="border-t border-[var(--qd-border)] pt-3">
                    <div className="text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-2">SIGNAL QUALITY</div>
                    <div className={`p-3 rounded-sm border ${
                      testResult.signal_validation.is_valid
                        ? "border-[var(--qd-profit)] bg-[rgba(0,230,118,0.08)]"
                        : "border-[var(--qd-warn)] bg-[rgba(255,159,10,0.08)]"
                    }`}>
                      <div className="grid grid-cols-2 gap-2 mb-2">
                        <Row k="Confidence" v={`${testResult.signal_validation.confidence}%`} />
                        <Row k="Threshold" v={`${testResult.signal_validation.threshold}%`} />
                        <Row k="Trend" v={testResult.signal_validation.trend?.trend || "-"} />
                        <Row k="RSI" v={testResult.signal_validation.trend?.rsi ?? "-"} />
                      </div>
                      {(testResult.signal_validation.reasons || []).map((reason, i) => (
                        <div key={i} className="text-[11px] text-[var(--qd-text-2)]">- {reason}</div>
                      ))}
                    </div>
                  </div>
                )}

                {testResult.order_error && (
                  <div className="bg-[rgba(255,159,10,0.08)] border border-[var(--qd-warn)] text-[var(--qd-warn)] text-[11px] p-3 rounded-sm">
                    {testResult.order_error}
                  </div>
                )}

                {/* No Signals Warning */}
                {testResult.signals?.length === 0 && (
                  <div className="bg-[var(--qd-surface-2)] p-3 rounded-sm text-[var(--qd-text-2)] text-[11px] border-l-2 border-[var(--qd-warn)]">
                    No signals generated on this data. Your strategy conditions didn't match the current market state. Try again later or adjust your strategy logic.
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function StrategyCard({ s, testing, toggle, del, testRun, manualOrder, exitAll }) {
  const live = s.status === "live";
  const paused = s.status === "paused";
  const displayStatus = live ? "live" : paused ? "paused" : "active";
  const assetLabel = {
    equity: "📈 EQUITY",
    options: "⚡ OPTIONS",
    futures: "🎯 FUTURES",
  }[getAssetClass(s)];
  const lastEvalSec = s.last_evaluated_at ? Math.floor((Date.now() - new Date(s.last_evaluated_at).getTime()) / 1000) : null;
  const scanningHot = live && lastEvalSec !== null && lastEvalSec < 60;

  return (
    <div className="qd-card p-4 flex flex-col gap-3" data-testid={`strategy-${s.id}`}>
      <div className="flex items-start justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{s.kind === "python" ? "// PYTHON" : "// VISUAL"} · {assetLabel}</div>
          <h3 className="font-head text-lg text-white mt-1">{s.name}</h3>
        </div>
        <div className="flex flex-col items-end gap-1">
          <span className={`font-mono text-[10px] uppercase px-2 py-1 rounded-sm ${
            live ? "bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]" :
            paused ? "bg-[rgba(255,159,10,0.1)] text-[var(--qd-warn)]" :
            "bg-[rgba(0,122,255,0.1)] text-[var(--qd-accent)]"
          }`}>{displayStatus}</span>
          {live && (
            <span className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)] flex items-center gap-1">
              <span className={`w-1.5 h-1.5 rounded-full ${scanningHot ? "bg-[var(--qd-profit)] animate-pulse" : "bg-[var(--qd-text-3)]"}`} />
              {scanningHot ? "SCANNING" : "IDLE"}
            </span>
          )}
        </div>
      </div>
      <p className="text-xs text-[var(--qd-text-2)] line-clamp-2 min-h-[2.5rem]">{s.description || "No description"}</p>

      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] font-mono pt-2 border-t border-[var(--qd-border)]">
        <Cell k="Scans" v={s.evaluations ?? 0} />
        <Cell k="Signals" v={s.signals_fired ?? 0} tone={(s.signals_fired ?? 0) > 0 ? "p" : ""} />
        <Cell k="Last scan" v={timeAgo(s.last_evaluated_at)} />
        <Cell k="Last signal" v={s.last_signal_action || "—"} tone={s.last_signal_action === "BUY" ? "p" : s.last_signal_action === "SELL" ? "l" : ""} />
        <Cell k="Last PnL" v={s.last_pnl != null ? `₹${s.last_pnl.toLocaleString("en-IN")}` : "—"} tone={(s.last_pnl ?? 0) >= 0 ? "p" : "l"} />
        <Cell k="Data" v={formatDataSource(s.last_data_source)} tone={s.last_data_live ? "p" : s.last_data_source ? "l" : ""} />
      </div>

      {strategyNotice(s) && (
        <div className={`text-[10px] font-mono px-2 py-1 rounded-sm break-all ${
          strategyNotice(s).kind === "filter"
            ? "text-[var(--qd-warn)] bg-[rgba(255,176,32,0.08)]"
            : "text-[var(--qd-loss)] bg-[rgba(255,59,48,0.08)]"
        }`}>
          {strategyNotice(s).kind === "filter" ? "FILTER" : "ERROR"} {strategyNotice(s).text}
        </div>
      )}

      <div className="pt-2 border-t border-[var(--qd-border)] space-y-2">
        <div className="flex gap-2">
          <button onClick={() => manualOrder(s.id, "BUY")} className="flex-1 border border-[var(--qd-profit)] hover:bg-[var(--qd-profit)] hover:text-black text-[var(--qd-profit)] text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1" data-testid={`manual-buy-${s.id}`} title="Place an immediate BUY order">
            <TrendingUp size={12} /> Buy
          </button>
          <button onClick={() => manualOrder(s.id, "SELL")} className="flex-1 border border-[var(--qd-loss)] hover:bg-[var(--qd-loss)] hover:text-white text-[var(--qd-loss)] text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1" data-testid={`manual-sell-${s.id}`} title="Place an immediate SELL order">
            <TrendingDown size={12} /> Sell
          </button>
          <button onClick={() => exitAll(s.id)} className="flex-1 border border-[var(--qd-warn)] hover:bg-[var(--qd-warn)] hover:text-black text-[var(--qd-warn)] text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1" data-testid={`exit-all-${s.id}`} title="Square off all open positions from this strategy">
            <LogOut size={12} /> Exit
          </button>
        </div>

        <div className="flex gap-2">
          <button onClick={() => testRun(s.id)} disabled={testing === s.id} className="flex-1 border border-[var(--qd-accent)] hover:bg-[var(--qd-accent)] hover:text-white text-[var(--qd-accent)] text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1 disabled:opacity-50" data-testid={`test-run-${s.id}`}>
            <Zap size={12} /> {testing === s.id ? "Running..." : "Test Run"}
          </button>
          <button onClick={() => toggle(s.id)} className="flex-1 border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1" data-testid={`toggle-${s.id}`}>
            {live ? <><Pause size={12} /> Pause</> : <><Play size={12} /> Go Live</>}
          </button>
          <Link to={s.kind === "python" ? `/python?id=${s.id}` : `/visual?id=${s.id}`} className="border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase py-1.5 px-3 rounded-sm" data-testid={`edit-${s.id}`}>Edit</Link>
          <button onClick={() => del(s.id)} className="border border-[var(--qd-border)] hover:border-[var(--qd-loss)] text-[var(--qd-loss)] py-1.5 px-2 rounded-sm" data-testid={`delete-${s.id}`}>
            <Trash2 size={12} />
          </button>
        </div>
      </div>
    </div>
  );
}

function StrategySection({ title, subtitle, count, tone, empty, children }) {
  const toneClass =
    tone === "live" ? "text-[var(--qd-profit)] border-[var(--qd-profit)] bg-[rgba(0,230,118,0.08)]" :
    tone === "paused" ? "text-[var(--qd-warn)] border-[var(--qd-warn)] bg-[rgba(255,159,10,0.08)]" :
    "text-[var(--qd-accent)] border-[var(--qd-accent)] bg-[rgba(0,122,255,0.08)]";

  return (
    <section className="space-y-3" data-testid={`strategy-section-${tone}`}>
      <div className="flex items-center justify-between gap-3 border-b border-[var(--qd-border)] pb-2">
        <div>
          <div className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">{subtitle}</div>
          <h2 className="font-head text-xl text-white mt-0.5">{title}</h2>
        </div>
        <span className={`font-mono text-xs px-2.5 py-1 rounded-sm border ${toneClass}`}>{count}</span>
      </div>
      {count === 0 ? (
        <div className="border border-dashed border-[var(--qd-border)] p-5 text-center text-xs font-mono text-[var(--qd-text-2)]">
          {empty}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">{children}</div>
      )}
    </section>
  );
}

const Row = ({ k, v, tone }) => (
  <div className="flex justify-between items-center">
    <span className="text-[var(--qd-text-3)] uppercase tracking-widest text-[9px]">{k}</span>
    <span className={`text-right ${tone === "p" ? "text-[var(--qd-profit)]" : tone === "l" ? "text-[var(--qd-loss)]" : "text-white"}`}>{v}</span>
  </div>
);

const Cell = ({ k, v, tone }) => (
  <>
    <span className="text-[var(--qd-text-3)] uppercase tracking-widest text-[9px]">{k}</span>
    <span className={`text-right ${
      tone === "p" ? "text-[var(--qd-profit)]" : tone === "l" ? "text-[var(--qd-loss)]" : "text-white"
    }`}>{v}</span>
  </>
);
