import React, { useState } from "react";
import { MessageSquare, Plus, ShieldCheck, Sliders, ShieldAlert, ChevronDown, Trash2 } from "lucide-react";

// Right rail for the Agent page. Replaces the old SessionHistorySidebar
// (which confusingly showed governance cards, not sessions). Now it carries:
//   1. A real conversation list  — the navigation that was missing.
//   2. A slim risk strip         — one line, not the duplicated stat cards
//                                  already shown in the global mini-blotter.
//   3. A collapsed capabilities  — kept out of the way until wanted.

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

export default function AgentContextPanel({
  sessions = [],
  sessionId,
  onSelect,
  onNew,
  onDelete,
  profile,
  executionSummary,
}) {
  const [capsOpen, setCapsOpen] = useState(false);

  const lossLimit = Number(profile?.max_daily_loss) || 0;
  const netPnl = Number(executionSummary?.net_pnl) || 0;
  const drawdownUsedPct = lossLimit > 0
    ? Math.min(100, Math.max(0, (Math.max(0, -netPnl) / lossLimit) * 100))
    : 0;
  const tone = drawdownUsedPct >= 80 ? "rose" : drawdownUsedPct >= 50 ? "amber" : "emerald";
  const barColor = tone === "rose" ? "bg-rose-500" : tone === "amber" ? "bg-amber-500" : "bg-emerald-500";
  const textColor = tone === "rose" ? "text-rose-400" : tone === "amber" ? "text-amber-400" : "text-emerald-400";

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:overflow-y-auto">
      {/* Conversations */}
      <div className="qd-card flex flex-col p-4 lg:min-h-0 lg:flex-1">
        <div className="flex items-center justify-between border-b border-[var(--qd-border)]/60 pb-3">
          <h2 className="qd-section-title flex items-center gap-2">
            <MessageSquare size={14} className="text-[var(--qd-accent)]" /> Conversations
          </h2>
          <button
            type="button"
            onClick={onNew}
            className="t-meta inline-flex items-center gap-1 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-2 py-1 font-semibold text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)]"
            data-testid="ai-new-chat"
          >
            <Plus size={13} /> New
          </button>
        </div>

        <div className="mt-2 space-y-1 lg:overflow-y-auto" data-testid="ai-session-list">
          {sessions.length === 0 && (
            <div className="t-label py-6 text-center text-[var(--qd-text-3)]">No conversations yet.</div>
          )}
          {sessions.map((s) => {
            const active = s.id === sessionId;
            return (
              <div
                key={s.id}
                className={`group flex items-center gap-2 rounded-[var(--qd-radius-sm)] border px-2.5 py-2 transition-colors ${
                  active
                    ? "border-[var(--qd-border-strong)] bg-[var(--qd-surface-3)]"
                    : "border-transparent hover:bg-[var(--qd-surface-2)]"
                }`}
              >
                <button
                  type="button"
                  onClick={() => onSelect(s.id)}
                  className="min-w-0 flex-1 text-left"
                  data-testid="ai-session-item"
                >
                  <div className={`t-label truncate font-semibold ${active ? "text-[var(--qd-text)]" : "text-[var(--qd-text-2)]"}`}>
                    {s.title || "New chat"}
                  </div>
                  <div className="t-meta mt-0.5 font-mono text-[var(--qd-text-3)]">{relativeTime(s.updatedAt)}</div>
                </button>
                {onDelete && sessions.length > 1 && (
                  <button
                    type="button"
                    onClick={() => onDelete(s.id)}
                    className="shrink-0 rounded p-1 text-[var(--qd-text-3)] opacity-0 transition-opacity hover:text-[var(--qd-loss)] group-hover:opacity-100"
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

      {/* Slim risk strip — one line, no duplicated stat cards */}
      <div className="qd-card p-4">
        <div className="flex items-center justify-between">
          <h2 className="qd-section-title flex items-center gap-2">
            <ShieldCheck size={14} className="text-emerald-400" /> Risk Posture
          </h2>
          <span className={`t-meta font-mono font-bold ${profile?.paper_mode ? "text-amber-400" : "text-rose-400"}`}>
            {profile?.paper_mode ? "PAPER" : "LIVE"}
          </span>
        </div>
        {profile ? (
          <div className="mt-3">
            <div className="flex items-center justify-between">
              <span className="t-meta font-mono uppercase tracking-wider text-[var(--qd-text-3)]">Daily Drawdown</span>
              <span className={`t-meta font-mono font-bold ${textColor}`}>{drawdownUsedPct.toFixed(0)}%</span>
            </div>
            <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full border border-[var(--qd-border)]/60 bg-[var(--qd-bg)]">
              <div className={`h-full rounded-full transition-all ${barColor}`} style={{ width: `${drawdownUsedPct}%` }} />
            </div>
            <div className="t-meta mt-2 font-mono text-[var(--qd-text-3)]">
              Net P&amp;L {netPnl >= 0 ? "+" : ""}{netPnl.toLocaleString()} INR
              {lossLimit > 0 ? ` · limit ${lossLimit.toLocaleString()}` : " · no limit set"}
            </div>
          </div>
        ) : (
          <div className="t-label mt-3 animate-pulse text-center text-[var(--qd-text-3)]">Loading…</div>
        )}
      </div>

      {/* Capabilities — collapsed by default to keep the rail calm */}
      <div className="qd-card">
        <button
          type="button"
          onClick={() => setCapsOpen((v) => !v)}
          className="flex w-full items-center justify-between p-4"
          aria-expanded={capsOpen}
        >
          <span className="qd-section-title flex items-center gap-2">
            <Sliders size={14} className="text-blue-400" /> What this agent can do
          </span>
          <ChevronDown size={15} className={`text-[var(--qd-text-3)] transition-transform ${capsOpen ? "rotate-180" : ""}`} />
        </button>
        {capsOpen && (
          <div className="space-y-3 px-4 pb-4">
            <div>
              <div className="t-meta flex items-center gap-1.5 font-mono font-bold uppercase tracking-wider text-emerald-400">
                <ShieldCheck size={12} /> Reads automatically
              </div>
              <ul className="t-label mt-1.5 space-y-0.5 text-[var(--qd-text-2)]">
                <li>· Orders, positions &amp; execution state</li>
                <li>· Strategies, errors &amp; rejected orders</li>
                <li>· Upstox feed, market status &amp; ticks</li>
                <li>· Daily P&amp;L, loss limit &amp; capital usage</li>
              </ul>
            </div>
            <div className="border-t border-[var(--qd-border)]/50 pt-3">
              <div className="t-meta flex items-center gap-1.5 font-mono font-bold uppercase tracking-wider text-blue-400">
                <Sliders size={12} /> Can change — you approve
              </div>
              <ul className="t-label mt-1.5 space-y-0.5 text-[var(--qd-text-2)]">
                <li>· Daily loss limit &amp; max trades / day</li>
                <li>· Position size &amp; per-strategy capital</li>
                <li>· Default qty · Paper ↔ Live (kill)</li>
              </ul>
            </div>
            <div className="flex items-start gap-1.5 border-t border-[var(--qd-border)]/50 pt-3 text-[var(--qd-text-3)]">
              <ShieldAlert size={12} className="mt-0.5 shrink-0 text-[var(--qd-loss)]" />
              <span className="t-label">Never places, cancels, or exits trades. Every change needs your click.</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
