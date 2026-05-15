import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { Code2, Blocks, Play, Pause, Trash2, Plus, RefreshCw, Activity, Zap, X } from "lucide-react";

const timeAgo = (iso) => {
  if (!iso) return "—";
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};

export default function Strategies() {
  const [list, setList] = useState([]);
  const [testing, setTesting] = useState(null);   // strategy id currently testing
  const [testResult, setTestResult] = useState(null); // diagnostic modal data

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
          <button onClick={load} className="border border-[var(--qd-border)] hover:border-white text-[var(--qd-text-2)] text-xs font-mono uppercase tracking-wider px-3 py-2 rounded-sm flex items-center gap-2" data-testid="refresh-strategies"><RefreshCw size={14} /></button>
          <Link to="/python" className="border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2" data-testid="new-python-btn"><Code2 size={14} /> Python</Link>
          <Link to="/visual" className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2" data-testid="new-visual-btn"><Blocks size={14} /> Visual Builder</Link>
        </div>
      </div>

      {list.length === 0 ? (
        <div className="qd-card p-16 text-center">
          <Plus className="mx-auto text-[var(--qd-text-3)] mb-3" />
          <p className="font-mono text-sm text-[var(--qd-text-2)]">No strategies yet. Build your first one.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {list.map((s) => {
            const live = s.status === "live";
            const lastEvalSec = s.last_evaluated_at ? Math.floor((Date.now() - new Date(s.last_evaluated_at).getTime()) / 1000) : null;
            const scanningHot = live && lastEvalSec !== null && lastEvalSec < 60;
            return (
              <div key={s.id} className="qd-card p-4 flex flex-col gap-3" data-testid={`strategy-${s.id}`}>
                <div className="flex items-start justify-between">
                  <div>
                    <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{s.kind === "python" ? "// PYTHON" : "// VISUAL"}</div>
                    <h3 className="font-head text-lg text-white mt-1">{s.name}</h3>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <span className={`font-mono text-[10px] uppercase px-2 py-1 rounded-sm ${
                      live ? "bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]" :
                      s.status === "paused" ? "bg-[rgba(255,159,10,0.1)] text-[var(--qd-warn)]" :
                      "bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]"
                    }`}>{s.status}</span>
                    {live && (
                      <span className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)] flex items-center gap-1">
                        <span className={`w-1.5 h-1.5 rounded-full ${scanningHot ? "bg-[var(--qd-profit)] animate-pulse" : "bg-[var(--qd-text-3)]"}`} />
                        {scanningHot ? "SCANNING" : "IDLE"}
                      </span>
                    )}
                  </div>
                </div>
                <p className="text-xs text-[var(--qd-text-2)] line-clamp-2 min-h-[2.5rem]">{s.description || "No description"}</p>

                {/* Telemetry grid */}
                <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] font-mono pt-2 border-t border-[var(--qd-border)]">
                  <Cell k="Scans" v={s.evaluations ?? 0} />
                  <Cell k="Signals" v={s.signals_fired ?? 0} tone={(s.signals_fired ?? 0) > 0 ? "p" : ""} />
                  <Cell k="Last scan" v={timeAgo(s.last_evaluated_at)} />
                  <Cell k="Last signal" v={s.last_signal_action || "—"} tone={s.last_signal_action === "BUY" ? "p" : s.last_signal_action === "SELL" ? "l" : ""} />
                  <Cell k="Last PnL" v={s.last_pnl != null ? `₹${s.last_pnl.toLocaleString("en-IN")}` : "—"} tone={(s.last_pnl ?? 0) >= 0 ? "p" : "l"} />
                  <Cell k="Errors" v={s.last_error ? "yes" : "—"} tone={s.last_error ? "l" : ""} />
                </div>
                {s.last_error && (
                  <div className="text-[10px] font-mono text-[var(--qd-loss)] bg-[rgba(255,59,48,0.08)] px-2 py-1 rounded-sm break-all">
                    ! {s.last_error}
                  </div>
                )}

                <div className="flex gap-2 pt-2 border-t border-[var(--qd-border)]">
                  <button onClick={() => testRun(s.id)} disabled={testing === s.id} className="flex-1 border border-[var(--qd-accent)] hover:bg-[var(--qd-accent)] hover:text-white text-[var(--qd-accent)] text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1 disabled:opacity-50" data-testid={`test-run-${s.id}`}>
                    <Zap size={12} /> {testing === s.id ? "Running…" : "Test Run"}
                  </button>
                  <button onClick={() => toggle(s.id)} className="flex-1 border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1" data-testid={`toggle-${s.id}`}>
                    {live ? <><Pause size={12} /> Pause</> : <><Play size={12} /> Go Live</>}
                  </button>
                  <Link to={s.kind === "python" ? `/python?id=${s.id}` : `/visual?id=${s.id}`} className="border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase py-1.5 px-3 rounded-sm" data-testid={`edit-${s.id}`}>Edit</Link>
                  <button onClick={() => del(s.id)} className="border border-[var(--qd-border)] hover:border-[var(--qd-loss)] text-[var(--qd-loss)] py-1.5 px-2 rounded-sm" data-testid={`delete-${s.id}`}><Trash2 size={12} /></button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {testResult && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setTestResult(null)} data-testid="test-result-modal">
          <div className="qd-card max-w-2xl w-full max-h-[85vh] overflow-y-auto p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between mb-4">
              <div>
                <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// TEST RUN DIAGNOSTIC</div>
                <h2 className="font-head text-xl text-white mt-1">{testResult.symbol || "Strategy"}</h2>
              </div>
              <button onClick={() => setTestResult(null)} className="text-[var(--qd-text-2)] hover:text-white" data-testid="close-test-modal"><X size={18} /></button>
            </div>

            {testResult.error && (
              <div className="bg-[rgba(255,59,48,0.1)] border border-[var(--qd-loss)] text-[var(--qd-loss)] text-xs font-mono p-3 rounded-sm mb-3">
                ! {testResult.error}
              </div>
            )}

            {testResult.ok && (
              <div className="space-y-3 text-xs font-mono">
                <Row k="Data source" v={testResult.data_source} />
                <Row k="Candles fetched" v={testResult.candles} />
                <Row k="First candle" v={testResult.first_candle?.date} />
                <Row k="Last candle" v={`${testResult.last_candle?.date} @ ₹${testResult.last_candle?.close}`} />
                <Row k="Last 5 closes" v={(testResult.last_5_closes || []).join(" → ")} />
                <Row k="Signals returned" v={testResult.signals?.length ?? 0} tone={testResult.signals?.length ? "p" : "l"} />

                {testResult.signals?.length > 0 && (
                  <div className="bg-[var(--qd-surface-2)] p-3 rounded-sm">
                    <div className="text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-2">Latest signal</div>
                    <div className="text-white">{testResult.signals[testResult.signals.length - 1].action} @ {testResult.signals[testResult.signals.length - 1].date}</div>
                  </div>
                )}

                {testResult.order_placed && (
                  <div className="bg-[rgba(0,230,118,0.08)] border border-[var(--qd-profit)] p-3 rounded-sm">
                    <div className="text-[10px] uppercase tracking-widest text-[var(--qd-profit)] mb-1">✓ Order placed</div>
                    <div className="text-white text-[11px] break-all">{testResult.order_placed.side} {testResult.order_placed.qty} {testResult.order_placed.symbol} @ ₹{testResult.order_placed.price} — {testResult.order_placed.status}</div>
                  </div>
                )}

                {testResult.order_error && (
                  <div className="bg-[rgba(255,59,48,0.08)] border border-[var(--qd-loss)] p-3 rounded-sm">
                    <div className="text-[10px] uppercase tracking-widest text-[var(--qd-loss)] mb-1">Order rejected</div>
                    <div className="text-white text-[11px]">{testResult.order_error}</div>
                  </div>
                )}

                {testResult.signals?.length === 0 && (
                  <div className="bg-[var(--qd-surface-2)] p-3 rounded-sm text-[var(--qd-text-2)] text-[11px]">
                    Your <code className="text-white">run(data)</code> returned <b>0 signals</b> on this data. The strategy logic is fine — the current candles just don't match your BUY/SELL conditions. Try again in a few minutes (data refreshes every 5 min).
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

const Row = ({ k, v, tone }) => (
  <div className="flex justify-between items-center border-b border-[var(--qd-border)] pb-1">
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
