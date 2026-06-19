import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  BarChart3, RefreshCw, TrendingUp, TrendingDown, ArrowUpDown,
  Activity, AlertTriangle, FlaskConical, Layers,
} from "lucide-react";
import { api, formatINR } from "../lib/api";
import { toast } from "sonner";

const money = (v) => `INR ${formatINR(v ?? 0)}`;
const pct = (v) => `${Number(v ?? 0).toFixed(1)}%`;
const num = (v, d = 2) => (v == null || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(d));

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

// Real equity-curve sparkline driven by the strategy's actual cumulative P&L.
const EquitySparkline = ({ curve }) => {
  if (!curve || curve.length < 2) {
    return <span className="font-mono text-[10px] text-[var(--qd-text-3)]">—</span>;
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
        <span className="font-mono text-[10px] text-[var(--qd-text-3)]">{agg.strategies} strat · {agg.total_trades} tr</span>
      </div>
      <div className={`mt-2 font-head text-2xl font-bold ${pnlPos ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{money(agg.total_pnl)}</div>
      <div className="mt-1 grid grid-cols-2 gap-2 font-mono text-[11px] text-[var(--qd-text-2)]">
        <div>Win rate: <span className="text-[var(--qd-text)]">{pct((agg.win_rate ?? 0) * 100)}</span></div>
        <div>Expectancy: <span className={(agg.expectancy ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>{money(agg.expectancy)}</span></div>
      </div>
    </div>
  );
};

const SortHeader = ({ label, col, sort, onSort, className = "" }) => (
  <th className={`px-3 py-2.5 ${className}`}>
    <button
      type="button"
      onClick={() => onSort(col)}
      className={`inline-flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest hover:text-[var(--qd-text)] ${
        sort.col === col ? "text-[var(--qd-accent)]" : "text-[var(--qd-text-3)]"
      }`}
    >
      {label}
      <ArrowUpDown size={10} className={sort.col === col ? "opacity-100" : "opacity-40"} />
    </button>
  </th>
);

export default function Analytics() {
  const [tab, setTab] = useState("realized"); // realized | backtest
  const [scorecard, setScorecard] = useState(null);
  const [backtest, setBacktest] = useState(null);
  const [loading, setLoading] = useState(false);
  const [btLoading, setBtLoading] = useState(false);
  const [error, setError] = useState("");
  const [sort, setSort] = useState({ col: "sharpe", dir: "desc" });

  const loadScorecard = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await api.get("/ops/risk-scorecard");
      setScorecard(r.data);
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Failed to load scorecard");
    } finally {
      setLoading(false);
    }
  }, []);

  const runBacktest = useCallback(async () => {
    setBtLoading(true);
    try {
      const r = await api.post("/ops/options-backtest", {});
      setBacktest(r.data);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Backtest failed");
    } finally {
      setBtLoading(false);
    }
  }, []);

  useEffect(() => { loadScorecard(); }, [loadScorecard]);

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

  const btRows = useMemo(() => {
    const data = (backtest?.results || []).filter((r) => !r.error);
    return [...data].sort((a, b) => (b.sharpe ?? -999) - (a.sharpe ?? -999));
  }, [backtest]);

  const byStructure = scorecard?.by_structure || {};

  return (
    <div className="space-y-5" data-testid="analytics-page">
      {/* Header */}
      <div className="qd-card flex flex-col gap-3 p-5 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-3">
          <div className="rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2.5 text-[var(--qd-accent)]">
            <BarChart3 size={22} />
          </div>
          <div>
            <div className="qd-section-title">Edge & Risk</div>
            <h1 className="font-head text-2xl font-extrabold text-[var(--qd-text)]">Strategy Analytics</h1>
            <p className="mt-0.5 text-xs text-[var(--qd-text-2)]">Risk-adjusted scoring (Sharpe · Sortino · profit-factor · expectancy) from real trades — ranks by edge, not raw P&L.</p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => (tab === "realized" ? loadScorecard() : runBacktest())}
          disabled={loading || btLoading}
          className="flex items-center gap-2 self-start rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)] disabled:opacity-50"
        >
          <RefreshCw size={14} className={loading || btLoading ? "animate-spin" : ""} /> Refresh
        </button>
      </div>

      {/* Tabs */}
      <div className="flex gap-2 border-b border-[var(--qd-border)] pb-px">
        {[
          { id: "realized", label: "Realized (live trades)" },
          { id: "backtest", label: "Option-priced backtest" },
        ].map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => { setTab(t.id); if (t.id === "backtest" && !backtest) runBacktest(); }}
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
              <span className="font-mono text-[10px] text-[var(--qd-text-3)]">{rows.length} strategies · base INR 100k</span>
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
                      <th className="px-3 py-2.5 text-right font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">Equity</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {rows.map((r) => (
                      <tr key={r.strategy_id} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                        <td className="px-3 py-2.5">
                          <div className="font-semibold text-[var(--qd-text)]">{r.name}</div>
                          <div className="text-[10px] text-[var(--qd-text-3)]">{r.structure}{r.underlying ? ` · ${r.underlying}` : ""}</div>
                        </td>
                        <td className="px-3 py-2.5"><GradeChip grade={r.grade} /></td>
                        <td className={`px-3 py-2.5 ${(r.sharpe ?? 0) >= 1 ? "text-[var(--qd-profit)]" : (r.sharpe ?? 0) < 0 ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"}`}>{num(r.sharpe, 2)}</td>
                        <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{num(r.sortino, 2)}</td>
                        <td className={`px-3 py-2.5 ${(r.profit_factor ?? 0) >= 1.3 ? "text-[var(--qd-profit)]" : (r.profit_factor ?? 0) < 1 ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"}`}>{num(r.profit_factor, 2)}</td>
                        <td className={`px-3 py-2.5 ${(r.expectancy ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{money(r.expectancy)}</td>
                        <td className="px-3 py-2.5 text-[var(--qd-loss)]">{num(r.max_drawdown_pct, 1)}%</td>
                        <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{r.total_trades}</td>
                        <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{Math.round((r.win_rate ?? 0) * 100)}%</td>
                        <td className={`px-3 py-2.5 text-right font-semibold ${(r.total_pnl ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{money(r.total_pnl)}</td>
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

      {tab === "backtest" && (
        <section className="qd-card overflow-hidden">
          <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-5 py-4">
            <div>
              <div className="qd-section-title flex items-center gap-1.5"><FlaskConical size={13} /> // Option-priced, real chains</div>
              <h2 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)]">Backtest (real CE/PE premiums)</h2>
            </div>
            <button
              type="button"
              onClick={runBacktest}
              disabled={btLoading}
              className="flex items-center gap-2 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] hover:text-[var(--qd-text)] disabled:opacity-50"
            >
              <RefreshCw size={14} className={btLoading ? "animate-spin" : ""} /> Run
            </button>
          </div>

          <div className="border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-5 py-2 font-mono text-[10px] text-[var(--qd-text-3)]">
            Signals come from real 5-min underlying OHLC; legs priced from real chain snapshots. Sample is bounded by collected chain history.
          </div>

          {btLoading && !backtest ? (
            <div className="p-10 text-center font-mono text-xs text-[var(--qd-text-3)]">Running backtest…</div>
          ) : btRows.length === 0 ? (
            <div className="p-10 text-center text-xs text-[var(--qd-text-2)]">No backtest results. Click Run to score option strategies on real chains.</div>
          ) : (
            <div className="qd-table-wrap overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)] font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">
                    <th className="px-3 py-2.5">Strategy</th>
                    <th className="px-3 py-2.5">Grade</th>
                    <th className="px-3 py-2.5">Sharpe</th>
                    <th className="px-3 py-2.5">PF</th>
                    <th className="px-3 py-2.5">Signals</th>
                    <th className="px-3 py-2.5">Trades</th>
                    <th className="px-3 py-2.5">Win%</th>
                    <th className="px-3 py-2.5">Source</th>
                    <th className="px-3 py-2.5 text-right">Net P&L</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {btRows.map((r) => (
                    <tr key={r.strategy_id} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                      <td className="px-3 py-2.5">
                        <div className="font-semibold text-[var(--qd-text)]">{r.name}</div>
                        <div className="text-[10px] text-[var(--qd-text-3)]">{r.structure}{r.underlying ? ` · ${r.underlying}` : ""}</div>
                      </td>
                      <td className="px-3 py-2.5"><GradeChip grade={r.grade} /></td>
                      <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{num(r.sharpe, 2)}</td>
                      <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{num(r.profit_factor, 2)}</td>
                      <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{r.signals_in_window ?? r.signals ?? "-"}</td>
                      <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{r.total_trades}</td>
                      <td className="px-3 py-2.5 text-[var(--qd-text-2)]">{Math.round((r.win_rate ?? 0) * 100)}%</td>
                      <td className="px-3 py-2.5">
                        <span className={`text-[10px] ${r.candle_source === "real_ohlc" ? "text-[var(--qd-profit)]" : "text-[var(--qd-text-3)]"}`}>
                          {r.candle_source === "real_ohlc" ? "real OHLC" : (r.candle_source || "-")}
                        </span>
                      </td>
                      <td className={`px-3 py-2.5 text-right font-semibold ${(r.total_pnl ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{money(r.total_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* surface skipped strategies (e.g., no chain data) */}
          {(backtest?.results || []).some((r) => r.error) && (
            <div className="border-t border-[var(--qd-border)] px-5 py-3 font-mono text-[10px] text-[var(--qd-text-3)]">
              Skipped: {(backtest.results.filter((r) => r.error)).map((r) => `${r.name || r.underlying} (${r.error})`).join(" · ")}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
