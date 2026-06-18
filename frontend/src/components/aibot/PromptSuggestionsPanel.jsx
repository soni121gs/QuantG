import React from "react";
import { Sparkles } from "lucide-react";

export const SUGGESTIONS = [
  "Verify my active strategies and drawdown limits",
  "Check Upstox feed and market status",
  "Lower my daily loss limit to 6000 INR for protection",
  "Switch terminal to emergency paper mode",
];

export function PromptSuggestionsPanel({ onPick, busy }) {
  return (
    <div className="flex flex-wrap gap-1.5 px-3 pt-3">
      {SUGGESTIONS.map((s) => (
        <button
          key={s}
          type="button"
          onClick={() => onPick(s)}
          disabled={busy}
          className="rounded-full border border-[var(--qd-border)] px-3 py-1 text-[11px] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] disabled:opacity-50 transition-all active:scale-95 font-sans"
        >
          {s}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({ onPick }) {
  return (
    <div className="flex h-full flex-col items-center justify-center text-center px-4 py-8">
      <div className="w-14 h-14 rounded-2xl bg-[var(--qd-accent)]/15 border border-[var(--qd-accent)]/30 flex items-center justify-center mb-4">
        <Sparkles className="text-[var(--qd-accent)]" size={26} />
      </div>
      <h3 className="font-head text-lg font-bold text-[var(--qd-text)]">How can I protect your trading setup?</h3>
      <p className="mt-1.5 max-w-md text-sm text-[var(--qd-text-2)] font-sans">
        I read your live orders, positions, risk and feed health, then answer grounded in that data. I can also propose risk-setting changes for you to approve.
      </p>
      <div className="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full max-w-xl">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="text-left text-xs text-[var(--qd-text-2)] border border-[var(--qd-border)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] p-3.5 rounded-[var(--qd-radius-sm)] bg-[var(--qd-surface-2)]/20 hover:bg-[var(--qd-surface-2)]/50 transition-all hover:-translate-y-0.5 font-sans"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}
