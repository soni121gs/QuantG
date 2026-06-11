import React, { useCallback, useEffect, useState } from "react";
import { Activity, BookOpen, HeartPulse, RefreshCw, ShieldCheck, SquareArrowOutUpRight, WalletCards } from "lucide-react";
import { api, formatINR } from "../lib/api";
import { APP_VERSION_LABEL } from "../lib/version";
import { toast } from "sonner";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";

const BROKER_LABELS = { upstox: "Upstox" };

export default function MarketHub() {
  const [health, setHealth] = useState(null);
  const [risk, setRisk] = useState(null);
  const [journal, setJournal] = useState(null);
  const [chain, setChain] = useState(null);
  const [comparison, setComparison] = useState(null);
  const [feed, setFeed] = useState(null);
  const [indicators, setIndicators] = useState(null);
  const [marketSession, setMarketSession] = useState(null);
  const [underlying, setUnderlying] = useState("NIFTY");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const [h, r, j, c, cmp, f, ind, s] = await Promise.all([
      api.get("/broker/health").catch(err => { console.error("health failed", err); return { data: null }; }),
      api.get("/risk/dashboard").catch(err => { console.error("risk failed", err); return { data: null }; }),
      api.get("/trade-journal").catch(err => { console.error("journal failed", err); return { data: null }; }),
      api.get(`/option-chain/${underlying}`).catch(err => { console.error("chain failed", err); return { data: null }; }),
      api.get("/strategies/live-backtest-comparison").catch(err => { console.error("comparison failed", err); return { data: null }; }),
      api.get("/market/feed-comparison").catch(err => { console.error("feed failed", err); return { data: null }; }),
      api.get(`/market/indicators/${underlying}`).catch(err => { console.error("indicators failed", err); return { data: null }; }),
      api.get("/market/session-status").catch(err => { console.error("session failed", err); return { data: null }; }),
    ]);
    if (h.data) setHealth(h.data);
    if (r.data) setRisk(r.data);
    if (j.data) setJournal(j.data);
    if (c.data) setChain(c.data);
    if (cmp.data) setComparison(cmp.data);
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

  const brokers = health?.brokers || {
    upstox: health?.upstox,
  };

  return (
    <div className="space-y-4 max-w-7xl" data-testid="market-hub-page">
      <PageHeader
        eyebrow={`${APP_VERSION_LABEL} Control Room`}
        title="Market Hub"
        subtitle="Broker health, ticker quality, option-chain context, and daily risk in one market workspace."
        badge={<StatusBadge tone={(marketSession?.global_status === "OPEN" || risk?.market_open) ? "healthy" : "warning"}>{marketSession?.global_status || "Market"}</StatusBadge>}
        actions={
          <>
            <button onClick={load} className="border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] text-[var(--qd-text)] px-3 py-2 text-xs font-mono uppercase rounded-sm flex items-center gap-2">
              <RefreshCw size={14} /> Refresh
            </button>
            <button onClick={squareOff} disabled={busy} className="border border-[var(--qd-loss)] text-[var(--qd-loss)] hover:bg-[rgba(255,59,48,0.08)] px-3 py-2 text-xs font-mono uppercase rounded-sm flex items-center gap-2 disabled:opacity-60">
              <SquareArrowOutUpRight size={14} /> Square Off All
            </button>
          </>
        }
      />

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Metric label="Mode" value={risk?.mode || "-"} tone={risk?.mode === "LIVE" ? "loss" : "warn"} />
        <Metric label="Market" value={marketSession?.global_status || (risk?.market_open ? "OPEN" : "CLOSED")} tone={(marketSession?.global_status === "OPEN" || risk?.market_open) ? "profit" : "warn"} />
        <Metric label="P&L Today" value={`₹${formatINR(risk?.total_pnl || 0)}`} tone={(risk?.total_pnl || 0) >= 0 ? "profit" : "loss"} />
        <Metric label="Trades" value={`${risk?.trades_used ?? 0}/${risk?.max_trades_per_day ?? "-"}`} />
        <Metric label="Risk Left" value={risk?.loss_remaining == null ? "-" : `₹${formatINR(risk.loss_remaining)}`} tone="warn" />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        {feed?.simulated_warning && (
          <div className="rounded border border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.08)] px-3 py-2 text-xs font-mono text-[var(--qd-loss)] xl:col-span-2">
            {feed.simulated_warning}
          </div>
        )}

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
            <button onClick={autoPickFeed} disabled={busy} className="border border-[var(--qd-border)] hover:border-[var(--qd-profit)] text-[var(--qd-text)] px-3 py-2 text-xs font-mono uppercase rounded-sm disabled:opacity-60">
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
            <Row k="Realised P&L" v={`₹${formatINR(risk?.realised_pnl || 0)}`} />
            <Row k="Per-strategy capital" v={`₹${formatINR(risk?.per_strategy_capital || 0)}`} />
            <Row k="Max position size" v={`₹${formatINR(risk?.max_position_size || 0)}`} />
          </div>
        </section>

        <section className="qd-card p-4 xl:col-span-2">
          <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><Activity size={16} /> Signal Stack</h2>
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

      <section className="qd-card">
        <div className="border-b border-[var(--qd-border)] px-4 py-3">
          <h2 className="font-head text-lg text-white flex items-center gap-2"><Activity size={16} /> Live vs Backtest</h2>
        </div>
        <div className="qd-table-wrap">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">Strategy</th><th className="px-4 py-2">Live P&L</th><th className="px-4 py-2">Backtest P&L</th><th className="px-4 py-2">Drift</th><th className="px-4 py-2">Signal Quality</th><th className="px-4 py-2">Verdict</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {(comparison?.items || []).slice(0, 12).map((row) => (
                <tr key={row.strategy_id} className="border-t border-[var(--qd-border)]">
                  <td className="px-4 py-2 text-white">{row.name}</td>
                  <td className={`px-4 py-2 ${(row.live?.realised_pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>₹{formatINR(row.live?.realised_pnl || 0)}</td>
                  <td className={`px-4 py-2 ${(row.backtest?.pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{row.backtest?.available ? `₹${formatINR(row.backtest.pnl || 0)}` : "-"}</td>
                  <td className={`px-4 py-2 ${(row.drift || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{row.drift == null ? "-" : `₹${formatINR(row.drift)}`}</td>
                  <td className="px-4 py-2 text-[var(--qd-text-2)]">{row.last_signal_validation?.confidence != null ? `${row.last_signal_validation.confidence}%` : row.last_filter_reason ? "filtered" : "-"}</td>
                  <td className="px-4 py-2 text-[var(--qd-warn)]">{row.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="qd-card">
        <div className="flex items-center justify-between gap-3 border-b border-[var(--qd-border)] px-4 py-3">
          <h2 className="font-head text-lg text-white flex items-center gap-2"><WalletCards size={16} /> Option Chain</h2>
          <select value={underlying} onChange={(e) => setUnderlying(e.target.value)} className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-xs text-white font-mono rounded-sm">
            {["NIFTY", "BANKNIFTY", "SENSEX"].map((u) => <option key={u} value={u}>{u}</option>)}
          </select>
        </div>
        <div className="px-4 py-3 grid grid-cols-2 md:grid-cols-4 gap-2">
          <Small label="Source" value={chain?.source || "-"} />
          <Small label="Spot" value={`₹${formatINR(chain?.spot || 0)}`} />
          <Small label="ATM" value={chain?.atm || "-"} />
          <Small label="Expiry" value={chain?.expiry || "-"} />
        </div>
        <div className="qd-table-wrap">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">CE Symbol</th><th className="px-4 py-2">CE LTP</th><th className="px-4 py-2 text-center">Strike</th><th className="px-4 py-2">PE LTP</th><th className="px-4 py-2">PE Symbol</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {(chain?.rows || []).map((r) => (
                <tr key={r.strike} className={`border-t border-[var(--qd-border)] ${r.strike === chain?.atm ? "bg-[rgba(0,122,255,0.08)]" : ""}`}>
                  <td className="px-4 py-2 text-white">{r.ce?.symbol || "-"}</td>
                  <td className="px-4 py-2 text-[var(--qd-profit)]">{r.ce?.ltp == null ? "-" : formatINR(r.ce.ltp)}</td>
                  <td className="px-4 py-2 text-center text-white">{r.strike}</td>
                  <td className="px-4 py-2 text-[var(--qd-loss)]">{r.pe?.ltp == null ? "-" : formatINR(r.pe.ltp)}</td>
                  <td className="px-4 py-2 text-white">{r.pe?.symbol || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
