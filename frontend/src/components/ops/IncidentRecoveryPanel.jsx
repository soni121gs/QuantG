import React from "react";
import { CheckCircle2 } from "lucide-react";

export default function IncidentRecoveryPanel({ data, busy, run }) {
  return (
    <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg shadow-xl overflow-hidden">
      <div className="border-b border-[var(--qd-border)] px-5 py-4 flex items-center justify-between bg-[var(--qd-surface)]">
        <div>
          <h2 className="font-head text-base font-bold text-white">Advanced Recovery Engine</h2>
          <p className="text-xs text-[var(--qd-text-3)] font-mono">Automated incident resolution and compliance metrics</p>
        </div>
        <span className={`font-mono text-xs font-semibold px-3 py-1.5 rounded border ${
          data?.recovery_plan?.status === "READY" 
            ? "text-[var(--qd-profit)] bg-[rgba(0,230,118,0.05)] border-[var(--qd-profit)]/30" 
            : "text-[var(--qd-warn)] bg-[rgba(255,159,10,0.05)] border-[var(--qd-warn)]/30"
        }`}>
          RECOVERY COMPLIANCE: {data?.recovery_plan?.status || "HEALTHY"} · {data?.recovery_plan?.score ?? 100}/100
        </span>
      </div>
      
      {!data?.recovery_plan?.issues?.length ? (
        <div className="p-6 text-sm text-[var(--qd-profit)] font-mono flex items-center gap-2">
          <CheckCircle2 size={16} /> All systems operational. Zero blocking issues detected by Risk engine.
        </div>
      ) : (
        <div className="divide-y divide-[rgba(255,255,255,0.05)]">
          {data.recovery_plan.issues.map((item, idx) => (
            <div key={`${item.title}-${idx}`} className="p-4 grid grid-cols-1 md:grid-cols-[120px_1fr_auto] gap-4 items-center">
              <span className={`font-mono text-[11px] uppercase font-bold tracking-wider px-2 py-1 rounded inline-block text-center ${
                item.severity === "critical" 
                  ? "text-[var(--qd-loss)] bg-red-500/10 border border-red-500/20" 
                  : item.severity === "warning" 
                    ? "text-[var(--qd-warn)] bg-orange-500/10 border border-orange-500/20" 
                    : "text-[var(--qd-text-3)] bg-[var(--qd-surface-2)] border border-[var(--qd-border)]"
              }`}>
                {item.severity}
              </span>
              <div>
                <div className="text-white font-semibold text-sm">{item.title}</div>
                <div className="text-xs text-[var(--qd-text-2)] mt-0.5">{item.detail}</div>
                <div className="text-xs font-mono text-indigo-400 mt-1.5 flex items-center gap-1">
                  <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-ping"></span> Suggested Action: {item.action}
                </div>
              </div>
              {item.endpoint && (
                <button 
                  onClick={() => run(`plan-${idx}`, item.endpoint)} 
                  disabled={busy === `plan-${idx}`}
                  className="border border-indigo-500/40 hover:border-indigo-400 hover:bg-indigo-500/10 px-4 py-2 rounded text-xs font-mono uppercase text-white shadow-md transition-all active:scale-95"
                >
                  {busy === `plan-${idx}` ? "Deploying..." : "Auto Fix"}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
