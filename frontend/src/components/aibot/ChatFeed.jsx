import React from "react";
import { Bot, User, ArrowRight, ShieldCheck, Sliders, CheckCircle2, XCircle, ShieldAlert, AlertCircle, Sparkles } from "lucide-react";
import { Button } from "../ui/button";
import { renderMarkdown } from "../../lib/markdown";

const FIELD_LABELS = {
  paper_mode: { label: "Trading Mode", fmt: (v) => (v ? "PAPER" : "LIVE") },
  max_daily_loss: { label: "Daily Loss Limit", fmt: (v) => `${Number(v).toLocaleString()} INR` },
  max_position_size: { label: "Max Position Size", fmt: (v) => `${Number(v).toLocaleString()} INR` },
  per_strategy_capital: { label: "Per-Strategy Capital", fmt: (v) => `${Number(v).toLocaleString()} INR` },
  max_trades_per_day: { label: "Max Trades / Day", fmt: (v) => `${v}` },
  default_qty: { label: "Default Quantity", fmt: (v) => `${v}` },
};

const ToolCitationCard = ({ tool }) => {
  const [expanded, setExpanded] = React.useState(false);
  const isOk = tool.status === "ok";
  const hasWarnings = tool.warnings && tool.warnings.length > 0;
  const isStale = tool.stale === true;
  
  const formatToolName = (name) => {
    return name
      .replace(/^get_/, "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  };

  const statusColorClass = !isOk 
    ? "text-rose-400 bg-rose-950/20 border-rose-500/20" 
    : isStale || hasWarnings
      ? "text-amber-400 bg-amber-950/20 border-amber-500/20"
      : "text-emerald-400 bg-emerald-950/20 border-emerald-500/20";

  return (
    <div 
      className="cursor-pointer space-y-1.5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-3)]/35 p-3 transition-all hover:bg-[var(--qd-surface-3)]/60 hover:scale-[1.01] hover:shadow-sm" 
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-center justify-between gap-1.5">
        <span className="t-label truncate font-semibold text-[var(--qd-text-2)]" title={tool.name}>
          {formatToolName(tool.name)}
        </span>
        <span className={`t-meta inline-flex items-center rounded-full border px-1.5 font-mono font-bold ${statusColorClass}`}>
          {!isOk ? "Error" : isStale ? "Stale" : "Verified"}
        </span>
      </div>

      <div className="t-meta flex flex-col font-mono leading-snug text-[var(--qd-text-3)]">
        <div className="truncate">Source: <span className="text-[var(--qd-text-2)]">{tool.source || "System"}</span></div>
        {tool.confidence !== undefined && (
          <div>Confidence: <span className={tool.confidence >= 0.8 ? "text-emerald-400 font-bold" : "text-amber-400 font-bold"}>
            {Math.round(tool.confidence * 100)}%
          </span></div>
        )}
      </div>

      {hasWarnings && tool.warnings && (
        <div className="t-meta mt-1 space-y-0.5 rounded border border-amber-500/10 bg-amber-500/5 p-1.5 font-mono leading-snug text-amber-300">
          {tool.warnings.map((w, idx) => (
            <div key={idx} className="flex items-start gap-1">
              <span>•</span>
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {expanded && (
        <div className="t-meta mt-1.5 max-h-32 overflow-y-auto whitespace-pre-wrap break-all rounded border border-[var(--qd-border)] bg-[var(--qd-bg)]/80 p-2 font-mono text-[var(--qd-text-3)]" onClick={(e) => e.stopPropagation()}>
          {!isOk ? (
            <span className="text-[var(--qd-loss)]">{tool.error}</span>
          ) : (
            <span>Data source verified successfully. Status OK.</span>
          )}
        </div>
      )}
    </div>
  );
};

const Message = ({ m, profile, onApprove, onReject }) => {
  const isUser = m.role === "user";
  const hasAction = !isUser && m.pending_action;
  const [citationsExpanded, setCitationsExpanded] = React.useState(false);

  const getActionBadge = (actionName, params) => {
    if (params?.paper_mode === true || actionName === "emergency_kill") {
      return <span className="action-badge badge-kill inline-flex items-center gap-1 text-[10px] font-bold text-rose-400 bg-rose-950/30 border border-rose-500/20 px-2 py-0.5 rounded-full"><ShieldAlert size={10} /> Kill Switch</span>;
    }
    if (params?.max_daily_loss !== undefined) {
      return <span className="action-badge badge-drawdown inline-flex items-center gap-1 text-[10px] font-bold text-amber-400 bg-amber-950/30 border border-amber-500/20 px-2 py-0.5 rounded-full"><Sliders size={10} /> Drawdown Control</span>;
    }
    if (params?.max_position_size !== undefined || params?.per_strategy_capital !== undefined) {
      return <span className="action-badge badge-size inline-flex items-center gap-1 text-[10px] font-bold text-blue-400 bg-blue-950/30 border border-blue-500/20 px-2 py-0.5 rounded-full"><Sliders size={10} /> Position Sizing</span>;
    }
    return <span className="action-badge badge-normal inline-flex items-center gap-1 text-[10px] font-bold text-indigo-400 bg-indigo-950/30 border border-indigo-500/20 px-2 py-0.5 rounded-full"><Sliders size={10} /> Parameter Adaptation</span>;
  };

  const changeRows = (params) => Object.keys(params || {})
    .filter((k) => FIELD_LABELS[k])
    .map((k) => {
      const cfg = FIELD_LABELS[k];
      const current = profile ? profile[k] : undefined;
      const hasCurrent = current !== undefined && current !== null && current !== "";
      return (
        <div key={k} className="flex items-center justify-between gap-3 py-2 border-b border-[var(--qd-border)]/30 last:border-0">
          <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)] font-semibold">{cfg.label}</span>
          <span className="flex items-center gap-2 font-mono text-xs">
            {hasCurrent && <span className="text-[var(--qd-text-3)] line-through">{cfg.fmt(current)}</span>}
            <ArrowRight size={11} className="text-[var(--qd-accent)]" />
            <span className="font-bold text-[var(--qd-text)]">{cfg.fmt(params[k])}</span>
          </span>
        </div>
      );
    });

  return (
    <div className={`flex gap-3.5 items-start ${isUser ? "justify-end" : ""}`}>
      {!isUser && (
        <div className="relative flex-shrink-0">
          <div className="w-8.5 h-8.5 rounded-lg bg-[var(--qd-accent)] flex items-center justify-center shadow-md">
            <Bot size={17} className="text-[var(--qd-accent-contrast)]" />
          </div>
          <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 bg-emerald-500 border-2 border-[var(--qd-bg)] rounded-full" title="Agent Online" />
        </div>
      )}
      <div 
        className={`max-w-[85%] px-5 py-4 rounded-xl shadow-sm border ${
          isUser 
            ? "qd-force-white bg-gradient-to-br from-[var(--qd-accent)] to-[var(--qd-accent-hover)] border-none text-white" 
            : "bg-[var(--qd-surface)] border-[var(--qd-border)] text-[var(--qd-text)]"
        }`}
      >
        {isUser ? (
          <div className="whitespace-pre-wrap text-[15px] leading-relaxed font-sans">{m.content}</div>
        ) : (
          <div className="text-[15px] leading-relaxed font-sans space-y-2">{renderMarkdown(m.content)}</div>
        )}

        {hasAction && (
          <div className="mt-4 rounded-xl border border-[var(--qd-border-strong)] bg-[var(--qd-surface-2)]/60 overflow-hidden shadow-md">
            <div className="flex items-center justify-between gap-2 border-b border-[var(--qd-border)] bg-[var(--qd-surface-3)]/40 px-4 py-2.5">
              <div className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-2)] font-bold flex items-center gap-1.5">
                <ShieldCheck size={13} className="text-[var(--qd-accent)]" /> Governance Proposal · Action Required
              </div>
              {getActionBadge(m.pending_action.action, m.pending_action.params)}
            </div>

            <div className="px-4 py-3">
              {changeRows(m.pending_action.params)}
            </div>

            {m.pending_action.status === "pending" && (
              <div className="flex gap-2.5 p-4 pt-0">
                <Button onClick={() => onApprove(m.pending_action.id)} className="flex-1 font-mono text-[10px] uppercase tracking-wider h-9 transition-all active:scale-95 hover:scale-[1.01]" variant="success" size="sm">
                  <CheckCircle2 size={13} /> Approve Proposal
                </Button>
                <Button onClick={() => onReject(m.pending_action.id)} className="flex-1 font-mono text-[10px] uppercase tracking-wider h-9 transition-all active:scale-95 hover:scale-[1.01]" variant="danger" size="sm">
                  <XCircle size={13} /> Decline
                </Button>
              </div>
            )}

            {m.pending_action.status === "approved" && (
              <div className="m-4 mt-0 py-2.5 px-3.5 rounded-lg bg-emerald-500/10 border border-emerald-500/30 flex items-center gap-2 text-emerald-400 text-xs font-mono">
                <CheckCircle2 size={14} className="flex-shrink-0" />
                <span className="font-semibold">Approved and committed to terminal settings.</span>
              </div>
            )}

            {m.pending_action.status === "rejected" && (
              <div className="m-4 mt-0 py-2.5 px-3.5 rounded-lg bg-rose-500/10 border border-rose-500/30 flex items-center gap-2 text-rose-400 text-xs font-mono">
                <XCircle size={14} className="flex-shrink-0" />
                <span className="font-semibold">Proposal rejected and discarded.</span>
              </div>
            )}
          </div>
        )}

        {!isUser && m.tools_used?.length > 0 && (
          <div className="mt-4 border-t border-[var(--qd-border)]/50 pt-3">
            <button
              onClick={() => setCitationsExpanded(!citationsExpanded)}
              className="flex items-center justify-between w-full text-[10px] font-mono font-bold uppercase tracking-wider text-[var(--qd-text-3)] hover:text-[var(--qd-text-2)] transition-colors cursor-pointer"
              type="button"
            >
              <span className="flex items-center gap-1.5">
                <Sparkles size={11} className="text-[var(--qd-accent)]" /> 
                Context Citations ({m.tools_used.length} sources)
              </span>
              <span className="text-[10px] font-semibold">
                {citationsExpanded ? "Hide Sources" : "Show Sources"}
              </span>
            </button>
            
            {citationsExpanded && (
              <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 animate-fade-in">
                {m.tools_used.map((tool, idx) => (
                  <ToolCitationCard key={tool.name || idx} tool={tool} />
                ))}
              </div>
            )}
          </div>
        )}
        {!isUser && m.unavailable?.length > 0 && (
          <div className="mt-3 flex gap-2 rounded-lg border border-[var(--qd-loss)]/60 bg-[var(--qd-bg)] p-2.5 text-[11px] text-[var(--qd-loss)]">
            <AlertCircle size={14} className="mt-0.5 flex-shrink-0" />
            <div>Some read-only data was unavailable. The co-pilot response may be partial.</div>
          </div>
        )}
      </div>
      {isUser && (
        <div className="w-8.5 h-8.5 rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface-2)] flex items-center justify-center flex-shrink-0 shadow-md">
          <User size={17} className="text-[var(--qd-text)]" />
        </div>
      )}
    </div>
  );
};

export default function ChatFeed({ messages, profile, onApprove, onReject }) {
  return (
    <div className="space-y-5">
      {messages.map((m, i) => (
        <Message 
          key={m.id || `m-${i}`} 
          m={m} 
          profile={profile} 
          onApprove={onApprove} 
          onReject={onReject} 
        />
      ))}
    </div>
  );
}
