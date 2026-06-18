import React from "react";

export const KpiCard = ({ label, value, sub, icon: Icon, tone, testid }) => (
  <div className="qd-stat-panel p-4" data-testid={testid || `kpi-${label.replace(/\s+/g, "-").toLowerCase()}`}>
    <div className="flex items-center justify-between gap-3">
      <span className="qd-section-title">{label}</span>
      {Icon && <Icon size={16} className="text-[var(--qd-text-3)]" strokeWidth={1.5} />}
    </div>
    <div className={`mt-3 font-mono text-2xl font-bold tracking-tight ${tone || "text-white"}`}>{value}</div>
    {sub && <div className="mt-2 text-xs text-[var(--qd-text-2)]">{sub}</div>}
  </div>
);

export default KpiCard;
