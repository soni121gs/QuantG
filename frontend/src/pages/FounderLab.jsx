import React, { useCallback, useMemo, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  Bot,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  FlaskConical,
  Gauge,
  Layers,
  RefreshCw,
  ShieldCheck,
  Target,
  TrendingDown,
} from "lucide-react";
import { api, formatINR } from "../lib/api";
import { usePolling } from "../hooks/usePolling";
import { toast } from "sonner";

const money = (value) => `INR ${formatINR(value ?? 0)}`;
const pct = (value) => `${(Number(value || 0) * 100).toFixed(1)}%`;
const textOf = (value, fallback = "-") => {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map((item) => textOf(item, "")).filter(Boolean).join(", ") || fallback;
  if (typeof value === "object") return value.status || value.label || value.summary || value.title || fallback;
  return fallback;
};

const dateTime = (value) => {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return String(value);
  }
};

const themeTone = {
  profit_giveback: "text-[var(--qd-loss)] border-[var(--qd-loss)]/35 bg-[color-mix(in_srgb,var(--qd-loss)_8%,var(--qd-surface))]",
  data_freshness: "text-[var(--qd-warn)] border-[var(--qd-warn)]/35 bg-[color-mix(in_srgb,var(--qd-warn)_8%,var(--qd-surface))]",
  regime_disagreement: "text-[var(--qd-cyan)] border-[var(--qd-cyan)]/35 bg-[color-mix(in_srgb,var(--qd-cyan)_8%,var(--qd-surface))]",
  strategy_governor: "text-[var(--qd-accent)] border-[var(--qd-accent)]/35 bg-[color-mix(in_srgb,var(--qd-accent)_8%,var(--qd-surface))]",
};

const actionIcon = {
  profit_giveback: TrendingDown,
  data_freshness: CalendarClock,
  regime_disagreement: Gauge,
  strategy_governor: ShieldCheck,
};

const cardClass = "rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface)] shadow-[var(--qd-shadow)]";
const tabClass = (active) =>
  `inline-flex items-center gap-2 rounded-[var(--qd-radius-sm)] border px-3 py-2 font-head text-xs font-semibold ${
    active
      ? "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-[var(--qd-accent-contrast)]"
      : "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
  }`;

const MetricCard = ({ label, value, sub, icon: Icon, tone = "text-[var(--qd-text)]" }) => (
  <div className={`${cardClass} p-4`}>
    <div className="flex items-center justify-between gap-3">
      <span className="font-mono text-[10px] font-semibold uppercase tracking-widest text-[var(--qd-text-3)]">{label}</span>
      {Icon && <Icon size={15} className="text-[var(--qd-text-3)]" />}
    </div>
    <div className={`mt-3 font-mono text-xl font-bold ${tone}`}>{value}</div>
    {sub && <div className="mt-2 text-xs text-[var(--qd-text-2)]">{sub}</div>}
  </div>
);

const EmptyState = ({ label }) => (
  <div className={`${cardClass} p-6 text-center text-sm text-[var(--qd-text-2)]`}>
    {label}
  </div>
);

