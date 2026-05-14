import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { Code2, Blocks, Play, Pause, Trash2, Plus } from "lucide-react";

export default function Strategies() {
  const [list, setList] = useState([]);

  const load = () => api.get("/strategies").then((r) => setList(r.data));
  useEffect(() => { load(); }, []);

  const toggle = async (id) => {
    await api.post(`/strategies/${id}/toggle`);
    load();
  };
  const del = async (id) => {
    if (!window.confirm("Delete strategy?")) return;
    await api.delete(`/strategies/${id}`);
    load();
  };

  return (
    <div className="space-y-4" data-testid="strategies-page">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// STRATEGIES</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1">My Strategies</h1>
        </div>
        <div className="flex gap-2">
          <Link to="/python" className="border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2" data-testid="new-python-btn"><Code2 size={14} /> Python</Link>
          <Link to="/visual" className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2" data-testid="new-visual-btn"><Blocks size={14} /> Visual Builder</Link>
        </div>
      </div>

      {list.length === 0 ? (
        <div className="qd-card p-16 text-center">
          <Plus className="mx-auto text-[var(--qd-text-3)] mb-3" />
          <p className="font-mono text-sm text-[var(--qd-text-2)]">No strategies yet. Build your first one.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {list.map((s) => (
            <div key={s.id} className="qd-card p-4 flex flex-col gap-3" data-testid={`strategy-${s.id}`}>
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{s.kind === "python" ? "// PYTHON" : "// VISUAL"}</div>
                  <h3 className="font-head text-lg text-white mt-1">{s.name}</h3>
                </div>
                <span className={`font-mono text-[10px] uppercase px-2 py-1 rounded-sm ${
                  s.status === "live" ? "bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]" :
                  s.status === "paused" ? "bg-[rgba(255,159,10,0.1)] text-[var(--qd-warn)]" :
                  "bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]"
                }`}>{s.status}</span>
              </div>
              <p className="text-xs text-[var(--qd-text-2)] line-clamp-2 min-h-[2.5rem]">{s.description || "No description"}</p>
              <div className="flex items-center justify-between text-xs font-mono">
                <span className="text-[var(--qd-text-3)]">PnL</span>
                <span className={`${(s.last_pnl ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                  {s.last_pnl != null ? `₹${s.last_pnl.toLocaleString("en-IN")}` : "Never run"}
                </span>
              </div>
              <div className="flex gap-2 pt-2 border-t border-[var(--qd-border)]">
                <button onClick={() => toggle(s.id)} className="flex-1 border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase py-1.5 rounded-sm flex items-center justify-center gap-1" data-testid={`toggle-${s.id}`}>
                  {s.status === "live" ? <><Pause size={12} /> Pause</> : <><Play size={12} /> Go Live</>}
                </button>
                <Link to={s.kind === "python" ? `/python?id=${s.id}` : `/visual?id=${s.id}`} className="border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase py-1.5 px-3 rounded-sm" data-testid={`edit-${s.id}`}>Edit</Link>
                <button onClick={() => del(s.id)} className="border border-[var(--qd-border)] hover:border-[var(--qd-loss)] text-[var(--qd-loss)] py-1.5 px-2 rounded-sm" data-testid={`delete-${s.id}`}><Trash2 size={12} /></button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
