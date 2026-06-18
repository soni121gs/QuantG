import React from "react";

export default function OpsActionCard({ icon: Icon, title, text, onClick, busy, danger }) {
  return (
    <button
      onClick={onClick}
      disabled={busy}
      className={`w-full text-left border rounded-md p-4 flex gap-3.5 transition-all duration-200 hover:-translate-y-[1px] disabled:opacity-60 disabled:pointer-events-none ${
        danger
          ? "border-[rgba(255,59,48,0.3)] bg-red-950/10 text-[var(--qd-loss)] hover:bg-red-500/10 hover:border-red-500 hover:shadow-lg hover:shadow-red-500/5"
          : "border-[var(--qd-border)] bg-[var(--qd-surface)] text-white hover:border-[var(--qd-border-strong)] hover:bg-[var(--qd-surface-2)] hover:shadow-lg hover:shadow-black/20"
      }`}
    >
      <div className={`p-2 rounded-lg flex-shrink-0 ${danger ? "bg-red-500/10 text-[var(--qd-loss)]" : "bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]"}`}>
        <Icon size={18} />
      </div>
      <span className="flex-1 min-w-0">
        <span className="block text-xs uppercase tracking-wide font-bold font-mono">
          {busy ? "Transmitting..." : title}
        </span>
        <span className="block text-[11px] text-[var(--qd-text-3)] leading-relaxed mt-1">
          {text}
        </span>
      </span>
    </button>
  );
}
