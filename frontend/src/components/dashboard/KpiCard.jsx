import React from "react";

export const KpiCard = React.memo(({ label, value, sub, icon: Icon, tone, testid }) => (
  <div className="qd-stat-panel p-3" data-testid={testid || `kpi-${label.replace(/\s+/g, "-").toLowerCase()}`}>
    <div className="flex items-center justify-between gap-2">
      <span className="qd-section-title">{label}</span>
      {Icon && <Icon size={14} className="text-[var(--qd-text-3)]" strokeWidth={1.5} />}
    </div>
    <div className={`mt-2 truncate font-mono text-base font-semibold ${tone || "text-[var(--qd-text)]"}`} title={String(value ?? "")}>{value}</div>
    {sub && <div className="mt-1 truncate text-[11px] text-[var(--qd-text-2)]" title={sub}>{sub}</div>}
  </div>
));
KpiCard.displayName = "KpiCard";

export default KpiCard;
