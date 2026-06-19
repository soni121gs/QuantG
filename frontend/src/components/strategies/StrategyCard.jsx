import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Shield, Play, Pause, HelpCircle, Trash2, Zap, X } from "lucide-react";
import { toast } from "sonner";
import { api, formatINR } from "../../lib/api";
import { reasonLabel } from "../../lib/reasonLabels";
import RuntimeSettingsForm from "./RuntimeSettingsForm";

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

export const StrategyCard = ({ s, score, toggle, del, onAbout, exitAll, load, upstoxStatus }) => {
  const live = s.status === "live";
  const paused = s.status === "paused";
  const cardOpts = s.visual_config?.options || {};
  const isOptionStrat = !!cardOpts.enabled;
  const isSpreadCard = cardOpts.structure === "credit_spread" || cardOpts.structure === "debit_spread";
  const maxTd = s.visual_config?.risk?.max_trades_day;
  const tradesToday = s.order_count_today ?? 0;
  const atCap = maxTd != null && tradesToday >= maxTd;
  const notice = noticeFor(s);
  const editPath = s.kind === "python" ? `/python?id=${s.id}` : `/visual?id=${s.id}`;
  const scoreValue = score?.score ?? s.ai_confidence_score ?? 0;  
  
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);

  const saveSettings = async (payload) => {
    setSaving(true);
    try {
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
            {isOptionStrat && (
              <span className={`px-2 py-0.5 rounded text-[8px] font-mono font-bold uppercase tracking-wider border ${isSpreadCard ? "bg-[rgba(0,122,255,0.12)] border-[var(--qd-accent)]/30 text-[var(--qd-accent)]" : "bg-[var(--qd-surface-2)] border-[var(--qd-border)] text-[var(--qd-text-2)]"}`}>
                {cardOpts.structure === "debit_spread" ? "DEBIT SPREAD" : cardOpts.structure === "credit_spread" ? "CREDIT SPREAD" : "SINGLE-LEG"}
              </span>
            )}
            {maxTd != null && (
              <span
                className={`px-2 py-0.5 rounded text-[8px] font-mono font-bold uppercase tracking-wider border ${atCap ? "bg-[rgba(255,159,10,0.12)] border-[var(--qd-warn)]/30 text-[var(--qd-warn)]" : "bg-[var(--qd-surface-2)] border-[var(--qd-border)] text-[var(--qd-text-3)]"}`}
                title="Trades today / daily cap"
              >
                {tradesToday} / {maxTd} today
              </span>
            )}
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
          type="button"
        >
          <span className="flex items-center gap-1.5">
            <Shield size={12} className="text-indigo-400" /> 
            Risk & Exit Bounds
          </span>
          <span className="text-indigo-400 font-bold">{expanded ? "Hide ▲" : "Configure ▼"}</span>
        </button>
        
        {expanded && (
          <RuntimeSettingsForm s={s} saving={saving} onSubmit={saveSettings} />
        )}
      </div>

      <div className="mt-2">
        <div className="grid grid-cols-1 gap-2 sm:hidden">
          <button
            onClick={() => exitAll(s.id)}
            className="flex items-center justify-center gap-2 rounded border border-[var(--qd-warn)] text-[var(--qd-warn)] hover:bg-[var(--qd-warn)] hover:text-black py-2.5 font-mono text-xs font-bold uppercase tracking-wider transition-all"
            data-testid={`exit-all-${s.id}`}
            type="button"
          >
            <Shield size={14} /> Square Off / Exit Position
          </button>
        </div>
        <div className="mt-2 grid grid-cols-[1fr_1fr_1fr_auto_auto] gap-2">
          <button
            onClick={() => exitAll(s.id)}
            className="hidden items-center justify-center gap-2 rounded border border-[var(--qd-warn)] px-3 py-2 font-mono text-xs font-bold uppercase tracking-wider text-[var(--qd-warn)] transition-all hover:bg-[var(--qd-warn)] hover:text-black sm:flex"
            data-testid={`exit-all-${s.id}`}
            type="button"
          >
            <Shield size={13} /> Exit
          </button>
          <button
            onClick={() => onAbout(s)}
            className="flex items-center justify-center gap-2 rounded border border-[var(--qd-accent)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:bg-[var(--qd-accent)] hover:text-white"
            data-testid={`about-${s.id}`}
            type="button"
          >
            <HelpCircle size={13} /> About
          </button>
          <button onClick={() => toggle(s.id)} className="flex items-center justify-center gap-2 rounded border border-[var(--qd-border)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text)] hover:border-[var(--qd-border-strong)]" data-testid={`toggle-${s.id}`} type="button">
            {live ? <><Pause size={13} /> Pause</> : <><Play size={13} /> {paused ? "Resume" : "Live"}</>}
          </button>
          <Link to={editPath} className="rounded border border-[var(--qd-border)] px-3 py-3 text-center font-mono text-xs uppercase tracking-wider text-[var(--qd-text)] hover:border-[var(--qd-border-strong)]" data-testid={`edit-${s.id}`}>
            Edit
          </Link>
          <button onClick={() => del(s.id)} className="flex h-11 w-11 items-center justify-center rounded border border-[var(--qd-border)] text-[var(--qd-loss)] hover:border-[var(--qd-loss)]" data-testid={`delete-${s.id}`} aria-label="Delete strategy" type="button">
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </article>
  );
};

export default StrategyCard;
