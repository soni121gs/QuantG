import React, { useCallback, useEffect, useState } from "react";
import { AlertTriangle, BarChart3, RefreshCw, ShieldCheck, SlidersHorizontal } from "lucide-react";
import { api, formatINR } from "../lib/api";
import { Button } from "../components/ui/button";
import { MetricCard, PageHeader, SectionPanel, StatusBadge } from "../components/ui/app-shell";

const money = (value) => `₹${formatINR(value ?? 0)}`;
const signedTone = (value) => Number(value || 0) >= 0 ? "success" : "danger";
const num = (value, digits = 2) => value === null || value === undefined ? "—" : Number(value).toFixed(digits);

export default function PortfolioIntelligence() {
  const [snapshot, setSnapshot] = useState(null);
  const [scenario, setScenario] = useState(null);
  const [form, setForm] = useState({ move_pct: "-2", iv_points: "5", days: "1" });
  const [loading, setLoading] = useState(true);
  const [scenarioLoading, setScenarioLoading] = useState(false);
  const [error, setError] = useState("");

  const loadSnapshot = useCallback(async () => {
    setLoading(true);
    try {
      const response = await api.get("/ops/portfolio-snapshot");
      setSnapshot(response.data);
      setError("");
    } catch (err) {
      setError(err.response?.data?.detail || "Portfolio snapshot is unavailable.");
    } finally { setLoading(false); }
  }, []);

  const runScenario = async (event) => {
    event?.preventDefault();
    setScenarioLoading(true);
    try {
      const response = await api.post("/ops/portfolio-scenario", {
        move_pct: Number(form.move_pct || 0) / 100,
        iv_points: Number(form.iv_points || 0),
        days: Number(form.days || 0),
      });
      setScenario(response.data);
    } catch (err) {
      setError(err.response?.data?.detail || "Scenario could not be calculated.");
    } finally { setScenarioLoading(false); }
  };

  useEffect(() => { loadSnapshot(); }, [loadSnapshot]);

  const greekRows = ["delta", "gamma", "theta", "vega"];
  return (
    <main className="qd-page space-y-5">
      <PageHeader
        eyebrow="WHOLE PORTFOLIO / ALADDIN-STYLE INTELLIGENCE"
        title="Portfolio Intelligence"
        subtitle="One read-only view of book P&L, defined risk, Greeks, concentration and what-if stress."
        badge={<StatusBadge tone="paper"><ShieldCheck size={12} /> PAPER / ADVISORY</StatusBadge>}
        actions={<Button variant="secondary" size="sm" onClick={loadSnapshot} disabled={loading}><RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh</Button>}
      />

      {error && <div className="rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-sm text-[var(--qd-loss)]"><AlertTriangle size={15} className="mr-2 inline" />{error}</div>}
      <div className="rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 text-xs text-[var(--qd-text-2)]">
        This is an evidence surface, not an execution control. Missing marks or Greeks are shown as missing; Hermes and this page never infer them.
      </div>
      <div className="flex flex-wrap gap-2 text-xs text-[var(--qd-text-2)]"><span className="rounded border border-[var(--qd-border)] px-2 py-1">As of: {snapshot?.as_of ? new Date(snapshot.as_of).toLocaleString("en-IN") : "—"}</span><span className="rounded border border-[var(--qd-border)] px-2 py-1">Position source: {snapshot?.data_quality?.freshness?.positions?.status || "UNKNOWN"}</span><span className="rounded border border-[var(--qd-border)] px-2 py-1">Fill source: {snapshot?.data_quality?.freshness?.fills?.status || "UNKNOWN"}</span></div>
      {snapshot?.risk_alerts?.length > 0 && <SectionPanel title="Risk drivers requiring attention" subtitle="Deterministic alerts from the canonical snapshot; no automatic action is taken.">
        <div className="grid gap-2 p-4 md:grid-cols-2">{snapshot.risk_alerts.map((alert) => <div key={alert.code} className={`rounded-lg border p-3 ${alert.severity === "warning" ? "border-amber-400/30 bg-amber-400/10" : "border-[var(--qd-border)] bg-[var(--qd-surface-2)]"}`}><div className="flex items-center gap-2 text-sm font-semibold text-[var(--qd-text)]"><AlertTriangle size={14} className={alert.severity === "warning" ? "text-[var(--qd-warn)]" : "text-[var(--qd-text-3)]"} />{alert.title}</div><div className="mt-1 text-xs text-[var(--qd-text-2)]">{alert.detail}</div></div>)}</div>
      </SectionPanel>}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Open positions" value={snapshot?.open_positions ?? "—"} icon={BarChart3} />
        <MetricCard label="Realized P&L" value={money(snapshot?.realized_pnl)} tone={signedTone(snapshot?.realized_pnl)} />
        <MetricCard label="Unrealized P&L" value={money(snapshot?.unrealized_pnl)} tone={signedTone(snapshot?.unrealized_pnl)} />
        <MetricCard label="Total P&L" value={money(snapshot?.total_pnl)} tone={signedTone(snapshot?.total_pnl)} />
        <MetricCard label="Defined risk" value={money(snapshot?.defined_risk)} tone="warning" sub="Persisted position risk" />
      </div>

      <div className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
        <SectionPanel title="Portfolio Greeks" subtitle="Aggregated from persisted position data; coverage is shown explicitly.">
          <div className="grid gap-3 p-4 sm:grid-cols-2">
            {greekRows.map((name) => {
              const item = snapshot?.greeks?.[name];
              return <div key={name} className="rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
                <div className="flex justify-between"><span className="font-mono text-xs uppercase text-[var(--qd-text-3)]">{name}</span><span className="font-mono text-sm font-bold text-[var(--qd-text)]">{num(item?.value, 4)}</span></div>
                <div className="mt-2 text-xs text-[var(--qd-text-2)]">Coverage: {item?.covered_positions ?? 0}/{item?.total_positions ?? 0} positions</div>
              </div>;
            })}
          </div>
          {snapshot?.data_quality?.missing_greeks?.length > 0 && <div className="border-t border-[var(--qd-border)] px-4 py-3 text-xs text-[var(--qd-warn)]">Incomplete: {snapshot.data_quality.missing_greeks.join(", ")}</div>}
        </SectionPanel>

        <SectionPanel title="What-if stress lab" subtitle="Approximate Greek stress; never an order recommendation.">
          <form onSubmit={runScenario} className="grid gap-3 p-4 sm:grid-cols-3 xl:grid-cols-1">
            {[['move_pct', 'Underlying move %'], ['iv_points', 'IV change points'], ['days', 'Holding days']].map(([key, label]) => <label key={key} className="text-xs text-[var(--qd-text-2)]">{label}<input className="mt-1 w-full rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-3 py-2 font-mono text-sm text-[var(--qd-text)]" type="number" step="any" value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} /></label>)}
            <Button type="submit" variant="primary" disabled={scenarioLoading}><SlidersHorizontal size={14} /> {scenarioLoading ? "Calculating…" : "Run stress"}</Button>
          </form>
          {scenario && <div className="border-t border-[var(--qd-border)] p-4"><div className="flex items-center justify-between"><span className="text-xs text-[var(--qd-text-2)]">Estimated P&L impact</span><span className={`font-mono text-lg font-bold ${Number(scenario.estimated_pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{scenario.estimated_pnl === null ? "NOT COMPUTABLE" : money(scenario.estimated_pnl)}</span></div><div className="mt-2 text-xs text-[var(--qd-text-3)]">Computed: {scenario.coverage.computed}/{scenario.coverage.total}; missing inputs: {scenario.coverage.missing_inputs}</div></div>}
        </SectionPanel>
      </div>

      <div className="grid gap-5 xl:grid-cols-2">
        {[['by_underlying', 'Risk by underlying'], ['by_strategy', 'Risk by strategy']].map(([key, title]) => <SectionPanel key={key} title={title} subtitle="Sorted by defined risk; no sizing recommendation.">
          <div className="overflow-x-auto"><table className="qd-table"><thead><tr><th>Name</th><th>Positions</th><th>Risk</th><th>Open P&L</th></tr></thead><tbody>{(snapshot?.[key] || []).map((row) => <tr key={row.name}><td className="font-semibold">{row.name}</td><td>{row.positions}</td><td>{money(row.risk)}</td><td className={Number(row.unrealized_pnl) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>{money(row.unrealized_pnl)}</td></tr>)}</tbody></table>{!snapshot?.[key]?.length && <div className="p-6 text-center text-sm text-[var(--qd-text-2)]">No persisted open positions.</div>}</div>
        </SectionPanel>)}
      </div>
      <SectionPanel title="Booked P&L attribution" subtitle="Realized fills grouped by strategy; this is performance attribution, not open-risk exposure.">
        <div className="overflow-x-auto"><table className="qd-table"><thead><tr><th>Strategy</th><th>Fills</th><th>Realized P&L</th></tr></thead><tbody>{(snapshot?.realized_by_strategy || []).map((row) => <tr key={row.name}><td className="font-semibold">{row.name}</td><td>{row.fills}</td><td className={Number(row.realized_pnl) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>{money(row.realized_pnl)}</td></tr>)}</tbody></table>{!snapshot?.realized_by_strategy?.length && <div className="p-6 text-center text-sm text-[var(--qd-text-2)]">No persisted fills available for attribution.</div>}</div>
      </SectionPanel>
    </main>
  );
}
