import React from "react";

export const HealthScoreList = ({ rows }) => (
  <div className="space-y-3">
    {rows.length === 0 ? (
      <div className="py-6 text-center text-xs text-[var(--qd-text-3)]">No health scores yet.</div>
    ) : (
      rows.slice(0, 8).map((row) => {
        const score = Number(row.health_score || 0);
        const tone = score >= 70 ? "bg-[var(--qd-profit)]" : score >= 45 ? "bg-[var(--qd-warn)]" : "bg-[var(--qd-loss)]";
        return (
          <div key={row.strategy_id}>
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-white">{row.strategy_name}</div>
                <div className="font-mono text-[10px] uppercase text-[var(--qd-text-3)]">{row.recommendation}</div>
              </div>
              <div className="font-mono text-lg font-bold text-white">{score}</div>
            </div>
            <div className="mt-2 h-2 rounded bg-[var(--qd-surface-2)]">
              <div className={`h-2 rounded ${tone}`} style={{ width: `${Math.min(100, Math.max(0, score))}%` }} />
            </div>
          </div>
        );
      })
    )}
  </div>
);

export default HealthScoreList;
