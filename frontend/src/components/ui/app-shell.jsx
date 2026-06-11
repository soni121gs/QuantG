import React from "react";
import { cn } from "@/lib/utils";

const toneMap = {
  healthy: "border-emerald-400/35 bg-emerald-400/10 text-[var(--qd-profit)]",
  success: "border-emerald-400/35 bg-emerald-400/10 text-[var(--qd-profit)]",
  warning: "border-amber-400/35 bg-amber-400/10 text-[var(--qd-warn)]",
  danger: "border-rose-400/40 bg-rose-400/10 text-[var(--qd-loss)]",
  neutral: "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]",
  live: "border-emerald-400/40 bg-emerald-400/10 text-[var(--qd-profit)]",
  paper: "border-amber-400/40 bg-amber-400/10 text-[var(--qd-warn)]",
};

export function StatusBadge({ tone = "neutral", children, className, ...props }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[10px] font-bold uppercase tracking-wider",
        toneMap[tone] || toneMap.neutral,
        className
      )}
      {...props}
    >
      {children}
    </span>
  );
}

export function PageHeader({ eyebrow, title, subtitle, badge, actions, className }) {
  return (
    <section className={cn("flex flex-col gap-4 border-b border-[var(--qd-border)] pb-4 md:flex-row md:items-end md:justify-between", className)}>
      <div className="min-w-0">
        {eyebrow && <div className="qd-section-title">{eyebrow}</div>}
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="font-head text-2xl font-extrabold text-white md:text-3xl">{title}</h1>
          {badge}
        </div>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-[var(--qd-text-2)]">{subtitle}</p>}
      </div>
      {actions && <ActionGroup className="shrink-0">{actions}</ActionGroup>}
    </section>
  );
}

export function CommandBar({ children, className, ...props }) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 overflow-x-auto border-b border-[var(--qd-border)] bg-white/80 px-3 py-2 backdrop-blur-xl md:px-4",
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export function ActionGroup({ children, className }) {
  return <div className={cn("flex flex-wrap items-center gap-2", className)}>{children}</div>;
}

export function SectionPanel({ title, subtitle, actions, children, className, ...props }) {
  return (
    <section className={cn("qd-card overflow-hidden", className)} {...props}>
      {(title || subtitle || actions) && (
        <div className="flex flex-col gap-3 border-b border-[var(--qd-border)] px-4 py-3 md:flex-row md:items-center md:justify-between">
          <div>
            {title && <h2 className="font-head text-base font-bold text-white">{title}</h2>}
            {subtitle && <p className="mt-1 text-xs text-[var(--qd-text-2)]">{subtitle}</p>}
          </div>
          {actions && <ActionGroup>{actions}</ActionGroup>}
        </div>
      )}
      {children}
    </section>
  );
}

export function MetricCard({ label, value, sub, icon: Icon, tone = "neutral", className, ...props }) {
  const toneClass = {
    success: "text-[var(--qd-profit)]",
    danger: "text-[var(--qd-loss)]",
    warning: "text-[var(--qd-warn)]",
    neutral: "text-white",
  }[tone] || "text-white";

  return (
    <div className={cn("qd-stat-panel p-4", className)} {...props}>
      <div className="flex items-center justify-between gap-3">
        <span className="qd-section-title">{label}</span>
        {Icon && <Icon size={16} className="text-[var(--qd-text-3)]" strokeWidth={1.8} />}
      </div>
      <div className={cn("mt-3 font-mono text-2xl font-bold", toneClass)}>{value}</div>
      {sub && <div className="mt-2 text-xs text-[var(--qd-text-2)]">{sub}</div>}
    </div>
  );
}

export function DataTableShell({ title, subtitle, filters, children, empty, loading, className }) {
  return (
    <SectionPanel title={title} subtitle={subtitle} actions={filters} className={className}>
      {loading ? (
        <div className="p-10 text-center font-mono text-sm text-[var(--qd-text-2)]">Loading...</div>
      ) : empty ? (
        <div className="p-10 text-center font-mono text-sm text-[var(--qd-text-2)]">{empty}</div>
      ) : (
        <div className="qd-table-wrap">{children}</div>
      )}
    </SectionPanel>
  );
}
