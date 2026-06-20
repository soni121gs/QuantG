import React from "react";
import { CircleDollarSign, ArrowUpRight } from "lucide-react";

export default function MarginMeterPanel({ fundsData }) {
  const availCash = fundsData?.available_cash || 0;
  const usedMargin = fundsData?.used_margin || 0;
  const spanMargin = fundsData?.span || 0;
  const totalLimit = availCash + usedMargin;
  const utilPercent = totalLimit > 0 ? Math.round((usedMargin / totalLimit) * 100) : 0;

  const getUtilColor = (pct) => {
    if (pct > 80) return "bg-[var(--qd-loss)] shadow-[0_0_8px_#FF3B30]";
    if (pct > 50) return "bg-[var(--qd-warn)] shadow-[0_0_8px_#FF9F0A]";
    return "bg-[var(--qd-profit)] shadow-[0_0_8px_#00E676]";
  };

  return (
    <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 relative overflow-hidden shadow-xl">
      <div className="absolute top-0 left-0 w-full h-[2px] bg-gradient-to-r from-emerald-500 to-green-400"></div>

      <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-4 mb-5">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 rounded-md">
            <CircleDollarSign size={18} />
          </div>
          <div>
            <h2 className="font-head text-base font-bold text-white">Live Margin Desk Meter</h2>
            <p className="text-[11px] text-[var(--qd-text-3)] font-mono">Live Broker Balance and Utilization Monitor</p>
          </div>
        </div>
        
        <span className="font-mono text-xs font-semibold px-2 py-1 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] text-white rounded">
          Active Source: {fundsData?.source?.toUpperCase() || "PAPER"}
        </span>
      </div>

      {/* Cash Progress Bar */}
      <div className="space-y-2 mb-6">
        <div className="flex justify-between text-xs font-mono">
          <span className="text-[var(--qd-text-2)] font-semibold flex items-center gap-1">
            <ArrowUpRight size={14} className="text-emerald-400" /> Cash Used Utilization
          </span>
          <span className="text-white font-bold">
            {utilPercent}% Used / {availCash.toLocaleString("en-IN", { style: "currency", currency: "INR" })} Avail
          </span>
        </div>
        <div className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-full h-3 overflow-hidden p-[1px]">
          <div 
            className={`h-full rounded-full transition-all duration-700 ${getUtilColor(utilPercent)}`}
            style={{ width: `${Math.min(utilPercent || 1, 100)}%` }}
          ></div>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
          <div className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Available Cash</div>
          <div className="text-sm sm:text-base font-bold text-white font-mono mt-1 truncate">
            {availCash.toLocaleString("en-IN", { style: "currency", currency: "INR" })}
          </div>
          <div className="text-[11px] text-[var(--qd-text-3)] font-mono mt-0.5">Execution limit</div>
        </div>

        <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
          <div className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Used Margin</div>
          <div className="text-sm sm:text-base font-bold text-[var(--qd-loss)] font-mono mt-1 truncate">
            {usedMargin.toLocaleString("en-IN", { style: "currency", currency: "INR" })}
          </div>
          <div className="text-[11px] text-[var(--qd-text-3)] font-mono mt-0.5">Current block</div>
        </div>

        <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
          <div className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">SPAN Margin</div>
          <div className="text-sm sm:text-base font-bold text-[var(--qd-warn)] font-mono mt-1 truncate">
            {spanMargin.toLocaleString("en-IN", { style: "currency", currency: "INR" })}
          </div>
          <div className="text-[11px] text-[var(--qd-text-3)] font-mono mt-0.5">Underlying hedge margin</div>
        </div>

        <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
          <div className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Virtual Leverage</div>
          <div className="text-sm sm:text-base font-bold text-blue-400 font-mono mt-1 truncate">
            {fundsData?.source === "paper" ? "5.0x MIS" : "1.0x NRML"}
          </div>
          <div className="text-[11px] text-[var(--qd-text-3)] font-mono mt-0.5">Daily leverage capacity</div>
        </div>
      </div>
    </div>
  );
}
