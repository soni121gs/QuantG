import React from "react";
import { Sparkles, ShieldCheck, HeartPulse, ShieldAlert, BookOpen, X } from "lucide-react";

export const SUGGESTIONS = [
  {
    title: "Verify Setup & Limits",
    desc: "Audit active strategies, drawdown limits & risk rules.",
    prompt: "Verify my active strategies and drawdown limits",
    icon: ShieldCheck,
    color: "hover:border-emerald-500/40 border-[var(--qd-border)] bg-[var(--qd-surface-2)]/10 hover:bg-[var(--qd-surface-2)]/30",
  },
  {
    title: "Inspect Upstox Feed",
    desc: "Check live tick counts, latency and socket connectivity.",
    prompt: "Check Upstox feed and market status",
    icon: HeartPulse,
    color: "hover:border-blue-500/40 border-[var(--qd-border)] bg-[var(--qd-surface-2)]/10 hover:bg-[var(--qd-surface-2)]/30",
  },
  {
    title: "Configure Loss Limit",
    desc: "Lower daily loss limit to 6,000 INR for risk protection.",
    prompt: "Lower my daily loss limit to 6000 INR for protection",
    icon: ShieldAlert,
    color: "hover:border-amber-500/40 border-[var(--qd-border)] bg-[var(--qd-surface-2)]/10 hover:bg-[var(--qd-surface-2)]/30",
  },
  {
    title: "Emergency Paper Mode",
    desc: "Instantly switch from Live execution to simulated Paper mode.",
    prompt: "Switch terminal to emergency paper mode",
    icon: BookOpen,
    color: "hover:border-rose-500/40 border-[var(--qd-border)] bg-[var(--qd-surface-2)]/10 hover:bg-[var(--qd-surface-2)]/30",
  },
];

export function PromptSuggestionsPanel({ onPick, busy, onClose }) {
  return (
    <div className="flex gap-2 px-4 py-2 border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)]/30 items-center justify-between">
      <div className="flex gap-2 items-center overflow-x-auto scrollbar-none flex-1 py-0.5">
        <span className="t-meta mr-1 text-[var(--qd-text-3)] font-bold uppercase tracking-widest flex items-center gap-1 font-mono flex-shrink-0">
          <Sparkles size={11} className="text-[var(--qd-accent)]" /> Quick Suggestions:
        </span>
        {SUGGESTIONS.map((s) => (
          <button
            key={s.title}
            type="button"
            onClick={() => onPick(s.prompt)}
            disabled={busy}
            className="rounded-full border border-[var(--qd-border)] bg-[var(--qd-surface)] px-3 py-1 text-[11px] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] disabled:opacity-50 transition-all active:scale-95 font-sans cursor-pointer flex items-center gap-1.5 flex-shrink-0"
          >
            <s.icon size={10} className="text-[var(--qd-text-3)]" />
            <span>{s.title}</span>
          </button>
        ))}
      </div>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          className="p-1 rounded text-[var(--qd-text-3)] hover:text-[var(--qd-text-2)] cursor-pointer flex-shrink-0 hover:bg-[var(--qd-surface-2)]"
          title="Dismiss suggestions"
          aria-label="Dismiss suggestions"
        >
          <X size={12} />
        </button>
      )}
    </div>
  );
}

export function EmptyState({ onPick }) {
  return (
    <div className="flex flex-col items-center justify-center text-center px-4 py-12 md:py-16 h-full max-w-2xl mx-auto animate-fade-in">
      <div className="relative mb-6">
        <div className="w-15 h-15 rounded-2xl bg-[var(--qd-accent)]/10 border border-[var(--qd-accent)]/20 flex items-center justify-center">
          <Sparkles className="text-[var(--qd-accent)]" size={28} />
        </div>
        <span className="absolute -top-0.5 -right-0.5 flex h-3 w-3">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
          <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
        </span>
      </div>
      
      <h3 className="font-head text-2xl font-extrabold text-[var(--qd-text)] tracking-tight">
        How can I protect your trading setup?
      </h3>
      <p className="mt-2.5 max-w-md text-sm text-[var(--qd-text-2)] font-sans leading-relaxed">
        I am your live risk and execution analyst. I examine active orders, positions, capital usage, and Upstox feed health to give data-grounded answers.
      </p>
      
      <div className="mt-8 grid grid-cols-1 sm:grid-cols-2 gap-3.5 w-full">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.title}
            onClick={() => onPick(s.prompt)}
            className={`group text-left border rounded-xl p-4 transition-all duration-200 cursor-pointer flex gap-3.5 items-start ${s.color} hover:-translate-y-0.5 hover:shadow-md`}
          >
            <div className="p-2 rounded-lg bg-[var(--qd-surface)] border border-[var(--qd-border)] group-hover:border-[var(--qd-accent)]/30 shadow-sm flex-shrink-0 text-[var(--qd-text-2)] group-hover:text-[var(--qd-accent)] transition-colors">
              <s.icon size={16} />
            </div>
            <div className="min-w-0">
              <h4 className="font-head text-sm font-bold text-[var(--qd-text)] group-hover:text-[var(--qd-accent)] transition-colors">
                {s.title}
              </h4>
              <p className="mt-0.5 text-xs text-[var(--qd-text-2)] font-sans leading-normal">
                {s.desc}
              </p>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
