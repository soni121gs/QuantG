import React, { useCallback, useState } from "react";
import {
  Stethoscope, AlertTriangle, ShieldAlert, Bug, RefreshCw, ChevronDown,
  ChevronRight, Wrench, CheckCircle2, PlayCircle, X, Loader2,
} from "lucide-react";
import { api } from "../../lib/api";
import { toast } from "sonner";
import { usePolling } from "../../hooks/usePolling";

const SEV = {
  critical: { color: "text-[var(--qd-loss)]", bg: "bg-red-500/10", border: "border-red-500/30", label: "CRITICAL" },
  high:     { color: "text-orange-400", bg: "bg-orange-500/10", border: "border-orange-500/30", label: "HIGH" },
  medium:   { color: "text-[var(--qd-warn)]", bg: "bg-yellow-500/10", border: "border-yellow-500/25", label: "MEDIUM" },
  low:      { color: "text-[var(--qd-text-3)]", bg: "bg-[var(--qd-surface)]", border: "border-[var(--qd-border)]", label: "LOW" },
};
const sev = (s) => SEV[s] || SEV.low;
const DOMAIN_ICON = { execution: Bug, strategy: ShieldAlert, infra: AlertTriangle, data: AlertTriangle };

function FindingRow({ f }) {
  const [open, setOpen] = useState(false);
  const s = sev(f.severity);
  const Icon = DOMAIN_ICON[f.domain] || Bug;
  return (
    <div className={`border ${s.border} ${s.bg} rounded-md`}>
      <button onClick={() => setOpen((o) => !o)}
        className="w-full flex items-start gap-2.5 p-3 text-left">
        {open ? <ChevronDown size={14} className="mt-0.5 text-[var(--qd-text-3)] shrink-0" />
              : <ChevronRight size={14} className="mt-0.5 text-[var(--qd-text-3)] shrink-0" />}
        <Icon size={15} className={`mt-0.5 shrink-0 ${s.color}`} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`font-mono text-[10px] font-bold px-1.5 py-0.5 rounded ${s.bg} ${s.color} border ${s.border}`}>{s.label}</span>
            <span className="font-mono text-[10px] text-[var(--qd-text-3)]">{f.probe_id}</span>
          </div>
          <div className="text-sm text-white font-semibold mt-1">{f.title}</div>
        </div>
      </button>
      {open && (
        <div className="px-3 pb-3 pl-9 space-y-2 text-xs">
          <p className="text-[var(--qd-text-2)] leading-relaxed">{f.detail}</p>
          {f.suggested_fix && (
            <div className="flex gap-1.5 text-[var(--qd-text-2)]">
              <Wrench size={13} className="mt-0.5 shrink-0 text-[var(--qd-accent)]" />
              <span><span className="text-[var(--qd-text-3)]">fix:</span> {f.suggested_fix}</span>
            </div>
          )}
          {f.evidence && Object.keys(f.evidence).length > 0 && (
            <pre className="font-mono text-[10px] text-[var(--qd-text-3)] bg-[var(--qd-elevated)] border border-[var(--qd-border)] rounded p-2 overflow-x-auto">
{JSON.stringify(f.evidence, null, 2)}
            </pre>
          )}
          {f.reproduction && (
            <div className="font-mono text-[10px] text-[var(--qd-text-3)] break-all">
              <span className="text-[var(--qd-text-3)]">repro:</span> {f.reproduction}
            </div>
          )}
          <div className="font-mono text-[10px] text-[var(--qd-text-3)]">
            entity: {f.entity} · seen ×{f.occurrences || 1} · first {f.first_seen_date}
          </div>
        </div>
      )}
    </div>
  );
}

