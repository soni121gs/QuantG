import React from "react";
import { Shield, User, Loader2 } from "lucide-react";

const fmt = (value) => {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
  } catch {
    return String(value);
  }
};

export default function UserReconciler({ pendingUsers, userActionId, onApprove, onReject }) {
  if (pendingUsers.length === 0) return null;

  return (
    <div className="qd-card p-6 border-[var(--qd-cyan)] bg-[var(--qd-surface)]/70 backdrop-blur-md space-y-4 shadow-2xl relative overflow-hidden">
      <div className="absolute top-0 left-0 w-64 h-64 bg-[var(--qd-cyan)]/5 rounded-full blur-[100px] pointer-events-none" />
      <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3 relative z-10">
        <div className="flex items-center gap-2">
          <Shield className="text-[var(--qd-cyan)] animate-pulse" size={20} />
          <h2 className="text-lg font-head font-extrabold text-white">Owner's Registration Desk</h2>
          <span className="text-xs font-mono bg-[var(--qd-cyan)]/15 border border-[var(--qd-cyan)]/30 text-[var(--qd-cyan)] px-2 py-0.5 rounded-full font-bold animate-pulse">
            {pendingUsers.length} PENDING
          </span>
        </div>
        <span className="text-xs font-mono text-[var(--qd-text-3)] uppercase tracking-wider">
          Awaiting Verification
        </span>
      </div>

      <div className="qd-table-wrap relative z-10">
        <table className="w-full text-left text-xs font-mono border-collapse">
          <thead>
            <tr className="border-b border-[var(--qd-border)] text-[var(--qd-text-3)]">
              <th className="py-2.5">TRADER NAME</th>
              <th className="py-2.5">EMAIL ADDRESS</th>
              <th className="py-2.5">REQUEST DATE</th>
              <th className="py-2.5">ROLE</th>
              <th className="py-2.5 text-right">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {pendingUsers.map((p) => (
              <tr key={p.id} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)] transition-colors">
                <td className="py-3 text-white font-semibold flex items-center gap-1.5">
                  <User size={12} className="text-[var(--qd-text-3)]" />
                  {p.name}
                </td>
                <td className="py-3 text-[var(--qd-text-2)]">{p.email}</td>
                <td className="py-3 text-[var(--qd-text-3)]">{fmt(p.created_at)}</td>
                <td className="py-3">
                  <span className="px-1.5 py-0.5 text-[11px] bg-indigo-500/10 border border-indigo-500/30 text-indigo-300 rounded uppercase font-bold">
                    {p.role}
                  </span>
                </td>
                <td className="py-3 text-right space-x-2 flex items-center justify-end">
                  <button
                    onClick={() => onApprove(p.id, p.name)}
                    disabled={userActionId === p.id}
                    className="bg-emerald-500 hover:bg-emerald-600 active:scale-95 disabled:opacity-60 text-emerald-950 font-bold px-3 py-1.5 rounded text-[11px] uppercase tracking-wider transition-all shadow-md shadow-emerald-500/10 flex items-center gap-1.5"
                  >
                    {userActionId === p.id ? <Loader2 size={11} className="animate-spin" /> : null}
                    Approve
                  </button>
                  <button
                    onClick={() => onReject(p.id, p.name)}
                    disabled={userActionId === p.id}
                    className="bg-red-500/15 border border-red-500/30 hover:bg-red-500 hover:text-white disabled:opacity-60 text-[var(--qd-loss)] font-bold px-3 py-1.5 rounded text-[11px] uppercase tracking-wider transition-all"
                  >
                    Reject
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
