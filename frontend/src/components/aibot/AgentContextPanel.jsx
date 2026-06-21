import React, { useState } from "react";
import { MessageSquare, Plus, ShieldCheck, Sliders, ShieldAlert, ChevronDown, Trash2 } from "lucide-react";

// Right rail / Left sidebar sub-components for the Agent page.

const relativeTime = (ts) => {
  if (!ts) return "";
  const diff = Date.now() - ts;
  const m = Math.round(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
};

export function ConversationHistoryCard({
  sessions = [],
  sessionId,
  onSelect,
  onNew,
  onDelete,
}) {
  return (
    <div className="qd-card flex flex-col p-4 h-full lg:min-h-0 lg:flex-1">
      <div className="flex items-center justify-between border-b border-[var(--qd-border)]/60 pb-3">
        <h2 className="qd-section-title flex items-center gap-2 font-mono">
          <MessageSquare size={14} className="text-[var(--qd-accent)]" /> Conversations
        </h2>
        <button
          type="button"
          onClick={onNew}
          className="t-meta inline-flex items-center gap-1 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-2 py-1 font-semibold text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] cursor-pointer"
          data-testid="ai-new-chat"
        >
          <Plus size={13} /> New
        </button>
      </div>

      <div className="mt-3 space-y-1 overflow-y-auto flex-1 pr-1" data-testid="ai-session-list">
        {sessions.length === 0 && (
          <div className="t-label py-6 text-center text-[var(--qd-text-3)]">No conversations yet.</div>
        )}
        {sessions.map((s) => {
          const active = s.id === sessionId;
          return (
            <div
              key={s.id}
              className={`group flex items-center gap-2 rounded-[var(--qd-radius-sm)] border px-3 py-2.5 transition-all ${
                active
                  ? "border-[var(--qd-border-strong)] bg-[var(--qd-surface-3)]/80 shadow-xs"
                  : "border-transparent hover:bg-[var(--qd-surface-2)]/60"
              }`}
            >
              <button
                type="button"
                onClick={() => onSelect(s.id)}
                className="min-w-0 flex-1 text-left cursor-pointer"
                data-testid="ai-session-item"
              >
                <div className={`text-sm truncate font-bold leading-normal ${active ? "text-[var(--qd-text)]" : "text-[var(--qd-text-2)]"}`}>
                  {s.title || "New chat"}
                </div>
                <div className="t-meta mt-1 font-mono text-[var(--qd-text-3)]">{relativeTime(s.updatedAt)}</div>
              </button>
              {onDelete && sessions.length > 1 && (
                <button
                  type="button"
                  onClick={() => onDelete(s.id)}
                  className="shrink-0 rounded p-1 text-[var(--qd-text-3)] opacity-0 transition-opacity hover:text-[var(--qd-loss)] group-hover:opacity-100 cursor-pointer"
                  title="Delete conversation"
                  aria-label="Delete conversation"
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function RiskPostureCard({ profile, executionSummary }) {
  const lossLimit = Number(profile?.max_daily_loss) || 0;
  const netPnl = Number(executionSummary?.net_pnl) || 0;
  const drawdownUsedPct = lossLimit > 0
    ? Math.min(100, Math.max(0, (Math.max(0, -netPnl) / lossLimit) * 100))
    : 0;
  const tone = drawdownUsedPct >= 80 ? "rose" : drawdownUsedPct >= 50 ? "amber" : "emerald";
  const barColor = tone === "rose" ? "bg-rose-500" : tone === "amber" ? "bg-amber-500" : "bg-emerald-500";
  const textColor = tone === "rose" ? "text-rose-400" : tone === "amber" ? "text-amber-400" : "text-emerald-400";

  return (
    <div className="qd-card p-4">
      <div className="flex items-center justify-between">
        <h2 className="qd-section-title flex items-center gap-2 font-mono">
          <ShieldCheck size={14} className="text-emerald-400" /> Risk Posture
        </h2>
        <span className={`t-meta font-mono font-bold px-1.5 py-0.5 rounded border ${
          profile?.paper_mode 
            ? "text-amber-400 border-amber-500/20 bg-amber-500/5" 
            : "text-rose-400 border-rose-500/20 bg-rose-500/5"
        }`}>
          {profile?.paper_mode ? "PAPER" : "LIVE"}
        </span>
      </div>
      {profile ? (
        <div className="mt-4">
          <div className="flex items-center justify-between text-xs">
            <span className="font-mono uppercase tracking-wider text-[var(--qd-text-3)] font-semibold">Daily Drawdown</span>
            <span className={`font-mono font-bold ${textColor}`}>{drawdownUsedPct.toFixed(0)}%</span>
          </div>
          <div className="mt-2 h-2.5 w-full overflow-hidden rounded-full border border-[var(--qd-border)]/60 bg-[var(--qd-bg)]">
            <div className={`h-full rounded-full transition-all duration-300 ${barColor}`} style={{ width: `${drawdownUsedPct}%` }} />
          </div>
          <div className="t-meta mt-3.5 font-mono text-[var(--qd-text-2)] bg-[var(--qd-surface-2)]/40 p-2.5 rounded border border-[var(--qd-border)]/40 leading-relaxed">
            <div>Net P&amp;L: <span className={`font-bold ${netPnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{netPnl >= 0 ? "+" : ""}{netPnl.toLocaleString()} INR</span></div>
            <div className="text-[var(--qd-text-3)] mt-0.5">Limit: {lossLimit > 0 ? `${lossLimit.toLocaleString()} INR` : "No limit set"}</div>
          </div>
        </div>
      ) : (
        <div className="t-label mt-3 animate-pulse text-center text-[var(--qd-text-3)]">Loading…</div>
      )}
    </div>
  );
}

export function CapabilitiesCard({ profile }) {
  const [capsOpen, setCapsOpen] = useState(true); // default to open for visibility, collapsible by user

  return (
    <div className="qd-card flex flex-col">
      <button
        type="button"
        onClick={() => setCapsOpen((v) => !v)}
        className="flex w-full items-center justify-between p-4 cursor-pointer"
        aria-expanded={capsOpen}
      >
        <span className="qd-section-title flex items-center gap-2 font-mono">
          <Sliders size={14} className="text-blue-400" /> Capabilities
        </span>
        <ChevronDown size={15} className={`text-[var(--qd-text-3)] transition-transform ${capsOpen ? "rotate-180" : ""}`} />
      </button>
      {capsOpen && (
        <div className="space-y-3 px-4 pb-4 border-t border-[var(--qd-border)]/40 pt-3">
          <div>
            <div className="t-meta flex items-center gap-1.5 font-mono font-bold uppercase tracking-wider text-emerald-400">
              <ShieldCheck size={12} /> Live Read Scope
            </div>
            <ul className="t-label mt-1.5 space-y-1 text-[var(--qd-text-2)] list-disc pl-4">
              <li>Orders, positions &amp; execution state</li>
              <li>Strategies, errors &amp; logs</li>
              <li>Upstox feed status &amp; pricing ticks</li>
              <li>Daily P&amp;L &amp; capital utilization</li>
            </ul>
          </div>
          <div className="border-t border-[var(--qd-border)]/40 pt-3">
            <div className="t-meta flex items-center gap-1.5 font-mono font-bold uppercase tracking-wider text-blue-400">
              <Sliders size={12} /> Write Actions (Governed)
            </div>
            <ul className="t-label mt-1.5 space-y-1 text-[var(--qd-text-2)] list-disc pl-4">
              <li>Update Daily loss limit</li>
              <li>Adjust Max trades count per day</li>
              <li>Scale position size &amp; strategy capital</li>
              <li>Trigger emergency paper switch</li>
            </ul>
          </div>
          <div className="flex items-start gap-2 border-t border-[var(--qd-border)]/40 pt-3 text-[var(--qd-text-3)] bg-[var(--qd-surface-2)]/30 p-2.5 rounded-lg">
            <ShieldAlert size={14} className="mt-0.5 shrink-0 text-[var(--qd-loss)]" />
            <span className="text-[10px] font-sans leading-normal font-medium">
              Hermes NEVER places, cancels, or exits trades. All parameters changes are proposed and require your manual click to commit.
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AgentContextPanel({
  sessions = [],
  sessionId,
  onSelect,
  onNew,
  onDelete,
  profile,
  executionSummary,
}) {
  return (
    <div className="flex flex-col gap-4 lg:h-full lg:overflow-y-auto">
      <ConversationHistoryCard
        sessions={sessions}
        sessionId={sessionId}
        onSelect={onSelect}
        onNew={onNew}
        onDelete={onDelete}
      />
      <RiskPostureCard profile={profile} executionSummary={executionSummary} />
      <CapabilitiesCard profile={profile} />
    </div>
  );
}