const ActionCard = ({ action, index }) => {
  const Icon = actionIcon[action.theme] || ClipboardList;
  const tone = themeTone[action.theme] || "text-[var(--qd-text-2)] border-[var(--qd-border)] bg-[var(--qd-surface-2)]";
  return (
    <div className={`${cardClass} p-4`}>
      <div className="flex items-start gap-3">
        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--qd-radius-sm)] border ${tone}`}>
          <Icon size={16} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] font-bold uppercase tracking-widest text-[var(--qd-text-3)]">
              P{action.priority || index + 1}
            </span>
            <span className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-0.5 font-mono text-[10px] uppercase text-[var(--qd-text-2)]">
              {(action.theme || "review").replaceAll("_", " ")}
            </span>
          </div>
          <h3 className="mt-2 text-base font-semibold text-[var(--qd-text)]">{textOf(action.title, "Review item")}</h3>
          <p className="mt-2 text-sm text-[var(--qd-text-2)]">{textOf(action.why)}</p>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
              <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Next move</div>
              <div className="mt-1 text-sm text-[var(--qd-text)]">{textOf(action.recommended_action)}</div>
            </div>
            <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
              <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Expected benefit</div>
              <div className="mt-1 text-sm text-[var(--qd-text)]">{textOf(action.expected_benefit)}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

const FounderBrief = ({ brief }) => {
  const actions = brief?.recommended_actions || [];
  const today = brief?.today || {};
  const giveback = brief?.profit_giveback || {};
  const governor = brief?.strategy_governor_summary || {};
  return (
    <div className="space-y-4">
      <div className={`${cardClass} p-5`}>
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="font-mono text-[10px] font-semibold uppercase tracking-widest text-[var(--qd-text-3)]">
              Daily Founder Brief · {brief?.date || "-"}
            </div>
            <h2 className="mt-2 max-w-4xl text-xl font-semibold text-[var(--qd-text)]">{brief?.headline || "No brief available yet."}</h2>
          </div>
          <div className="font-mono text-[11px] text-[var(--qd-text-3)]">Generated {dateTime(brief?.generated_at)}</div>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Today P&L" value={money(today.realized_pnl)} icon={BarChart3} tone={(today.realized_pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"} sub={`${today.closed_trades || 0} closes · ${today.open_positions || 0} open`} />
        <MetricCard label="Green Then Red" value={giveback.green_then_loss || 0} icon={TrendingDown} tone="text-[var(--qd-loss)]" sub={`${pct(giveback.pct_losers_green_first)} of losing trades`} />
        <MetricCard label="Peak Profit Lost" value={money(giveback.loss_after_peak)} icon={AlertTriangle} tone="text-[var(--qd-loss)]" sub={`${money(giveback.peak_profit_available)} was available`} />
        <MetricCard label="Strategy Governor" value={`${governor.pause || 0} pause · ${governor.kill_candidate || 0} kill`} icon={ShieldCheck} sub={`${governor.keep || 0} keep · ${governor.watch || 0} watch`} />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        <div className="space-y-3">
          {actions.length ? actions.map((action, i) => <ActionCard key={`${action.theme}-${i}`} action={action} index={i} />) : <EmptyState label="No recommended actions returned by the founder brief." />}
        </div>
        <div className="space-y-4">
          <div className={`${cardClass} p-4`}>
            <div className="flex items-center gap-2 font-head text-sm font-semibold text-[var(--qd-text)]">
              <Bot size={15} /> Hermes Reading List
            </div>
            <div className="mt-3 space-y-3">
              {(brief?.open_findings || []).slice(0, 5).map((finding, i) => (
                <div key={`${finding.probe_id || "finding"}-${i}`} className="border-l-2 border-[var(--qd-border-strong)] pl-3">
                  <div className="text-sm font-semibold text-[var(--qd-text)]">{textOf(finding.title || finding.probe_id, "Open finding")}</div>
                  <div className="mt-1 line-clamp-2 text-xs text-[var(--qd-text-2)]">{textOf(finding.suggested_fix || finding.summary || finding.domain)}</div>
                </div>
              ))}
              {!(brief?.open_findings || []).length && <div className="text-sm text-[var(--qd-text-2)]">No open Hermes findings.</div>}
            </div>
          </div>
          <div className={`${cardClass} p-4`}>
            <div className="flex items-center gap-2 font-head text-sm font-semibold text-[var(--qd-text)]">
              <FlaskConical size={15} /> Research Queue
            </div>
            <div className="mt-3 space-y-3">
              {(brief?.research_hypotheses || []).slice(0, 5).map((h, i) => (
                <div key={h.hypothesis_id || i} className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
                  <div className="text-sm font-semibold text-[var(--qd-text)]">{textOf(h.hypothesis || h.hypothesis_id)}</div>
                  <div className="mt-2 flex flex-wrap gap-2 font-mono text-[10px] uppercase text-[var(--qd-text-3)]">
                    <span>{textOf(h.status, "open")}</span>
                    <span>{textOf(h.verdict, "unjudged")}</span>
                  </div>
                </div>
              ))}
              {!(brief?.research_hypotheses || []).length && <div className="text-sm text-[var(--qd-text-2)]">No hypotheses recorded yet.</div>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

const GivebackLab = ({ lab }) => {
  const summary = lab?.summary || {};
  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Closed Trades" value={summary.closed_trades || 0} icon={ClipboardList} sub={`${summary.losers || 0} losing closes`} />
        <MetricCard label="Green First Losers" value={summary.green_then_loss || 0} icon={TrendingDown} tone="text-[var(--qd-loss)]" sub={`${pct(summary.pct_losers_green_first)} of losers`} />
        <MetricCard label="Open Profit Available" value={money(summary.peak_profit_available)} icon={Target} tone="text-[var(--qd-profit)]" />
        <MetricCard label="Loss After Peak" value={money(summary.loss_after_peak)} icon={AlertTriangle} tone="text-[var(--qd-loss)]" />
      </div>

      <div className={`${cardClass} p-4`}>
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="font-head text-sm font-semibold text-[var(--qd-text)]">Next Lab Action</div>
            <div className="mt-1 text-sm text-[var(--qd-text-2)]">{lab?.next_action || "-"}</div>
          </div>
          <span className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-1 font-mono text-[10px] uppercase text-[var(--qd-text-3)]">
            {lab?.days || 30} days
          </span>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <RankTable title="Strategy Leaks" rows={lab?.by_strategy || []} nameKey="strategy_id" countKey="green_then_loss" />
        <RankTable title="Exit Reason Leaks" rows={lab?.by_exit_reason || []} nameKey="exit_reason" countKey="green_then_loss" />
      </div>

      <div className={`${cardClass} overflow-hidden`}>
        <div className="border-b border-[var(--qd-border)] p-4">
          <h3 className="font-head text-sm font-semibold text-[var(--qd-text)]">Worst Giveback Trades</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead className="bg-[var(--qd-surface-2)] font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">
              <tr>
                <th className="px-4 py-3">Strategy</th>
                <th className="px-4 py-3">Symbol</th>
                <th className="px-4 py-3">Exit</th>
                <th className="px-4 py-3 text-right">Peak</th>
                <th className="px-4 py-3 text-right">Closed</th>
                <th className="px-4 py-3 text-right">Given Back</th>
                <th className="px-4 py-3">Closed At</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--qd-border)]">
              {(lab?.worst_trades || []).slice(0, 12).map((row, i) => (
                <tr key={row.position_id || i} className="text-sm text-[var(--qd-text-2)]">
                  <td className="px-4 py-3 font-mono text-[var(--qd-text)]">{textOf(row.strategy_id)}</td>
                  <td className="px-4 py-3">{textOf(row.target_symbol)}</td>
                  <td className="px-4 py-3">{textOf(row.exit_reason, "unknown")}</td>
                  <td className="px-4 py-3 text-right font-mono text-[var(--qd-profit)]">{money(row.peak_pnl)}</td>
                  <td className="px-4 py-3 text-right font-mono text-[var(--qd-loss)]">{money(row.realized_pnl)}</td>
                  <td className="px-4 py-3 text-right font-mono text-[var(--qd-loss)]">{money(row.profit_given_back)}</td>
                  <td className="px-4 py-3 font-mono text-xs">{dateTime(row.closed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

const RankTable = ({ title, rows, nameKey, countKey }) => (
  <div className={`${cardClass} overflow-hidden`}>
    <div className="border-b border-[var(--qd-border)] p-4">
      <h3 className="font-head text-sm font-semibold text-[var(--qd-text)]">{title}</h3>
    </div>
    <div className="overflow-x-auto">
      <table className="w-full text-left">
        <thead className="bg-[var(--qd-surface-2)] font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">
          <tr>
            <th className="px-4 py-3">Bucket</th>
            <th className="px-4 py-3 text-right">Count</th>
            <th className="px-4 py-3 text-right">Lost After Peak</th>
            <th className="px-4 py-3 text-right">Green First</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--qd-border)]">
          {rows.slice(0, 10).map((row, i) => (
            <tr key={`${row[nameKey]}-${i}`} className="text-sm">
              <td className="px-4 py-3 font-mono text-[var(--qd-text)]">{row[nameKey] || "-"}</td>
              <td className="px-4 py-3 text-right font-mono text-[var(--qd-text-2)]">{row[countKey] || 0}</td>
              <td className="px-4 py-3 text-right font-mono text-[var(--qd-loss)]">{money(row.loss_after_peak)}</td>
              <td className="px-4 py-3 text-right font-mono text-[var(--qd-text-2)]">{pct(row.pct_losing_trades_green_first)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </div>
);

const Dossiers = ({ brief, lab }) => {
  const rows = useMemo(() => {
    const byStrategy = new Map((lab?.by_strategy || []).map((row) => [row.strategy_id, { ...row }]));
    (brief?.recommended_actions || []).forEach((action) => {
      const match = String(action.recommended_action || action.title || "").match(/QG-[A-Z0-9-]+/i);
      if (!match) return;
      const sid = match[0].toUpperCase();
      const row = byStrategy.get(sid) || { strategy_id: sid, green_then_loss: 0, loss_after_peak: 0, peak_available: 0 };
      row.action = action;
      byStrategy.set(sid, row);
    });
    return [...byStrategy.values()].sort((a, b) => (b.loss_after_peak || 0) - (a.loss_after_peak || 0)).slice(0, 16);
  }, [brief, lab]);

  if (!rows.length) return <EmptyState label="No strategy dossiers available from the current founder/giveback data." />;

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {rows.map((row) => (
        <div key={row.strategy_id} className={`${cardClass} p-4`}>
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Strategy dossier</div>
              <h3 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)]">{row.strategy_id}</h3>
            </div>
            <span className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-1 font-mono text-[10px] uppercase text-[var(--qd-text-2)]">
              read-only
            </span>
          </div>
          <div className="mt-4 grid grid-cols-3 gap-2">
            <MiniStat label="Green-red" value={row.green_then_loss || 0} tone="text-[var(--qd-loss)]" />
            <MiniStat label="Lost" value={money(row.loss_after_peak)} tone="text-[var(--qd-loss)]" />
            <MiniStat label="Peak" value={money(row.peak_available)} tone="text-[var(--qd-profit)]" />
          </div>
          <div className="mt-4 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
            <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Review instruction</div>
            <div className="mt-1 text-sm text-[var(--qd-text)]">
              {textOf(row.action?.recommended_action || row.recommended_action, "Replay this strategy's exit path before adding capital.")}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
};

const MiniStat = ({ label, value, tone }) => (
  <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
    <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className={`mt-1 break-words font-mono text-sm font-bold ${tone || "text-[var(--qd-text)]"}`}>{value}</div>
  </div>
);

const Hypotheses = ({ brief }) => {
  const rows = brief?.research_hypotheses || [];
  return (
    <div className="space-y-4">
      <div className={`${cardClass} p-4`}>
        <div className="flex items-center gap-2 font-head text-sm font-semibold text-[var(--qd-text)]">
          <FlaskConical size={15} /> Research Hypothesis Pipeline
        </div>
        <p className="mt-2 max-w-3xl text-sm text-[var(--qd-text-2)]">
          Ideas stay here as research until they have evidence, OOS validation, and founder approval.
        </p>
      </div>
      {rows.length ? (
        <div className="grid gap-3">
          {rows.map((row, i) => (
            <div key={row.hypothesis_id || i} className={`${cardClass} p-4`}>
              <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                <h3 className="font-head text-base font-semibold text-[var(--qd-text)]">{textOf(row.hypothesis || row.hypothesis_id)}</h3>
                <div className="flex flex-wrap gap-2">
                  <span className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-1 font-mono text-[10px] uppercase text-[var(--qd-text-2)]">{textOf(row.status, "open")}</span>
                  <span className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-1 font-mono text-[10px] uppercase text-[var(--qd-text-2)]">{textOf(row.verdict, "unjudged")}</span>
                </div>
              </div>
              <div className="mt-3 font-mono text-[11px] text-[var(--qd-text-3)]">Updated {dateTime(row.updated_at)}</div>
            </div>
          ))}
        </div>
      ) : <EmptyState label="No hypotheses are available yet. Hermes will surface them here once research rows exist." />}
    </div>
  );
};

const stageTone = {
  partial: "border-[var(--qd-cyan)]/40 text-[var(--qd-cyan)]",
  needed: "border-[var(--qd-warn)]/40 text-[var(--qd-warn)]",
  shipped: "border-[var(--qd-profit)]/40 text-[var(--qd-profit)]",
};

const ProfitableMachine = ({ machine }) => {
  const summary = machine?.summary || {};
  const flags = machine?.live_flags || {};
  const giveback = summary.profit_giveback || {};
  const program = machine?.program || [];
  return (
    <div className="space-y-4">
      <div className={`${cardClass} p-5`}>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="font-mono text-[10px] font-semibold uppercase tracking-widest text-[var(--qd-text-3)]">
              Profitable Machine Blueprint
            </div>
            <h2 className="mt-2 max-w-4xl text-xl font-semibold text-[var(--qd-text)]">
              {machine?.headline || "Strict edge factory: research first, execution proof next, live last."}
            </h2>
          </div>
          <div className="font-mono text-[11px] text-[var(--qd-text-3)]">Generated {dateTime(machine?.generated_at)}</div>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Program Items" value={summary.program_items || program.length} icon={Layers} sub={`${summary.stage_counts?.partial || 0} partial · ${summary.stage_counts?.needed || 0} needed`} />
        <MetricCard label="Research Ideas" value={summary.research_hypotheses || 0} icon={FlaskConical} sub="ledger cards available" />
        <MetricCard label="Hermes Findings" value={summary.open_hermes_findings || 0} icon={Bot} sub="open diagnostics" />
        <MetricCard label="Giveback Risk" value={giveback.green_then_loss || 0} icon={TrendingDown} tone="text-[var(--qd-loss)]" sub={`${money(giveback.loss_after_peak)} lost after peak`} />
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        {program.map((item) => (
          <div key={item.id} className={`${cardClass} p-4`}>
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{item.id}</div>
                <h3 className="mt-1 font-head text-base font-semibold text-[var(--qd-text)]">{item.title}</h3>
              </div>
              <span className={`rounded border bg-[var(--qd-surface-2)] px-2 py-1 font-mono text-[10px] uppercase ${stageTone[item.stage] || "border-[var(--qd-border)] text-[var(--qd-text-2)]"}`}>
                {item.stage}
              </span>
            </div>
            <p className="mt-3 text-sm text-[var(--qd-text-2)]">{item.why}</p>
            <div className="mt-4 space-y-3">
              <div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Now</div>
                <div className="mt-1 text-sm text-[var(--qd-text)]">{item.current_quantg_surface}</div>
              </div>
              <div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Next build</div>
                <div className="mt-1 text-sm text-[var(--qd-text)]">{item.next_build}</div>
              </div>
              <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Hard gate</div>
                <div className="mt-1 text-sm text-[var(--qd-text)]">{item.hard_gate}</div>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
        <div className={`${cardClass} p-4`}>
          <div className="flex items-center gap-2 font-head text-sm font-semibold text-[var(--qd-text)]">
            <ShieldCheck size={15} /> Blocking Truths
          </div>
          <div className="mt-3 space-y-2">
            {(machine?.blockers || []).map((blocker, i) => (
              <div key={i} className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 text-sm text-[var(--qd-text-2)]">
                {blocker}
              </div>
            ))}
          </div>
        </div>
        <div className={`${cardClass} p-4`}>
          <div className="flex items-center gap-2 font-head text-sm font-semibold text-[var(--qd-text)]">
            <Gauge size={15} /> Live Flags
          </div>
          <div className="mt-3 space-y-2">
            {Object.entries(flags).map(([key, value]) => (
              <div key={key} className="flex items-center justify-between gap-3 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
                <span className="font-mono text-[10px] uppercase text-[var(--qd-text-3)]">{key}</span>
                <span className={`font-mono text-xs font-bold ${value ? "text-[var(--qd-profit)]" : "text-[var(--qd-text-2)]"}`}>
                  {value ? "true" : "false"}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 text-xs text-[var(--qd-text-2)]">{machine?.note}</div>
        </div>
      </div>
    </div>
  );
};

export default function FounderLab() {
  const [brief, setBrief] = useState(null);
  const [lab, setLab] = useState(null);
  const [machine, setMachine] = useState(null);
  const [activeTab, setActiveTab] = useState("brief");
  const [busy, setBusy] = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [briefRes, labRes, machineRes] = await Promise.all([
        api.get("/ops/daily-founder-brief", { params: { days: 30 } }),
        api.get("/ops/profit-giveback-lab", { params: { days: 30 } }),
        api.get("/ops/profitable-machine", { params: { days: 30 } }),
      ]);
      setBrief(briefRes.data);
      setLab(labRes.data);
      setMachine(machineRes.data);
      setLastRefresh(new Date().toISOString());
    } catch (error) {
      toast.error(error?.response?.data?.detail || "Founder intelligence failed to load");
    } finally {
      setBusy(false);
    }
  }, []);

  usePolling(load, 30000, { hiddenMs: 0 });

  const tabs = [
    { id: "brief", label: "Daily Brief", icon: ClipboardList },
    { id: "giveback", label: "Giveback Lab", icon: TrendingDown },
    { id: "dossiers", label: "Dossiers", icon: Layers },
    { id: "hypotheses", label: "Hypotheses", icon: FlaskConical },
    { id: "machine", label: "Machine", icon: Gauge },
  ];

  return (
    <div className="max-w-7xl space-y-5 pb-10" data-testid="founder-lab">
      <div className="flex flex-col gap-4 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-elevated)] p-4 shadow-[var(--qd-shadow)] md:flex-row md:items-center md:justify-between">
        <div>
          <div className="font-mono text-[10px] font-semibold uppercase tracking-widest text-[var(--qd-text-3)]">
            Founder Intelligence · Hermes read-only
          </div>
          <h1 className="mt-1 font-head text-2xl font-semibold text-[var(--qd-text)]">Decision Brief & Profit Lab</h1>
          <p className="mt-2 max-w-3xl text-sm text-[var(--qd-text-2)]">
            Daily priorities, exit leaks, strategy dossiers, and research ideas in one operating surface.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden font-mono text-[11px] text-[var(--qd-text-3)] md:inline">Updated {dateTime(lastRefresh)}</span>
          <button
            type="button"
            onClick={load}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 font-head text-xs font-semibold text-[var(--qd-text)] hover:bg-[var(--qd-surface-3)] disabled:opacity-60"
          >
            <RefreshCw size={14} className={busy ? "animate-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      <div className="flex gap-2 overflow-x-auto pb-1">
        {tabs.map((tab) => (
          <button key={tab.id} type="button" onClick={() => setActiveTab(tab.id)} className={tabClass(activeTab === tab.id)}>
            <tab.icon size={14} /> {tab.label}
          </button>
        ))}
      </div>

      {!brief || !lab || !machine ? (
        <div className={`${cardClass} flex min-h-[320px] items-center justify-center p-8`}>
          <div className="text-center">
            <RefreshCw size={24} className="mx-auto animate-spin text-[var(--qd-accent)]" />
            <div className="mt-3 font-mono text-xs uppercase tracking-widest text-[var(--qd-text-3)]">Loading founder intelligence</div>
          </div>
        </div>
      ) : (
        <>
          {activeTab === "brief" && <FounderBrief brief={brief} />}
          {activeTab === "giveback" && <GivebackLab lab={lab} />}
          {activeTab === "dossiers" && <Dossiers brief={brief} lab={lab} />}
          {activeTab === "hypotheses" && <Hypotheses brief={brief} />}
          {activeTab === "machine" && <ProfitableMachine machine={machine} />}
        </>
      )}

      <div className="flex items-start gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 text-xs text-[var(--qd-text-2)]">
        <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-[var(--qd-profit)]" />
        <span>{textOf(brief?.note || lab?.note, "Read-only analytics surface. Strategy and trading changes remain founder-approved.")}</span>
      </div>
    </div>
  );
}
