import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BarChart3, RefreshCw, TrendingUp, TrendingDown, ArrowUpDown,
  Activity, AlertTriangle, FlaskConical, Layers, ShieldCheck, CircleSlash,
  Database, Target, Zap,
} from "lucide-react";
import { api, formatINR } from "../lib/api";
import { toast } from "sonner";

const money = (v) => `INR ${formatINR(v ?? 0)}`;
const pct = (v) => `${Number(v ?? 0).toFixed(1)}%`;
const num = (v, d = 2) => (v == null || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(d));
const signCls = (v) => ((v ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]");

const timeAgo = (iso) => {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

// Honest letter-grade → colour. Mirrors core/metrics.grade thresholds.
const GRADE_TONE = {
  A: { bg: "bg-[rgba(0,230,118,0.14)]", text: "text-[var(--qd-profit)]", br: "border-[var(--qd-profit)]/40" },
  B: { bg: "bg-[rgba(0,230,118,0.10)]", text: "text-[var(--qd-profit)]", br: "border-[var(--qd-profit)]/25" },
  C: { bg: "bg-[rgba(255,159,10,0.12)]", text: "text-[var(--qd-warn)]", br: "border-[var(--qd-warn)]/30" },
  D: { bg: "bg-[rgba(255,159,10,0.10)]", text: "text-[var(--qd-warn)]", br: "border-[var(--qd-warn)]/25" },
  F: { bg: "bg-[rgba(255,59,48,0.12)]", text: "text-[var(--qd-loss)]", br: "border-[var(--qd-loss)]/30" },
  INSUFFICIENT: { bg: "bg-[var(--qd-surface-3)]", text: "text-[var(--qd-text-3)]", br: "border-[var(--qd-border)]" },
};

const GradeChip = ({ grade }) => {
  const t = GRADE_TONE[grade] || GRADE_TONE.INSUFFICIENT;
  const label = grade === "INSUFFICIENT" ? "N/A" : grade;
  return (
    <span
      className={`inline-flex min-w-[2.1rem] items-center justify-center rounded-md border px-2 py-0.5 font-mono text-[11px] font-bold ${t.bg} ${t.text} ${t.br}`}
      title={grade === "INSUFFICIENT" ? "Fewer than 5 trades — not enough to grade" : `Grade ${grade}`}
    >
      {label}
    </span>
  );
};

// OOS walk-forward verdict → tone. This is the truth column: is there an edge
// that persists out-of-sample, or is it noise to archive?
const VERDICT_TONE = {
  CANDIDATE_EDGE: { text: "text-[var(--qd-profit)]", br: "border-[var(--qd-profit)]/40", label: "CANDIDATE EDGE" },
  FRAGILE: { text: "text-[var(--qd-warn)]", br: "border-[var(--qd-warn)]/35", label: "FRAGILE" },
  NO_EDGE_NEGATIVE: { text: "text-[var(--qd-loss)]", br: "border-[var(--qd-loss)]/35", label: "NO EDGE" },
  INSUFFICIENT_DATA: { text: "text-[var(--qd-text-3)]", br: "border-[var(--qd-border)]", label: "THIN" },
  ERROR: { text: "text-[var(--qd-text-3)]", br: "border-[var(--qd-border)]", label: "SKIPPED" },
};
const VerdictChip = ({ verdict }) => {
  const t = VERDICT_TONE[verdict] || VERDICT_TONE.ERROR;
  return (
    <span className={`inline-flex rounded-md border px-2 py-1 font-mono text-[10px] font-bold ${t.text} ${t.br}`}>
      {t.label}
    </span>
  );
};

// Real equity-curve sparkline driven by the strategy's actual cumulative P&L.
const EquitySparkline = ({ curve }) => {
  if (!curve || curve.length < 2) {
    return <span className="font-mono text-[11px] text-[var(--qd-text-3)]">—</span>;
  }
  const w = 96, h = 26, pad = 2;
  const min = Math.min(...curve);
  const max = Math.max(...curve);
  const span = max - min || 1;
  const stepX = (w - pad * 2) / (curve.length - 1);
  const pts = curve.map((v, i) => {
    const x = pad + i * stepX;
    const y = pad + (h - pad * 2) * (1 - (v - min) / span);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const up = curve[curve.length - 1] >= curve[0];
  const stroke = up ? "var(--qd-profit)" : "var(--qd-loss)";
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-6 w-24" aria-hidden="true">
      <polyline points={pts.join(" ")} fill="none" stroke={stroke} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
};

const StructureCard = ({ name, agg }) => {
  const seller = name === "credit_spread";
  const label = name === "credit_spread" ? "Credit spreads (sellers)"
    : name === "debit_spread" ? "Debit spreads"
    : name === "single_leg" ? "Single-leg (buyers)" : name;
  const Icon = seller ? Layers : Activity;
  const pnlPos = (agg.total_pnl ?? 0) >= 0;
  return (
    <div className="qd-card p-4">
      <div className="flex items-center justify-between">
        <div className="qd-section-title flex items-center gap-1.5"><Icon size={13} /> {label}</div>
        <span className="font-mono text-[11px] text-[var(--qd-text-3)]">{agg.strategies} strat · {agg.total_trades} tr</span>
      </div>
      <div className={`mt-2 font-head text-2xl font-bold ${pnlPos ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{money(agg.total_pnl)}</div>
      <div className="mt-1 grid grid-cols-2 gap-2 font-mono text-[11px] text-[var(--qd-text-2)]">
        <div>Win rate: <span className="text-[var(--qd-text)]">{pct((agg.win_rate ?? 0) * 100)}</span></div>
        <div>Expectancy: <span className={signCls(agg.expectancy)}>{money(agg.expectancy)}</span></div>
      </div>
    </div>
  );
};

const SortHeader = ({ label, col, sort, onSort, className = "" }) => (
  <th className={`px-3 py-2.5 ${className}`}>
    <button
      type="button"
      onClick={() => onSort(col)}
      className={`inline-flex items-center gap-1 font-mono text-[11px] uppercase tracking-widest hover:text-[var(--qd-text)] ${
        sort.col === col ? "text-[var(--qd-accent)]" : "text-[var(--qd-text-3)]"
      }`}
    >
      {label}
      <ArrowUpDown size={10} className={sort.col === col ? "opacity-100" : "opacity-40"} />
    </button>
  </th>
);

const SummaryTile = ({ label, value, sub, tone = "neutral", icon: Icon }) => {
  const toneClass = {
    good: "text-[var(--qd-profit)]",
    bad: "text-[var(--qd-loss)]",
    warn: "text-[var(--qd-warn)]",
    neutral: "text-[var(--qd-text)]",
  }[tone] || "text-[var(--qd-text)]";
  return (
    <div className="qd-stat-panel p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="qd-section-title">{label}</span>
        {Icon && <Icon size={15} className="text-[var(--qd-text-3)]" />}
      </div>
      <div className={`mt-3 font-mono text-xl font-bold ${toneClass}`}>{value}</div>
      {sub && <div className="mt-2 text-xs text-[var(--qd-text-2)]">{sub}</div>}
    </div>
  );
};

const verdictTone = (verdict) => (
  verdict === "KEEP" ? "good" : verdict === "KILL" ? "bad" : "warn"
);

// ---- Edge Lab (OOS research surface) ---------------------------------------

// One short-vol cell: mean ₹/cycle (win-rate), tinted by sign. Tooltip carries n / sum / worst.
const VolCell = ({ s, highlight }) => {
  if (!s) return <td className="px-3 py-2 text-center font-mono text-[11px] text-[var(--qd-text-3)]">—</td>;
  return (
    <td className={`px-3 py-2 text-center ${highlight ? "bg-[var(--qd-surface-2)]" : ""}`}
        title={`n=${s.n} cycles · Σ ${money(s.sum_rupees)} · worst ${num(s.worst_pts, 0)} pts`}>
      <div className={`font-mono text-sm font-semibold ${signCls(s.mean_rupees)}`}>{money(s.mean_rupees)}</div>
      <div className="font-mono text-[11px] text-[var(--qd-text-3)]">{pct(s.win_rate)} WR</div>
    </td>
  );
};

// FE-04: propose a hypothetical option structure → run it through the OOS
// walk-forward backtester → see the verdict BEFORE anything is seeded live.
const F_UNDER = ["NIFTY", "BANKNIFTY", "SENSEX"];
const F_STRUCT = [["credit_spread", "Credit spread (1-sided)"], ["iron_condor", "Iron condor"],
  ["single_leg", "Single leg (buy)"], ["debit_spread", "Debit spread"]];
const F_DIR = [["bullish", "Bullish / put side"], ["bearish", "Bearish / call side"]];

const Field = ({ label, children }) => (
  <label className="flex flex-col gap-1">
    <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</span>
    {children}
  </label>
);
const inputCls = "rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-1.5 font-mono text-xs text-[var(--qd-text)] focus:border-[var(--qd-accent)] focus:outline-none";

function Proposer() {
  const [form, setForm] = useState({ underlying: "NIFTY", structure: "credit_spread",
    direction: "bullish", short_otm_pct: 0.03, spread_width: 6, wing_width: 6, max_dte: 8, exit_mode: "expiry" });
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const run = async () => {
    setRunning(true); setResult(null);
    try {
      const r = await api.post("/ops/edge-lab/propose", {
        ...form,
        short_otm_pct: Number(form.short_otm_pct), spread_width: Number(form.spread_width),
        wing_width: Number(form.wing_width), max_dte: Number(form.max_dte),
      });
      setResult(r.data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "OOS test failed");
    } finally { setRunning(false); }
  };
  const isCondor = form.structure === "iron_condor";
  const o = result?.overall;
  return (
    <section className="qd-card p-5">
      <div className="qd-section-title flex items-center gap-1.5"><Target size={13} /> // Propose &amp; OOS-test</div>
      <h2 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)]">Test a structure before you seed it</h2>
      <p className="mt-0.5 text-xs text-[var(--qd-text-2)]">Walk-forward on 2yr real bhavcopy. Nothing is seeded — this is the OOS-first discipline as a button.</p>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        <Field label="Underlying"><select className={inputCls} value={form.underlying} onChange={set("underlying")}>{F_UNDER.map((u) => <option key={u} value={u}>{u}</option>)}</select></Field>
        <Field label="Structure"><select className={inputCls} value={form.structure} onChange={set("structure")}>{F_STRUCT.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
        <Field label="Side"><select className={inputCls} value={form.direction} onChange={set("direction")}>{F_DIR.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
        <Field label="Short OTM %"><input className={inputCls} type="number" step="0.005" value={form.short_otm_pct} onChange={set("short_otm_pct")} /></Field>
        <Field label={isCondor ? "Wing (strikes)" : "Width (strikes)"}><input className={inputCls} type="number" value={isCondor ? form.wing_width : form.spread_width} onChange={set(isCondor ? "wing_width" : "spread_width")} /></Field>
        <Field label="Max DTE"><input className={inputCls} type="number" value={form.max_dte} onChange={set("max_dte")} /></Field>
        <Field label="Exit"><select className={inputCls} value={form.exit_mode} onChange={set("exit_mode")}><option value="expiry">Hold to expiry</option><option value="">Intraday tp/sl</option></select></Field>
      </div>
      <button type="button" onClick={run} disabled={running}
        className="mt-3 inline-flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-accent)]/40 bg-[var(--qd-surface-2)] px-4 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:bg-[var(--qd-surface-3)] disabled:opacity-50">
        <FlaskConical size={14} className={running ? "animate-spin" : ""} /> {running ? "Running OOS…" : "Run OOS test"}
      </button>
      {result && (
        <div className="mt-4 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-4">
          {result.error ? (
            <div className="font-mono text-xs text-[var(--qd-loss)]">{result.error}</div>
          ) : (
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
              <VerdictChip verdict={result.verdict} />
              <span className="font-mono text-xs text-[var(--qd-text-2)]">n=<b className="text-[var(--qd-text)]">{o?.n}</b></span>
              <span className="font-mono text-xs">exp <b className={signCls(o?.expectancy)}>{money(o?.expectancy)}</b>/trade</span>
              <span className="font-mono text-xs text-[var(--qd-text-2)]">WR <b className="text-[var(--qd-text)]">{pct(o?.win_rate)}</b></span>
              <span className="font-mono text-xs text-[var(--qd-text-2)]">green {pct(result.pct_green_months)}</span>
              <span className="font-mono text-[11px] text-[var(--qd-text-3)]">by year: {Object.entries(result.by_year || {}).map(([y, b]) => `${y} ${money(b.expectancy)}`).join(" · ")}</span>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function EdgeLab({ data, loading, onRefresh }) {
  const building = data?.building || data?.status === "building";

  if (loading && !data) {
    return <div className="qd-card p-10 text-center font-mono text-xs text-[var(--qd-text-3)]">Loading Edge Lab…</div>;
  }
  if (!data || data.status === "empty") {
    return (
      <div className="qd-card p-10 text-center">
        <FlaskConical className="mx-auto mb-3 text-[var(--qd-text-3)]" size={24} />
        <div className="text-sm text-[var(--qd-text)]">No Edge Lab snapshot yet.</div>
        <div className="mx-auto mt-1 max-w-md text-xs text-[var(--qd-text-2)]">
          {data?.hint || "Builds an out-of-sample verdict for the whole book from 2 years of real NSE/BSE option settlement prices."}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={building}
          className="mt-4 inline-flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-accent)]/40 bg-[var(--qd-surface-2)] px-4 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-accent)] hover:bg-[var(--qd-surface-3)] disabled:opacity-50"
        >
          <RefreshCw size={14} className={building ? "animate-spin" : ""} /> {building ? "Building…" : "Build snapshot"}
        </button>
      </div>
    );
  }

  if (data.status === "error") {
    return (
      <div className="qd-card border-l-2 border-l-[var(--qd-loss)] p-6">
        <div className="qd-section-title text-[var(--qd-loss)]">Edge Lab build failed</div>
        <div className="mt-1 font-mono text-xs text-[var(--qd-text-2)]">{data.error}</div>
        <button type="button" onClick={onRefresh} className="mt-3 font-mono text-xs text-[var(--qd-accent)]">Retry</button>
      </div>
    );
  }

  const cov = data.coverage || {};
  const shortVol = data.base_rate?.short_vol || [];
  const directional = data.base_rate?.directional || [];
  const oos = data.oos || {};
  const oosCounts = oos.counts || {};
  const sweep = data.sweep || [];

  return (
    <div className="space-y-5">
      {/* Freshness / build bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-4 py-2.5">
        <div className="flex items-center gap-2 font-mono text-[11px] text-[var(--qd-text-3)]">
          <Database size={13} />
          {data.book && (
            <span className="text-[var(--qd-text-2)]">
              {data.book.active_strategies || 0} active / {data.book.archived_strategies || 0} archived hidden
            </span>
          )}
          {building ? "Rebuilding snapshot (this can take a few minutes)…" : `Snapshot ${timeAgo(data.generated_at)}`}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={building}
          className="inline-flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)] disabled:opacity-50"
        >
          <RefreshCw size={13} className={building ? "animate-spin" : ""} /> {building ? "Building" : "Rebuild"}
        </button>
      </div>

      <Proposer />

      {/* Data coverage — honesty about what can/can't be tested */}
      <section className="qd-card p-5">
        <div className="qd-section-title flex items-center gap-1.5"><Database size={13} /> // Real data coverage</div>
        <div className="mt-2 flex flex-wrap items-baseline gap-x-6 gap-y-1">
          <div className="font-head text-lg font-semibold text-[var(--qd-text)]">
            {cov.first} → {cov.last}
          </div>
          <div className="font-mono text-xs text-[var(--qd-text-2)]">{cov.n_days} trading days · NSE/BSE F&O settlement prices</div>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {(cov.underlyings || []).map((u) => (
            <span key={u.underlying} className="rounded-md border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2.5 py-1 font-mono text-[11px] text-[var(--qd-text-2)]"
                  title={`${u.first} → ${u.last}`}>
              {u.underlying} <span className="text-[var(--qd-text-3)]">· {u.days}d</span>
            </span>
          ))}
        </div>
        {(cov.gaps || []).length > 0 && (
          <div className="mt-3 space-y-1">
            {cov.gaps.map((g, i) => (
              <div key={i} className="flex items-start gap-1.5 font-mono text-[11px] text-[var(--qd-text-3)]">
                <CircleSlash size={12} className="mt-0.5 shrink-0" /> {g}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Base-rate finding — the short-OTM-vol edge */}
      <section className="qd-card overflow-hidden">
        <div className="border-b border-[var(--qd-border)] px-5 py-4">
          <div className="qd-section-title flex items-center gap-1.5"><Target size={13} /> // Where the edge lives</div>
          <h2 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)]">Short-vol base rate — sell &amp; hold to expiry</h2>
          <p className="mt-0.5 text-xs text-[var(--qd-text-2)]">
            Per-cycle P&amp;L of selling a strangle and holding to weekly settlement (entry slippage {pct((data.base_rate?.slippage_pct ?? 0) * 100)}/leg).
            The vol risk premium is real and grows the further OTM you sell — the lever is distance, not regime.
          </p>
        </div>
        <div className="qd-table-wrap overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)] font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">
                <th className="px-3 py-2.5">Underlying</th>
                <th className="px-3 py-2.5 text-center">ATM straddle</th>
                <th className="px-3 py-2.5 text-center">~1% OTM</th>
                <th className="px-3 py-2.5 text-center">~2% OTM <span className="text-[var(--qd-accent)]">◆</span></th>
              </tr>
            </thead>
            <tbody>
              {shortVol.map((r) => (
                <tr key={r.underlying} className="border-b border-[var(--qd-border)]">
                  <td className="px-3 py-2 font-mono font-semibold text-[var(--qd-text)]">{r.underlying}<span className="ml-1 text-[11px] text-[var(--qd-text-3)]">lot {r.lot}</span></td>
                  <VolCell s={r.atm} />
                  <VolCell s={r.otm_1pct} />
                  <VolCell s={r.otm_2pct} highlight />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="border-t border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-5 py-3 space-y-1 font-mono text-[11px] text-[var(--qd-text-3)]">
          <div><Zap size={11} className="mr-1 inline text-[var(--qd-warn)]" /><span className="text-[var(--qd-warn)]">Naked = undefined risk.</span> These are the raw edge, not yet deployable — the next build is a defined-risk iron condor (buy wings to cap the tail).</div>
          <div>
            No directional edge: {directional.map((d) => `${d.underlying} ${num(d.continuation_wr, 0)}% cont / ${num(d.mean_fwd_return_pct, 2)}% fwd`).join(" · ")} — ≈ random walk. Stop building buyers.
          </div>
        </div>
      </section>

      {/* Per-strategy OOS verdict — the truth about the live book */}
      <section className="qd-card overflow-hidden">
        <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-5 py-4">
          <div>
            <div className="qd-section-title flex items-center gap-1.5"><ShieldCheck size={13} /> // Walk-forward truth</div>
            <h2 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)]">Out-of-sample verdict</h2>
          </div>
          <div className="flex flex-wrap justify-end gap-1.5 font-mono text-[11px]">
            <span className="text-[var(--qd-profit)]">{oosCounts.CANDIDATE_EDGE || 0} edge</span>
            <span className="text-[var(--qd-text-3)]">·</span>
            <span className="text-[var(--qd-warn)]">{oosCounts.FRAGILE || 0} fragile</span>
            <span className="text-[var(--qd-text-3)]">·</span>
            <span className="text-[var(--qd-loss)]">{oosCounts.NO_EDGE_NEGATIVE || 0} no-edge</span>
            <span className="text-[var(--qd-text-3)]">·</span>
            <span className="text-[var(--qd-text-3)]">{(oosCounts.INSUFFICIENT_DATA || 0) + (oosCounts.ERROR || 0)} thin</span>
          </div>
        </div>
        <div className="qd-table-wrap overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)] font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">
                <th className="px-3 py-2.5">Strategy</th>
                <th className="px-3 py-2.5">Verdict</th>
                <th className="px-3 py-2.5 text-right">Trades</th>
                <th className="px-3 py-2.5 text-right">Expectancy</th>
                <th className="px-3 py-2.5 text-right">OOS exp</th>
                <th className="px-3 py-2.5 text-right">Win%</th>
                <th className="px-3 py-2.5 text-right">Green mo</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {(oos.rows || []).map((r, i) => (
                <tr key={i} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                  <td className="px-3 py-2.5">
                    <div className="font-semibold text-[var(--qd-text)]">{r.name}</div>
                    <div className="text-[11px] text-[var(--qd-text-3)]">{r.error ? r.error : `${r.structure || "-"}${r.underlying ? ` · ${r.underlying}` : ""}`}</div>
                  </td>
                  <td className="px-3 py-2.5"><VerdictChip verdict={r.verdict || "ERROR"} /></td>
                  <td className="px-3 py-2.5 text-right text-[var(--qd-text-2)]">{r.n ?? "-"}</td>
                  <td className={`px-3 py-2.5 text-right ${signCls(r.expectancy)}`}>{r.error ? "-" : money(r.expectancy)}</td>
                  <td className={`px-3 py-2.5 text-right ${signCls(r.oos_expectancy)}`}>{r.error ? "-" : money(r.oos_expectancy)}</td>
                  <td className="px-3 py-2.5 text-right text-[var(--qd-text-2)]">{r.win_rate != null ? pct(r.win_rate) : "-"}</td>
                  <td className="px-3 py-2.5 text-right text-[var(--qd-text-2)]">{r.pct_green_months != null ? pct(r.pct_green_months) : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Sweep evidence — the credit-spread geometry is structurally negative */}
      {sweep.length > 0 && (
        <section className="qd-card overflow-hidden">
          <div className="border-b border-[var(--qd-border)] px-5 py-4">
            <div className="qd-section-title flex items-center gap-1.5"><FlaskConical size={13} /> // Exit-geometry sweep</div>
            <h2 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)]">Can tuning save the credit-spread book?</h2>
            <p className="mt-0.5 text-xs text-[var(--qd-text-2)]">A grid over take-profit × stop × width × DTE, each run through the OOS validator. If nothing crosses positive OOS, the geometry is structurally negative — not tunable.</p>
          </div>
          <div className="grid grid-cols-1 gap-4 p-5 md:grid-cols-2">
            {sweep.map((s) => {
              const dead = (s.positive_oos || 0) === 0;
              return (
                <div key={s.name} className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-4">
                  <div className="font-mono text-xs font-semibold text-[var(--qd-text)]">{s.name}</div>
                  <div className={`mt-2 font-head text-2xl font-bold ${dead ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"}`}>
                    {s.positive_oos} / {s.configs}
                  </div>
                  <div className="font-mono text-[11px] text-[var(--qd-text-3)]">configs positive out-of-sample · {s.candidate_edges} candidate-edge</div>
                  <div className={`mt-2 font-mono text-[11px] ${dead ? "text-[var(--qd-loss)]" : "text-[var(--qd-warn)]"}`}>
                    {dead ? "Structurally negative — no config is tunable to an edge." : "Some configs survive — validate before trusting."}
                  </div>
                  {(s.cells || []).slice(0, 3).length > 0 && (
                    <div className="mt-3 border-t border-[var(--qd-border)] pt-2 font-mono text-[10px] text-[var(--qd-text-3)]">
                      best OOS: {(s.cells || []).slice(0, 3).map((c) => `tp${c.tp}/sl${c.sl}/w${c.width}/d${c.dte}=${money(c.oos_expectancy)}`).join("  ·  ")}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

const VERDICT_CLS = {
  CANDIDATE_EDGE: "text-[var(--qd-profit)]",
  FRAGILE: "text-[var(--qd-warn,#d9a441)]",
  NO_EDGE_NEGATIVE: "text-[var(--qd-loss)]",
  INSUFFICIENT_DATA: "text-[var(--qd-text-3)]",
  DATA_QUALITY_FAIL: "text-[var(--qd-loss)]",
};

// IMD-09: intraday 1-minute OOS for QG-O5..QG-O10 — clearly labelled
// separate from the EOD theta OOS above so users never mix the two judges.
function IntradayOOS({ data, onRefresh }) {
  if (!data) return null;
  const cov = data.coverage || {};
  const latest = data.latest_run || null;
  const rows = latest?.results || [];
  return (
    <section className="qd-card p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="qd-section-title flex items-center gap-2"><Zap size={14} /> Intraday 1m OOS — QG-O5…O10</div>
          <p className="mt-0.5 text-xs text-[var(--qd-text-2)]">
            Judges intraday BUYERS on real 1-minute option history. Separate from the EOD theta OOS above.
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={data.running}
          className="inline-flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-accent)]/40 bg-[var(--qd-surface-2)] px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--qd-accent)] hover:bg-[var(--qd-surface-3)] disabled:opacity-50"
        >
          <RefreshCw size={13} className={data.running ? "animate-spin" : ""} /> {data.running ? "Running…" : "Run"}
        </button>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Minute-data days" value={cov.total_days ?? 0} />
        <Stat label="Contract-days" value={cov.total_contract_days ?? 0} />
        <Stat label="Coverage window" value={cov.first_day ? `${cov.first_day} → ${cov.last_day}` : "none"} />
        <Stat label="Underlyings" value={(cov.underlyings || []).join(", ") || "-"} />
      </div>

      {!latest && (
        <div className="mt-4 font-mono text-xs text-[var(--qd-text-3)]">
          No intraday OOS run yet. Judge-first: until 1-minute index + option coverage matures, verdicts stay INSUFFICIENT_DATA.
        </div>
      )}
      {rows.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left font-mono text-xs">
            <thead className="text-[var(--qd-text-3)]">
              <tr><th className="py-1 pr-4">Strategy</th><th className="pr-4">Verdict</th><th className="pr-4">Trades</th><th className="pr-4">OOS exp.</th><th className="pr-4">Green mo.</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.strategy} className="border-t border-[var(--qd-border)]">
                  <td className="py-1 pr-4 text-[var(--qd-text)]">{r.strategy}</td>
                  <td className={`pr-4 ${VERDICT_CLS[r.verdict] || ""}`}>{r.verdict}</td>
                  <td className="pr-4">{r.trades ?? 0}</td>
                  <td className="pr-4">{num(r.oos?.expectancy)}</td>
                  <td className="pr-4">{r.green_month_pct != null ? pct(r.green_month_pct * 100) : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value }) {
  return (
    <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2">
      <div className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">{label}</div>
      <div className="mt-0.5 text-sm text-[var(--qd-text)]">{value}</div>
    </div>
  );
}

export default function Analytics() {
  const [tab, setTab] = useState("realized"); // realized | edgelab
  const [scorecard, setScorecard] = useState(null);
  const [oos, setOos] = useState(null);
  const [edgeLab, setEdgeLab] = useState(null);
  const [intradayOos, setIntradayOos] = useState(null);
  const [edgeMath, setEdgeMath] = useState(null);
  const [loading, setLoading] = useState(false);
  const [elLoading, setElLoading] = useState(false);
  const [error, setError] = useState("");
  const [sort, setSort] = useState({ col: "sharpe", dir: "desc" });
  const pollRef = useRef(null);

  const loadScorecard = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [r, o, e] = await Promise.all([
        api.get("/ops/risk-scorecard"),
        api.get("/ops/hermes-oos-validation").catch(() => ({ data: null })),
        api.get("/execution/snapshot").catch(() => ({ data: null })),
      ]);
      setScorecard(r.data);
      setOos(o.data);
      setEdgeMath(e.data?.edge_math || null);
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Failed to load scorecard");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEdgeLab = useCallback(async () => {
    setElLoading(true);
    try {
      const r = await api.get("/ops/edge-lab");
      setEdgeLab(r.data);
      return r.data;
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Failed to load Edge Lab");
      return null;
    } finally {
      setElLoading(false);
    }
  }, []);

  const loadIntradayOos = useCallback(async () => {
    try {
      const r = await api.get("/ops/intraday-oos");
      setIntradayOos(r.data);
    } catch (e) {
      // non-fatal — the EOD Edge Lab is the primary surface
    }
  }, []);

  const refreshIntradayOos = useCallback(async () => {
    try {
      await api.post("/ops/intraday-oos/refresh");
      setIntradayOos((d) => ({ ...(d || {}), running: true }));
      toast.info("Running intraday 1m OOS validation…");
      setTimeout(loadIntradayOos, 4000);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not start intraday OOS run");
    }
  }, [loadIntradayOos]);

  // Kick off a background rebuild, then poll the cached snapshot until it lands.
  const refreshEdgeLab = useCallback(async () => {
    try {
      await api.post("/ops/edge-lab/refresh");
      setEdgeLab((d) => ({ ...(d || {}), status: d?.status || "empty", building: true }));
      toast.info("Building Edge Lab snapshot — this can take a few minutes.");
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        const d = await loadEdgeLab();
        if (d && !d.building && d.status !== "building") {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      }, 5000);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not start Edge Lab build");
    }
  }, [loadEdgeLab]);

  useEffect(() => { loadScorecard(); }, [loadScorecard]);
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const onSort = (col) =>
    setSort((s) => (s.col === col ? { col, dir: s.dir === "desc" ? "asc" : "desc" } : { col, dir: "desc" }));

  const rows = useMemo(() => {
    const data = scorecard?.rows || [];
    const dir = sort.dir === "desc" ? -1 : 1;
    const val = (r) => {
      const v = r[sort.col];
      return typeof v === "number" ? v : (v ?? "").toString().toLowerCase();
    };
    return [...data].sort((a, b) => {
      const av = val(a), bv = val(b);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [scorecard, sort]);

  const byStructure = scorecard?.by_structure || {};
  const verdicts = scorecard?.verdicts?.counts || {};
  const bestByExpectancy = useMemo(() => (
    [...(scorecard?.rows || [])].sort((a, b) => (b.expectancy ?? -Infinity) - (a.expectancy ?? -Infinity))[0]
  ), [scorecard]);
  const worstByExpectancy = useMemo(() => (
    [...(scorecard?.rows || [])].sort((a, b) => (a.expectancy ?? Infinity) - (b.expectancy ?? Infinity))[0]
  ), [scorecard]);
  const oosCounts = oos?.counts || {};
  const oosTotal = Object.values(oosCounts).reduce((a, b) => a + Number(b || 0), 0);

  return (
    <div className="space-y-5" data-testid="analytics-page">
      {/* Header */}
      <div className="qd-card flex flex-col gap-3 p-5 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-3">
          <div className="rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2.5 text-[var(--qd-accent)]">
            <BarChart3 size={22} />
          </div>
          <div>
            <div className="qd-section-title">Edge &amp; Risk</div>
            <h1 className="font-head text-2xl font-extrabold text-[var(--qd-text)]">Strategy Analytics</h1>
            <p className="mt-0.5 text-xs text-[var(--qd-text-2)]">Realized risk-adjusted scoring from live trades, and the Edge Lab — an out-of-sample verdict on the whole book from 2 years of real option prices.</p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => (tab === "realized" ? loadScorecard() : refreshEdgeLab())}
          disabled={loading || elLoading}
          className="flex items-center gap-2 self-start rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)] disabled:opacity-50"
        >
          <RefreshCw size={14} className={loading || elLoading ? "animate-spin" : ""} /> {tab === "realized" ? "Refresh" : "Rebuild"}
        </button>
      </div>

      {/* Tabs */}
      <div className="flex gap-2 border-b border-[var(--qd-border)] pb-px">
        {[
          { id: "realized", label: "Realized (live trades)" },
          { id: "edgelab", label: "Edge Lab (OOS)" },
        ].map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => { setTab(t.id); if (t.id === "edgelab") { if (!edgeLab) loadEdgeLab(); if (!intradayOos) loadIntradayOos(); } }}
            className={`px-4 py-2.5 font-head text-xs font-semibold uppercase tracking-widest border-b-2 transition-colors ${
              tab === t.id ? "border-[var(--qd-accent)] text-[var(--qd-text)]" : "border-transparent text-[var(--qd-text-3)] hover:text-[var(--qd-text)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="qd-card border-l-2 border-l-[var(--qd-warn)] p-4">
          <div className="flex items-start gap-3">
            <AlertTriangle size={18} className="mt-0.5 text-[var(--qd-warn)]" />
            <div>
              <div className="qd-section-title text-[var(--qd-warn)]">Could not load analytics</div>
              <div className="mt-1 text-sm text-[var(--qd-text-2)]">{error}</div>
            </div>
          </div>
        </div>
      )}

      {tab === "realized" && (
        <>
          <section className="qd-card p-4">
            <div className="qd-section-title">EdgeMath — current contract decisions</div>
            <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {(edgeMath?.positions || []).slice(0, 6).map((row, i) => (
                <div key={`${row.selection_signature || row.target_symbol}-${i}`} className="rounded border border-[var(--qd-border)] p-3">
                  <div className="text-sm font-semibold text-[var(--qd-text)]">{row.target_symbol || row.selection_signature}</div>
                  <div className="mt-1 font-mono text-xs text-[var(--qd-text-2)]">
                    contract {Number(row.contract_edge_score || 0).toFixed(3)} · size {Number(row.edge_math?.edge_score || 0).toFixed(3)} · lots {row.edge_math?.final_lots ?? "-"}
                  </div>
                  <div className="mt-1 text-xs text-[var(--qd-text-3)]">
                    {row.edge_math?.reason || "Awaiting rolling expectancy"}
                  </div>
                </div>
              ))}
              {!(edgeMath?.positions || []).length && <div className="text-xs text-[var(--qd-text-3)]">No EdgeMath-sized spread has entered yet.</div>}
            </div>
          </section>
          <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <SummaryTile
              label="Verdicts"
              value={`${verdicts.KEEP || 0} KEEP / ${verdicts.WATCH || 0} WATCH / ${verdicts.KILL || 0} KILL`}
              sub={scorecard?.stats_window?.since ? `Clean since ${scorecard.stats_window.since.slice(0, 10)}` : "Lifetime window"}
              tone={(verdicts.KILL || 0) > 0 ? "bad" : (verdicts.KEEP || 0) > 0 ? "good" : "warn"}
              icon={ShieldCheck}
            />
            <SummaryTile
              label="Best Expectancy"
              value={bestByExpectancy ? money(bestByExpectancy.expectancy) : "-"}
              sub={bestByExpectancy?.name || "No realized strategies"}
              tone={(bestByExpectancy?.expectancy || 0) >= 0 ? "good" : "bad"}
              icon={TrendingUp}
            />
            <SummaryTile
              label="Worst Expectancy"
              value={worstByExpectancy ? money(worstByExpectancy.expectancy) : "-"}
              sub={worstByExpectancy?.name || "No realized strategies"}
              tone={(worstByExpectancy?.expectancy || 0) >= 0 ? "good" : "bad"}
              icon={TrendingDown}
            />
            <SummaryTile
              label="OOS Judge"
              value={oosTotal ? `${oosCounts.PASS || 0} pass / ${oosCounts.INSUFFICIENT_DATA || 0} waiting` : "No tests yet"}
              sub="Lessons cannot influence trading without OOS pass"
              tone={(oosCounts.PASS || 0) > 0 ? "good" : "warn"}
              icon={oosTotal ? ShieldCheck : CircleSlash}
            />
          </section>

          {/* Buyers vs sellers structure summary */}
          {Object.keys(byStructure).length > 0 && (
            <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {Object.entries(byStructure).map(([name, agg]) => (
                <StructureCard key={name} name={name} agg={agg} />
              ))}
            </section>
          )}

          {/* Per-strategy scorecard table */}
          <section className="qd-card overflow-hidden">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-5 py-4">
              <div>
                <div className="qd-section-title">// Per-strategy edge</div>
                <h2 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)]">Risk-Adjusted Scorecard</h2>
              </div>
              <span className="font-mono text-[11px] text-[var(--qd-text-3)]">{rows.length} strategies · base INR 100k</span>
            </div>

            {loading && !scorecard ? (
              <div className="p-10 text-center font-mono text-xs text-[var(--qd-text-3)]">Loading scorecard…</div>
            ) : rows.length === 0 ? (
              <div className="p-10 text-center">
                <Activity className="mx-auto mb-2 text-[var(--qd-text-3)]" size={20} />
                <div className="text-xs text-[var(--qd-text-2)]">No realized trades yet. Grades appear once strategies close trades.</div>
              </div>
            ) : (
              <div className="qd-table-wrap overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)]">
                      <SortHeader label="Strategy" col="name" sort={sort} onSort={onSort} />
                      <SortHeader label="Grade" col="grade" sort={sort} onSort={onSort} />
                      <SortHeader label="Sharpe" col="sharpe" sort={sort} onSort={onSort} />
                      <SortHeader label="Sortino" col="sortino" sort={sort} onSort={onSort} />
                      <SortHeader label="PF" col="profit_factor" sort={sort} onSort={onSort} />
                      <SortHeader label="Expectancy" col="expectancy" sort={sort} onSort={onSort} />
                      <SortHeader label="Max DD%" col="max_drawdown_pct" sort={sort} onSort={onSort} />
                      <SortHeader label="Trades" col="total_trades" sort={sort} onSort={onSort} />
                      <SortHeader label="Win%" col="win_rate" sort={sort} onSort={onSort} />
                      <SortHeader label="Net P&L" col="total_pnl" sort={sort} onSort={onSort} className="text-right" />
                      <SortHeader label="Verdict" col="verdict" sort={sort} onSort={onSort} />
                      <th className="px-3 py-2.5 text-right font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Equity</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {rows.map((r) => (
                      <tr key={r.strategy_id} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                        <td className="px-3 py-2.5">
                          <div className="font-semibold text-[var(--qd-text)]">{r.name}</div>
                          <div className="text-[11px] text-[var(--qd-text-3)]">{r.structure}{r.underlying ? ` · ${r.underlying}` : ""}</div>
                        </td>
                        <td className="px-3 py-2.5"><GradeChip grade={r.grade} /></td>
                        <td className={`px-3 py-2.5 ${(r.sharpe ?? 0) >= 1 ? "text-[var(--qd-profit)]" : (r.sharpe ?? 0) < 0 ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"}`}>{num(r.sharpe, 2)}</td>
                        <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{num(r.sortino, 2)}</td>
                        <td className={`px-3 py-2.5 ${(r.profit_factor ?? 0) >= 1.3 ? "text-[var(--qd-profit)]" : (r.profit_factor ?? 0) < 1 ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"}`}>{num(r.profit_factor, 2)}</td>
                        <td className={`px-3 py-2.5 ${signCls(r.expectancy)}`}>{money(r.expectancy)}</td>
                        <td className="px-3 py-2.5 text-[var(--qd-loss)]">{num(r.max_drawdown_pct, 1)}%</td>
                        <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{r.total_trades}</td>
                        <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{Math.round((r.win_rate ?? 0) * 100)}%</td>
                        <td className={`px-3 py-2.5 text-right font-semibold ${signCls(r.total_pnl)}`}>{money(r.total_pnl)}</td>
                        <td className="px-3 py-2.5">
                          <span
                            className={`inline-flex rounded-md border px-2 py-1 font-mono text-[10px] font-bold ${verdictTone(r.verdict) === "good" ? "border-[var(--qd-profit)]/35 text-[var(--qd-profit)]" : verdictTone(r.verdict) === "bad" ? "border-[var(--qd-loss)]/35 text-[var(--qd-loss)]" : "border-[var(--qd-warn)]/35 text-[var(--qd-warn)]"}`}
                            title={r.verdict_reason}
                          >
                            {r.verdict || "WATCH"}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 text-right"><div className="flex justify-end"><EquitySparkline curve={r.equity_curve} /></div></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      {tab === "edgelab" && (
        <div className="space-y-5">
          <EdgeLab data={edgeLab} loading={elLoading} onRefresh={refreshEdgeLab} />
          <IntradayOOS data={intradayOos} onRefresh={refreshIntradayOos} />
        </div>
      )}
    </div>
  );
}
