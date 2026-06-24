import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Shield, Play, Pause, HelpCircle, Trash2, Zap, Code2 } from "lucide-react";
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
  return <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${tone}`}>{status || "draft"}</span>;
}

function Metric({ label, value, tone, compact = false }) {
  return (
    <div className="min-w-0">
      <div className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">{label}</div>
      <div className={`${compact ? "text-xs" : "text-sm"} mt-0.5 truncate font-mono font-semibold ${tone || "text-white"}`}>{value}</div>
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
  // Day-level stand-down (profit_lock / loss_killswitch). Only flag when the lock
  // is dated today (IST), so a prior day's lock never lingers on the card.
  const istToday = new Date(Date.now() + (new Date().getTimezoneOffset() + 330) * 60000)
    .toISOString().slice(0, 10);
  const lossLockedToday = s.day_loss_locked && s.day_loss_locked_date === istToday;
  const profitLockedToday = s.day_profit_locked && s.day_profit_locked_date === istToday;
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
      className={`qd-card qd-strategy-card flex flex-col gap-1.5 p-2.5 relative overflow-hidden ${
        isHft
          ? "border-[var(--qd-border-strong)]"
          : "hover:border-[var(--qd-border-strong)]"
      }`}
      data-testid={`strategy-${s.id}`}
    >
      {/* Header: title + status */}
      <div className="flex items-start justify-between gap-2 relative z-10">
        <div className="min-w-0">
          <div className="qd-section-title flex items-center gap-1.5">
            <span className="truncate">{s.instrument_group || "NSE"} / {s.kind}</span>
            <span className="w-1 h-1 rounded-full bg-[var(--qd-text-3)] shrink-0" />
            <span className="text-[var(--qd-text-2)] truncate">{speedLabel()}</span>
          </div>
          <h2 className="mt-0.5 truncate font-head text-sm font-semibold text-[var(--qd-text)]">{s.name}</h2>
        </div>
        <StatusBadge status={s.status} />
      </div>

      {/* Tag badges */}
      <div className="flex items-center gap-1 flex-wrap relative z-10">
        {isHft && (
          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-amber-500/35 bg-amber-500/10 text-[var(--qd-warn)] font-mono text-[10px] uppercase tracking-wider font-semibold">
            <Zap size={9} /> Upstox
          </span>
        )}
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider ${s.mode === "live" ? "bg-rose-500/10 border border-rose-500/30 text-[var(--qd-loss)] animate-pulse" : "bg-cyan-500/10 border border-cyan-500/30 text-[var(--qd-cyan)]"}`}>
          {s.mode === "live" ? "LIVE" : "PAPER"}
        </span>
        {isOptionStrat && (
          <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider border ${isSpreadCard ? "bg-[rgba(0,122,255,0.12)] border-[var(--qd-accent)]/30 text-[var(--qd-accent)]" : "bg-[var(--qd-surface-2)] border-[var(--qd-border)] text-[var(--qd-text-2)]"}`}>
            {cardOpts.structure === "debit_spread" ? "DEBIT" : cardOpts.structure === "credit_spread" ? "CREDIT" : "SINGLE"}
          </span>
        )}
        {maxTd != null && (
          <span
            className={`px-1.5 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider border ${atCap ? "bg-[rgba(255,159,10,0.12)] border-[var(--qd-warn)]/30 text-[var(--qd-warn)]" : "bg-[var(--qd-surface-2)] border-[var(--qd-border)] text-[var(--qd-text-3)]"}`}
            title="Trades today / daily cap"
          >
            {tradesToday}/{maxTd}
          </span>
        )}
        {(lossLockedToday || profitLockedToday) && (
          <span
            className={`px-1.5 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider border ${lossLockedToday ? "bg-rose-500/10 border-rose-500/30 text-[var(--qd-loss)]" : "bg-emerald-500/10 border-emerald-500/30 text-[var(--qd-profit)]"}`}
            title={lossLockedToday ? "Daily loss floor hit — stood down for the day" : "Day's profit locked — stood down for the day"}
          >
            {lossLockedToday ? "LOSS-LOCKED" : "PROFIT-LOCKED"}
          </span>
        )}
      </div>

      {/* Metrics 3 x 2 */}
      <div className="grid grid-cols-3 gap-x-2 gap-y-1.5 rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2 relative z-10">
        <Metric label="Capital" value={money(s.required_capital)} compact />
        <Metric label="Type" value={s.strategy_type || "Option Buying"} compact />
        <Metric label="Score" value={scoreValue ? `${scoreValue}%` : "-"} compact />
        <Metric label="Scans" value={s.evaluations_today ?? 0} compact />
        <Metric label="Orders" value={tradesToday} compact tone={tradesToday > 0 ? "text-[var(--qd-profit)]" : ""} />
        <Metric label="Last" value={timeAgo(s.last_evaluated_at)} compact />
      </div>

      {/* Broker status line */}
      <div className="flex items-center gap-1.5 font-mono text-[10px] text-[var(--qd-text-3)] relative z-10">
        <span>{broker.name}</span>
        <span className={`w-1.5 h-1.5 rounded-full ${broker.connected ? "bg-emerald-400 animate-pulse" : "bg-rose-500"}`} />
        <span className="uppercase tracking-wider">{broker.connected ? "online" : "offline"}</span>
      </div>

      {notice && (
        <div className={`rounded border px-2 py-1 text-[10px] relative z-10 ${
          notice.kind === "filter"
            ? "border-[rgba(255,159,10,0.35)] text-[var(--qd-warn)]"
            : "border-[rgba(255,59,48,0.35)] text-[var(--qd-loss)]"
        }`}>
          {notice.text}
        </div>
      )}

      {/* EXPANDABLE RISK & EXIT SETTINGS */}
      <div className="border-t border-[var(--qd-border)] pt-1.5 relative z-10">
        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center justify-between py-1 px-2 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] rounded font-mono text-[10px] uppercase tracking-wide text-[var(--qd-text)] transition-all active:scale-[0.99]"
          type="button"
        >
          <span className="flex items-center gap-1.5">
            <Shield size={12} className="text-[var(--qd-accent)]" />
            Risk &amp; Exit Bounds
          </span>
          <span className="text-[var(--qd-accent)] font-bold">{expanded ? "Hide ▲" : "Configure ▼"}</span>
        </button>

        {expanded && (
          <RuntimeSettingsForm s={s} saving={saving} onSubmit={saveSettings} />
        )}
      </div>

      {/* Actions — single solid colour each, no hover-fill swap */}
      <div className="grid grid-cols-5 gap-1 relative z-10 mt-auto">
        <button
          onClick={() => exitAll(s.id)}
          className="flex flex-col items-center justify-center gap-0.5 rounded border border-[color-mix(in_srgb,var(--qd-warn)_28%,transparent)] bg-[color-mix(in_srgb,var(--qd-warn)_12%,transparent)] py-1.5 font-mono text-[9px] font-bold uppercase tracking-wide text-[var(--qd-warn)] hover:bg-[color-mix(in_srgb,var(--qd-warn)_20%,transparent)]"
          data-testid={`exit-all-${s.id}`}
          type="button"
          title="Square off / exit positions"
        >
          <Shield size={13} /> Exit
        </button>
        <button
          onClick={() => onAbout(s)}
          className="flex flex-col items-center justify-center gap-0.5 rounded border border-[color-mix(in_srgb,var(--qd-accent)_28%,transparent)] bg-[color-mix(in_srgb,var(--qd-accent)_12%,transparent)] py-1.5 font-mono text-[9px] uppercase tracking-wide text-[var(--qd-accent)] hover:bg-[color-mix(in_srgb,var(--qd-accent)_20%,transparent)]"
          data-testid={`about-${s.id}`}
          type="button"
          title="About this strategy"
        >
          <HelpCircle size={13} /> About
        </button>
        <button
          onClick={() => toggle(s.id)}
          className="flex flex-col items-center justify-center gap-0.5 rounded border border-[var(--qd-border)] py-1.5 font-mono text-[9px] uppercase tracking-wide text-[var(--qd-text)] hover:border-[var(--qd-border-strong)]"
          data-testid={`toggle-${s.id}`}
          type="button"
        >
          {live ? <><Pause size={13} /> Pause</> : <><Play size={13} /> {paused ? "Resume" : "Live"}</>}
        </button>
        <Link
          to={editPath}
          className="flex flex-col items-center justify-center gap-0.5 rounded border border-[var(--qd-border)] py-1.5 font-mono text-[9px] uppercase tracking-wide text-[var(--qd-text)] hover:border-[var(--qd-border-strong)]"
          data-testid={`edit-${s.id}`}
        >
          <Code2 size={13} /> Edit
        </Link>
        <button
          onClick={() => del(s.id)}
          className="flex flex-col items-center justify-center gap-0.5 rounded border border-[var(--qd-border)] py-1.5 font-mono text-[9px] uppercase tracking-wide text-[var(--qd-loss)] hover:border-[var(--qd-loss)]"
          data-testid={`delete-${s.id}`}
          aria-label="Delete strategy"
          type="button"
        >
          <Trash2 size={13} /> Del
        </button>
      </div>
    </article>
  );
};

export default StrategyCard;
