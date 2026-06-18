import React from "react";

export const AllocationList = ({ rows }) => (
  <div className="space-y-3">
    {rows.length === 0 ? (
      <div className="py-6 text-center text-xs text-[var(--qd-text-3)]">
        No capital increase recommended from current closed-trade history.
      </div>
    ) : (
      rows.map((row) => (
        <div key={row.strategy_id}>
          <div className="flex items-center justify-between gap-3">
            <div className="truncate text-sm font-semibold text-white">{row.strategy_name}</div>
            <div className="font-mono text-lg font-bold text-[var(--qd-profit)]">{row.recommended_percent}%</div>
          </div>
          <div className="mt-2 h-2 rounded bg-[var(--qd-surface-2)]">
            <div className="h-2 rounded bg-[var(--qd-profit)]" style={{ width: `${Math.min(100, Math.max(0, row.recommended_percent))}%` }} />
          </div>
        </div>
      ))
    )}
  </div>
);

export default AllocationList;
