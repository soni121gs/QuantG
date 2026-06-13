import React, { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, BookOpen, HeartPulse, RefreshCw, ShieldCheck, Sparkles, SquareArrowOutUpRight } from "lucide-react";
import { api, formatINR } from "../lib/api";
import { APP_VERSION_LABEL } from "../lib/version";
import { toast } from "sonner";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";
import { useExecutionState } from "../hooks/useExecutionState";
import { renderMarkdown } from "../lib/markdown";

const BROKER_LABELS = { upstox: "Upstox" };

export default function MarketHub() {
  const { summary: executionSummary, refresh: refreshExecution } = useExecutionState({ pollMs: 15000 });
  const [health, setHealth] = useState(null);
  const [risk, setRisk] = useState(null);
  const [journal, setJournal] = useState(null);
  const [feed, setFeed] = useState(null);
  const [indicators, setIndicators] = useState(null);
  const [marketSession, setMarketSession] = useState(null);
  const [underlying, setUnderlying] = useState("NIFTY");
  const [busy, setBusy] = useState(false);
  const [analysis, setAnalysis] = useState(null);
  const [analysisBusy, setAnalysisBusy] = useState(false);

  const load = useCallback(async () => {
    const [h, r, j, f, ind, s] = await Promise.all([
      api.get("/broker/health").catch(() => ({ data: null })),
      api.get("/risk/dashboard").catch(() => ({ data: null })),
      api.get("/trade-journal").catch(() => ({ data: null })),
      api.get("/market/feed-comparison").catch(() => ({ data: null })),
      api.get(`/market/indicators/${underlying}`).catch(() => ({ data: null })),
      api.get("/market/session-status").catch(() => ({ data: null })),
    ]);
    if (h.data) setHealth(h.data);
    if (r.data) setRisk(r.data);
    if (j.data) setJournal(j.data);
    if (f.data) setFeed(f.data);
    if (ind.data) setIndicators(ind.data);
    if (s.data) setMarketSession(s.data);
  }, [underlying]);

  useEffect(() => {
    load().catch(() => {});
    const t = setInterval(() => load().catch(() => {}), 60000);
    return () => clearInterval(t);
  }, [load]);

  const squareOff = async () => {
    if (!window.confirm("Square off all open positions now?")) return;
    setBusy(true);
    try {
      const r = await api.post("/ops/squareoff-all");
      toast.success(`Square-off sent: ${r.data.closed?.length || 0} closed, ${r.data.failed?.length || 0} failed`);
      await load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Square-off failed");
    } finally {
      setBusy(false);
    }
  };

  const autoPickFeed = async () => {
    setBusy(true);
    try {
      const r = await api.post("/market/auto-data-broker");
      setFeed(r.data);
      await load();
      toast.success(`Data broker set to ${BROKER_LABELS[r.data.recommended_data_broker] || r.data.recommended_data_broker}`);
    } catch (e) {
      toast.error(e.response?.data?.detail || "No healthy feed to auto-pick yet");
    } finally {
      setBusy(false);
    }
  };

  const runAnalysis = async () => {
    setAnalysisBusy(true);
    try {
      const r = await api.get("/ai/market-analysis");
      setAnalysis(r.data);
    } catch (e) {
      setAnalysis({ content: `Error: ${e.response?.data?.detail || e.message}` });
    } finally {
      setAnalysisBusy(false);
    }
  };

  const brokers = health?.brokers || {
    upstox: health?.upstox,
  };
  const sessionPnl = executionSummary?.net_pnl ?? 0;

  return (
    <div className="space-y-4 max-w-7xl" data-testid="market-hub-page">
      <PageHeader
        eyebrow={`${APP_VERSION_LABEL} Control Room`}
        title="Market Hub"
        subtitle="Broker health, ticker quality, live signal stack, and daily risk in one market workspace."
        badge={<StatusBadge tone={(marketSession?.global_status === "OPEN" || risk?.market_open) ? "healthy" : "warning"}>{marketSession?.global_status || "Market"}</StatusBadge>}
        actions={
          <>
            <button onClick={() => { load(); refreshExecution(); }} className="border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] text-[var(--qd-text)] px-3 py-2 text-xs font-mono uppercase rounded-sm flex items-center gap-2">
              <RefreshCw size={14} /> Refresh
            </button>
            <button onClick={squareOff} disabled={busy} className="border border-[var(--qd-loss)] text-[var(--qd-loss)] hover:bg-[rgba(255,59,48,0.08)] px-3 py-2 text-xs font-mono uppercase rounded-sm flex items-center gap-2 disabled:opacity-60">
              {busy ? <RefreshCw size={14} className="animate-spin" /> : <SquareArrowOutUpRight size={14} />}
              {busy ? "Working..." : "Square Off All"}
            </button>
          </>
        }
      />

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Metric label="Mode" value={risk?.mode || "-"} tone={risk?.mode === "LIVE" ? "loss" : "warn"} />
        <Metric label="Market" value={marketSession?.global_status || (risk?.market_open ? "OPEN" : "CLOSED")} tone={(marketSession?.global_status === "OPEN" || risk?.market_open) ? "profit" : "warn"} />
        <Metric label="P&L Today" value={`INR ${formatINR(sessionPnl)}`} tone={sessionPnl >= 0 ? "profit" : "loss"} />
        <Metric label="Trades" value={`${risk?.trades_used ?? 0}/${risk?.max_trades_per_day ?? "-"}`} />
        <Metric label="Risk Left" value={risk?.loss_remaining == null ? "-" : `₹${formatINR(risk.loss_remaining)}`} tone="warn" />
      </div>

      {feed?.simulated_warning && (
        <div className="flex items-center gap-2 rounded border border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.08)] px-3 py-2 text-xs font-mono text-[var(--qd-loss)]">
          <AlertTriangle size={14} className="shrink-0" /> {feed.simulated_warning}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <section className="qd-card p-4">
          <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><HeartPulse size={16} /> Broker Health</h2>
          <div className="space-y-3">
            {Object.entries(brokers || {}).map(([key, value]) => (
              <BrokerRow key={key} name={BROKER_LABELS[key] || key} data={value || {}} active={health?.preferences?.execution_broker === key} />
            ))}
          </div>
          <div className="grid grid-cols-3 gap-2 mt-4">
            <Small label="Data" value={health?.preferences?.data_broker || "-"} />
            <Small label="Execution" value={health?.preferences?.execution_broker || "-"} />
            <Small label="Fallback" value={health?.preferences?.fallback_broker || "-"} />
          </div>
        </section>

        <section className="qd-card p-4 xl:col-span-2">
          <div className="flex items-start justify-between gap-3 mb-3">
            <h2 className="font-head text-lg text-white flex items-center gap-2"><Activity size={16} /> Ticker Quality</h2>
            <button onClick={autoPickFeed} disabled={busy} className="border border-[var(--qd-border)] hover:border-[var(--qd-profit)] text-[var(--qd-text)] px-3 py-2 text-xs font-mono uppercase rounded-sm disabled:opacity-60 flex items-center gap-2">
              {busy && <RefreshCw size={13} className="animate-spin" />}
              Auto-pick
            </button>
          </div>
          <div className="grid grid-cols-1 gap-3">
            <FeedCard name="Upstox" data={feed?.upstox} />
          </div>
          <div className="mt-3 text-xs font-mono text-[var(--qd-text-2)]">
            Recommended: <span className="text-[var(--qd-text)]">{BROKER_LABELS[feed?.recommended_data_broker] || feed?.recommended_data_broker || "-"}</span>
            <span className="text-[var(--qd-text-3)]"> · {feed?.reason || "Waiting for live ticks."}</span>
          </div>
        </section>

        <section className="qd-card p-4">
          <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><ShieldCheck size={16} /> Daily Risk</h2>
          <div className="space-y-2">
            <Row k="Daily loss limit" v={`₹${formatINR(risk?.daily_loss_limit || 0)}`} />
            <Row k="Open P&L" v={`₹${formatINR(risk?.open_pnl || 0)}`} />
            <Row k="Realised P&L" v={`₹${formatINR(risk?.realized_pnl || 0)}`} />
            <Row k="Per-strategy capital" v={`₹${formatINR(risk?.per_strategy_capital || 0)}`} />
            <Row k="Max position size" v={`₹${formatINR(risk?.max_position_size || 0)}`} />
          </div>
        </section>

        <section className="qd-card p-4 xl:col-span-2">
          <div className="flex items-start justify-between gap-3 mb-3">
            <h2 className="font-head text-lg text-white flex items-center gap-2"><Activity size={16} /> Signal Stack</h2>
            <select value={underlying} onChange={(e) => setUnderlying(e.target.value)} className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-xs text-white font-mono rounded-sm">
              {["NIFTY", "BANKNIFTY", "SENSEX"].map((u) => <option key={u} value={u}>{u}</option>)}
            </select>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Small label="Trend" value={indicators?.indicators?.trend || "-"} />
            <Small label="RSI" value={indicators?.indicators?.rsi ?? "-"} />
            <Small label="ATR %" value={indicators?.indicators?.atr_pct == null ? "-" : `${indicators.indicators.atr_pct}%`} />
            <Small label="VWAP Dist" value={indicators?.indicators?.vwap_distance_pct == null ? "-" : `${indicators.indicators.vwap_distance_pct}%`} />
            <Small label="HTF" value={indicators?.indicators?.higher_timeframe?.trend || "-"} />
            <Small label="Volume" value={indicators?.indicators?.volume_ratio == null ? "-" : `${indicators.indicators.volume_ratio}x`} />
            <Small label="Support" value={indicators?.indicators?.support ?? "-"} />
            <Small label="Resistance" value={indicators?.indicators?.resistance ?? "-"} />
          </div>
          <div className="mt-3 text-[10px] font-mono text-[var(--qd-text-3)]">
            {indicators?.available ? `${indicators.candles} candles · ${indicators.source} · ${indicators.is_live ? "REAL" : "not live"}` : indicators?.reason || "Loading indicators"}
          </div>
        </section>

        <section className="qd-card p-4">
          <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><BookOpen size={16} /> Trade Journal</h2>
          <div className="grid grid-cols-2 gap-2 mb-3">
            <Small label="Orders" value={journal?.summary?.orders ?? 0} />
            <Small label="Skipped" value={journal?.summary?.skipped_signals ?? 0} />
            <Small label="Win Rate" value={`${journal?.summary?.win_rate ?? 0}%`} />
          </div>
          <div className="space-y-2 max-h-52 overflow-y-auto pr-1">
            {(journal?.orders || []).slice(0, 8).map((o) => (
              <div key={o.id} className="border border-[var(--qd-border)] p-2 rounded-sm">
                <div className="flex justify-between gap-2 text-xs">
                  <span className="text-white font-mono">{o.symbol}</span>
                  <span className={o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>{o.side}</span>
                </div>
                <div className="text-[10px] font-mono text-[var(--qd-text-3)] mt-1">{o.status} · {o.source || "manual"} · ₹{formatINR(o.price || 0)}</div>
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="qd-card p-4">
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between border-b border-[var(--qd-border)]/50 pb-4 mb-4">
          <div>
            <h2 className="font-head text-lg text-white flex items-center gap-2"><Sparkles size={16} className="text-[var(--qd-accent)]" /> AI Market Brief</h2>
            <p className="mt-1 text-xs text-[var(--qd-text-2)]">Gemini evaluates live strategy scores, index trend structure, and Upstox feed state.</p>
          </div>
          <button
            onClick={runAnalysis}
            disabled={analysisBusy}
            className="border border-[var(--qd-accent)] text-[var(--qd-accent)] hover:bg-[var(--qd-accent)]/10 px-3 py-2 text-xs font-mono uppercase rounded-sm flex items-center gap-2 disabled:opacity-60"
          >
            {analysisBusy ? <RefreshCw size={13} className="animate-spin" /> : <Sparkles size={13} />}
            {analysisBusy ? "Analyzing..." : "Generate Brief"}
          </button>
        </div>
        {analysis?.content ? (
          <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-4 text-sm leading-relaxed text-[var(--qd-text-2)]">
            {renderMarkdown(analysis.content)}
          </div>
        ) : (
          <div className="text-xs font-mono text-[var(--qd-text-3)]">Generate a Gemini summary of current market structure and your strategy scores.</div>
        )}
      </section>

    </div>
  );
}

const Metric = ({ label, value, tone }) => (
  <div className="qd-card p-3">
    <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className={`font-head text-xl mt-1 ${tone === "profit" ? "text-[var(--qd-profit)]" : tone === "loss" ? "text-[var(--qd-loss)]" : tone === "warn" ? "text-[var(--qd-warn)]" : "text-white"}`}>{value}</div>
  </div>
);

const BrokerRow = ({ name, data, active }) => (
  <div className="border border-[var(--qd-border)] rounded-sm p-3">
    <div className="flex justify-between gap-2">
      <span className="text-white font-semibold flex items-center gap-2"><Activity size={14} /> {name}</span>
      <span className={`font-mono text-[10px] uppercase ${data.connected ? "text-[var(--qd-profit)]" : data.keys_saved ? "text-[var(--qd-warn)]" : "text-[var(--qd-text-3)]"}`}>
        {data.connected ? "connected" : data.keys_saved ? "keys saved" : "not setup"}
      </span>
    </div>
    <div className="text-[10px] font-mono text-[var(--qd-text-3)] mt-1">{active ? "execution broker" : data.reason || "-"}</div>
  </div>
);

const FeedCard = ({ name, data }) => (
  <div className={`border rounded-sm p-3 ${data?.healthy ? "border-[var(--qd-profit)]" : "border-[var(--qd-border)]"}`}>
    <div className="flex items-center justify-between gap-2">
      <span className="text-white font-semibold">{name}</span>
      <span className={`font-mono text-[10px] uppercase ${data?.healthy ? "text-[var(--qd-profit)]" : data?.connected ? "text-[var(--qd-warn)]" : "text-[var(--qd-text-3)]"}`}>
        {data?.healthy ? "fresh" : data?.connected ? "connected" : "offline"}
      </span>
    </div>
    <div className="grid grid-cols-3 gap-2 mt-3">
      <Small label="Age" value={data?.age_ms == null ? "-" : `${data.age_ms} ms`} />
      <Small label="Tokens" value={data?.subscribed_tokens ?? 0} />
      <Small label="Ticks" value={data?.ticks ?? (data?.last_tick_at ? "live" : 0)} />
    </div>
    <div className="text-[10px] font-mono text-[var(--qd-text-3)] mt-2 break-all">
      {data?.last_error || data?.last_tick_at || "-"}
    </div>
  </div>
);

const Small = ({ label, value }) => (
  <div className="border border-[var(--qd-border)] p-2 rounded-sm">
    <div className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className="text-sm text-white mt-1 break-all">{value}</div>
  </div>
);

const Row = ({ k, v }) => (
  <div className="flex justify-between gap-4 border-b border-[var(--qd-border)] pb-2">
    <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{k}</span>
    <span className="text-xs text-white text-right">{v}</span>
  </div>
);
