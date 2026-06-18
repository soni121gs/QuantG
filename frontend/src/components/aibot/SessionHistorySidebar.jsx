import React from "react";
import { ShieldCheck, Sliders, HelpCircle, ShieldAlert } from "lucide-react";

const CompactRow = ({ label, value }) => (
  <div className="flex items-center justify-between p-2.5 bg-[var(--qd-surface-2)]/20 rounded border border-[var(--qd-border)]/40 font-mono text-xs">
    <div className="text-[var(--qd-text-2)]">{label}</div>
    <div className="font-extrabold text-blue-400">{value}</div>
  </div>
);

export default function SessionHistorySidebar({ profile, executionSummary }) {
  const lossLimit = Number(profile?.max_daily_loss) || 0;
  const netPnl = Number(executionSummary?.net_pnl) || 0;
  const drawdownUsedPct = lossLimit > 0 ? Math.min(100, Math.max(0, (Math.max(0, -netPnl) / lossLimit) * 100)) : 0;
  
  const getDrawdownTone = (pct) => {
    if (pct >= 80) return "rose";
    if (pct >= 50) return "amber";
    return "emerald";
  };
  const drawdownTone = getDrawdownTone(drawdownUsedPct);

  return (
    <div className="lg:col-span-4 space-y-4">
      {/* System governance */}
      <div className="qd-card p-4">
        <div className="flex items-center justify-between border-b border-[var(--qd-border)]/50 pb-2.5">
          <h2 className="font-head font-bold text-white text-xs uppercase tracking-wider flex items-center gap-2">
            <ShieldCheck size={15} className="text-emerald-400" /> System Governance
          </h2>
          <div className="flex items-center gap-1.5">
            <span className={`w-2 h-2 rounded-full ${profile?.paper_mode ? "bg-rose-500" : "bg-emerald-500"}`} />
            <span className="font-mono text-[9px] font-bold text-[var(--qd-text)] uppercase">
              {profile?.paper_mode ? "Sandbox Paper" : "Production Live"}
            </span>
          </div>
        </div>

        {profile ? (
          <div className="mt-3 space-y-3">
            <div className={`px-3 py-2 rounded border text-center font-mono text-[11px] font-bold ${
              profile.paper_mode 
                ? "bg-rose-950/20 border-rose-500/30 text-rose-400" 
                : "bg-emerald-950/20 border-emerald-500/30 text-emerald-400"
            }`}>
              {profile.paper_mode ? "PAPER / EMERGENCY PAUSE ACTIVE" : "TERMINAL ROUTING LIVE ORDERS"}
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div className="p-2.5 bg-[var(--qd-surface-2)]/30 rounded border border-[var(--qd-border)]/50">
                <div className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider font-bold">Loss Limit</div>
                <div className="font-head font-bold text-sm text-white mt-0.5">{Number(profile.max_daily_loss || 0).toLocaleString()} INR</div>
              </div>
              <div className="p-2.5 bg-[var(--qd-surface-2)]/30 rounded border border-[var(--qd-border)]/50">
                <div className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider font-bold">Trades / Day</div>
                <div className="font-head font-bold text-sm text-white mt-0.5">{profile.max_trades_per_day || "Unlimited"}</div>
              </div>
            </div>

            <div className="space-y-1.5">
              <div className="flex justify-between font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider font-bold">
                <span>Daily Drawdown Used</span>
                <span className={`font-bold ${
                  drawdownTone === "rose" ? "text-rose-400" : 
                  drawdownTone === "amber" ? "text-amber-400" : 
                  "text-emerald-400"
                }`}>
                  {drawdownUsedPct.toFixed(0)}%
                </span>
              </div>
              <div className="w-full bg-[var(--qd-bg)] rounded-full h-2 overflow-hidden border border-[var(--qd-border)]/60">
                <div className={`h-full rounded-full transition-all ${
                  drawdownTone === "rose" ? "bg-rose-500" : 
                  drawdownTone === "amber" ? "bg-amber-500" : 
                  "bg-emerald-500"
                }`} style={{ width: `${drawdownUsedPct}%` }} />
              </div>
              <div className="font-mono text-[9px] text-[var(--qd-text-3)]">
                Net P&L {netPnl >= 0 ? "+" : ""}{netPnl.toLocaleString()} INR {lossLimit > 0 ? `of ${lossLimit.toLocaleString()} INR limit` : "· no loss limit set"}
              </div>
            </div>
          </div>
        ) : (
          <div className="text-center font-mono text-xs text-[var(--qd-text-3)] py-5 animate-pulse">Loading risk metrics…</div>
        )}
      </div>

      {/* Capital limits */}
      <div className="qd-card p-4">
        <h2 className="font-head font-bold text-white text-xs uppercase tracking-wider border-b border-[var(--qd-border)]/50 pb-2.5 flex items-center gap-2">
          <Sliders size={15} className="text-blue-400" /> Capital &amp; Size Limits
        </h2>
        {profile ? (
          <div className="mt-3 space-y-2">
            <CompactRow label="Per-Strategy Cap" value={`${Number(profile.per_strategy_capital || 0).toLocaleString()} INR`} />
            <CompactRow label="Max Position Size" value={`${Number(profile.max_position_size || 0).toLocaleString()} INR`} />
            <CompactRow label="Default Lot Qty" value={profile.default_qty || "Not set"} />
          </div>
        ) : (
          <div className="text-center font-mono text-xs text-[var(--qd-text-3)] py-5 animate-pulse">Loading size boundaries…</div>
        )}
      </div>

      {/* What the agent can do */}
      <div className="qd-card p-4 text-xs leading-relaxed">
        <h3 className="font-head font-bold text-white uppercase text-xs pb-2.5 mb-2.5 flex items-center gap-2 border-b border-[var(--qd-border)]/50">
          <HelpCircle size={14} className="text-[var(--qd-accent)]" /> What I Can Do
        </h3>

        <div className="font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)] flex items-center gap-1.5 font-bold"><ShieldCheck size={12} className="text-emerald-400" /> Reads automatically</div>
        <ul className="mt-1.5 space-y-0.5 text-[var(--qd-text-2)] font-sans">
          <li>· Orders, positions &amp; execution state</li>
          <li>· Strategies, errors &amp; rejected orders</li>
          <li>· Upstox feed, market status &amp; ticks</li>
          <li>· Daily P&amp;L, loss limit &amp; capital usage</li>
        </ul>

        <div className="mt-2.5 pt-2.5 border-t border-[var(--qd-border)]/50 font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)] flex items-center gap-1.5 font-bold"><Sliders size={12} className="text-blue-400" /> Can change — you approve</div>
        <ul className="mt-1.5 space-y-0.5 text-[var(--qd-text-2)] font-sans">
          <li>· Daily loss limit &amp; max trades / day</li>
          <li>· Position size &amp; per-strategy capital</li>
          <li>· Default qty · Paper ↔ Live (kill)</li>
        </ul>

        <div className="mt-2.5 pt-2.5 border-t border-[var(--qd-border)]/50 flex items-start gap-1.5 text-[var(--qd-text-3)] font-sans">
          <ShieldAlert size={12} className="mt-0.5 text-[var(--qd-loss)] flex-shrink-0" />
          <span>Never places, cancels, or exits trades. Every change needs your click.</span>
        </div>
      </div>
    </div>
  );
}