export default function DiagnosticsPanel() {
  const [diag, setDiag] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      const [d, t] = await Promise.all([
        api.get("/ops/hermes-diagnostics?status=open"),
        api.get("/ops/hermes-fix-tasks?status=active"),
      ]);
      setDiag(d.data);
      setTasks(t.data?.tasks || []);
    } catch {
      /* usePolling swallows; keep last good state */
    }
  }, []);

  usePolling(load, 30000, { hiddenMs: 0 });

  const runAudit = async () => {
    setBusy("run");
    try {
      const r = await api.post("/ops/hermes-diagnostics/run");
      const s = r.data?.summary?.by_severity || {};
      toast.success(`Audit complete — ${s.critical || 0} critical, ${s.high || 0} high`);
      await load();
    } catch (e) {
      toast.error("Audit failed: " + (e?.response?.data?.detail || e.message));
    } finally {
      setBusy("");
    }
  };

  const setTaskStatus = async (key, status) => {
    setBusy(key + status);
    try {
      await api.post(`/ops/hermes-fix-tasks/${encodeURIComponent(key)}/status`, { status });
      await load();
    } catch (e) {
      toast.error("Update failed: " + (e?.response?.data?.detail || e.message));
    } finally {
      setBusy("");
    }
  };

  const findings = diag?.findings || [];
  const counts = diag?.counts || {};
  const run = diag?.latest_run || {};

  return (
    <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 relative overflow-hidden shadow-xl">
      <div className="absolute top-0 left-0 w-full h-[2px] bg-gradient-to-r from-teal-500 via-cyan-500 to-emerald-600" />

      <div className="flex flex-col sm:flex-row sm:items-center justify-between border-b border-[var(--qd-border)] pb-4 mb-4 gap-3">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 bg-teal-500/10 border border-teal-500/20 text-teal-400 rounded-md">
            <Stethoscope size={18} />
          </div>
          <div>
            <h2 className="font-head text-base font-bold text-white">Hermes Diagnostician</h2>
            <p className="text-[11px] text-[var(--qd-text-3)] font-mono">Daily deterministic system audit · finds, never fixes</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-mono font-semibold text-[var(--qd-loss)] bg-red-500/10 border border-red-500/30 rounded-full">
            {counts.critical || 0} critical
          </span>
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-mono font-semibold text-orange-400 bg-orange-500/10 border border-orange-500/30 rounded-full">
            {counts.high || 0} high
          </span>
          <button onClick={runAudit} disabled={busy === "run"}
            className="px-3 py-1.5 bg-[var(--qd-surface-3)] hover:bg-[var(--qd-surface-2)] text-white rounded border border-[var(--qd-border)] text-xs font-mono flex items-center gap-1.5 transition-all active:scale-95">
            {busy === "run" ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Run audit
          </button>
        </div>
      </div>

      {run.narrative && (
        <div className="mb-4 p-3.5 bg-[var(--qd-elevated)] border border-[var(--qd-border)] rounded-md">
          <div className="text-[10px] font-mono uppercase tracking-wider text-[var(--qd-text-3)] mb-1.5">
            Briefing · {run.date} {run.narrative_source === "llm" ? "(Hermes)" : "(deterministic)"}
          </div>
          <pre className="text-xs text-[var(--qd-text-2)] whitespace-pre-wrap font-sans leading-relaxed">{run.narrative}</pre>
        </div>
      )}

      {/* Fix-task queue */}
      {tasks.length > 0 && (
        <div className="mb-4">
          <div className="text-[10px] font-mono uppercase tracking-wider text-[var(--qd-text-3)] mb-2 flex items-center gap-1.5">
            <Wrench size={12} /> Auto-filed fix-tasks ({tasks.length})
          </div>
          <div className="space-y-2">
            {tasks.map((t) => {
              const s = sev(t.severity);
              return (
                <div key={t.key} className={`flex items-center gap-2 p-2.5 border ${s.border} ${s.bg} rounded-md`}>
                  <span className={`font-mono text-[10px] font-bold px-1.5 py-0.5 rounded ${s.color} border ${s.border}`}>{s.label}</span>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs text-white font-semibold truncate">{t.title}</div>
                    <div className="font-mono text-[10px] text-[var(--qd-text-3)]">{t.probe_id} · {t.status}</div>
                  </div>
                  {t.status === "open" && (
                    <button onClick={() => setTaskStatus(t.key, "in_progress")} disabled={busy === t.key + "in_progress"}
                      className="px-2 py-1 text-[10px] font-mono rounded border border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] flex items-center gap-1">
                      <PlayCircle size={12} /> Start
                    </button>
                  )}
                  <button onClick={() => setTaskStatus(t.key, "done")} disabled={busy === t.key + "done"}
                    className="px-2 py-1 text-[10px] font-mono rounded border border-[var(--qd-profit)]/30 hover:bg-[var(--qd-profit)]/10 text-[var(--qd-profit)] flex items-center gap-1">
                    <CheckCircle2 size={12} /> Done
                  </button>
                  <button onClick={() => setTaskStatus(t.key, "dismissed")} disabled={busy === t.key + "dismissed"}
                    className="px-1.5 py-1 text-[10px] rounded border border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)] text-[var(--qd-text-3)]">
                    <X size={12} />
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Findings */}
      <div className="text-[10px] font-mono uppercase tracking-wider text-[var(--qd-text-3)] mb-2">
        Open findings ({counts.open || findings.length})
      </div>
      {findings.length === 0 ? (
        <div className="flex items-center gap-2 p-4 text-sm text-[var(--qd-profit)] font-mono">
          <CheckCircle2 size={16} /> No open findings — book invariants hold.
        </div>
      ) : (
        <div className="space-y-2">
          {findings.map((f) => <FindingRow key={f.key} f={f} />)}
        </div>
      )}
    </div>
  );
}
