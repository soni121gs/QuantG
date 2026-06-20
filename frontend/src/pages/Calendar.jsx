import React, { useCallback, useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, X, TrendingUp, TrendingDown, Minus, Calendar as CalIcon } from "lucide-react";
import { api, formatINR } from "../lib/api";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";

const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

function todayIST() {
  const d = new Date(Date.now() + IST_OFFSET_MS);
  return d.toISOString().slice(0, 10);
}

function monthKey(year, month) {
  return `${year}-${String(month + 1).padStart(2, "0")}`;
}

function daysInMonth(year, month) {
  return new Date(year, month + 1, 0).getDate();
}

function firstWeekdayOfMonth(year, month) {
  return new Date(year, month, 1).getDay(); // 0=Sun
}

function isWeekend(year, month, day) {
  const dow = new Date(year, month, day).getDay();
  return dow === 0 || dow === 6;
}

function fmt(val) {
  return `INR ${formatINR(val ?? 0)}`;
}

function pnlTone(val) {
  if (val > 0) return "profit";
  if (val < 0) return "loss";
  return "neutral";
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const DAY_HEADERS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export default function Calendar() {
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth());
  const [reports, setReports] = useState({});  // date string → report doc
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(null); // date string
  const today = todayIST();

  const fetchMonth = useCallback(async (y, m) => {
    setLoading(true);
    try {
      const res = await api.get(`/reports/daily?month=${monthKey(y, m)}`);
      const map = {};
      for (const doc of res.data.reports || []) {
        map[doc.date] = doc;
      }
      setReports((prev) => ({ ...prev, ...map }));
    } catch {
      // silently ignore — cells just show as empty
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchMonth(year, month);
  }, [year, month, fetchMonth]);

  function prevMonth() {
    if (month === 0) { setYear(y => y - 1); setMonth(11); }
    else setMonth(m => m - 1);
    setSelected(null);
  }

  function nextMonth() {
    if (month === 11) { setYear(y => y + 1); setMonth(0); }
    else setMonth(m => m + 1);
    setSelected(null);
  }

  const totalDays = daysInMonth(year, month);
  const startDow = firstWeekdayOfMonth(year, month);
  // Build grid cells: nulls for leading blanks, then day numbers
  const cells = [];
  for (let i = 0; i < startDow; i++) cells.push(null);
  for (let d = 1; d <= totalDays; d++) cells.push(d);

  const selectedReport = selected ? reports[selected] : null;

  return (
    <div className="qd-page-root">
      <PageHeader
        eyebrow="Reports"
        title="Trading Calendar"
        subtitle="Daily P&L, trade count, and report details for quick review."
        badge={<StatusBadge tone={loading ? "warning" : "neutral"}>{loading ? "Loading" : `${MONTH_NAMES[month]} ${year}`}</StatusBadge>}
        actions={<CalIcon size={20} className="text-[var(--qd-accent)]" />}
        className="mb-4"
      />

      <div className="flex gap-4 flex-col lg:flex-row">
        {/* Calendar grid */}
        <div className="flex-1 qd-card p-4 min-w-0">
          {/* Month navigation */}
          <div className="flex items-center justify-between mb-4">
            <button
              onClick={prevMonth}
              className="p-1.5 rounded hover:bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)] transition-colors"
              aria-label="Previous month"
            >
              <ChevronLeft size={16} />
            </button>
            <span className="font-head font-bold text-[var(--qd-text)] text-sm tracking-wide">
              {MONTH_NAMES[month]} {year}
            </span>
            <button
              onClick={nextMonth}
              className="p-1.5 rounded hover:bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)] transition-colors"
              aria-label="Next month"
            >
              <ChevronRight size={16} />
            </button>
          </div>

          {/* Day-of-week headers */}
          <div className="grid grid-cols-7 gap-1 mb-1">
            {DAY_HEADERS.map((d) => (
              <div
                key={d}
                className="text-center text-[11px] font-mono uppercase tracking-widest text-[var(--qd-text-3)] py-1"
              >
                {d}
              </div>
            ))}
          </div>

          {/* Day cells */}
          <div className="grid grid-cols-7 gap-1">
            {cells.map((day, idx) => {
              if (!day) {
                return <div key={`blank-${idx}`} />;
              }
              const dateStr = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
              const report = reports[dateStr];
              const isFuture = dateStr > today;
              const isToday = dateStr === today;
              const weekend = isWeekend(year, month, day);
              const isSelected = selected === dateStr;
              const pnl = report?.total_realized_pnl ?? null;
              const hasTrades = report && report.trades_taken > 0;
              const tone = pnl !== null && hasTrades ? pnlTone(pnl) : "none";

              let bgClass = "bg-[var(--qd-surface)]";
              if (!isFuture && !weekend) {
                if (tone === "profit") bgClass = "bg-[rgba(52,199,89,0.12)] hover:bg-[rgba(52,199,89,0.2)]";
                else if (tone === "loss") bgClass = "bg-[rgba(255,59,48,0.12)] hover:bg-[rgba(255,59,48,0.2)]";
                else bgClass = "bg-[var(--qd-surface)] hover:bg-[var(--qd-surface-2)]";
              } else if (weekend) {
                bgClass = "bg-[var(--qd-bg)]";
              }

              const borderClass = isSelected
                ? "border border-[var(--qd-accent)]"
                : isToday
                ? "border border-[var(--qd-accent)]/40"
                : "border border-transparent";

              const clickable = !isFuture && !weekend;

              return (
                <button
                  key={dateStr}
                  onClick={() => clickable && setSelected(isSelected ? null : dateStr)}
                  disabled={!clickable}
                  className={`relative flex flex-col items-center justify-start rounded-[var(--qd-radius-sm)] p-1.5 min-h-[52px] transition-colors ${bgClass} ${borderClass} ${clickable ? "cursor-pointer" : "cursor-default"}`}
                  aria-label={dateStr}
                >
                  <span className={`text-[11px] font-mono font-semibold mb-0.5 ${
                    isToday ? "text-[var(--qd-accent)]" : weekend ? "text-[var(--qd-text-3)]" : "text-[var(--qd-text-2)]"
                  }`}>
                    {day}
                  </span>
                  {hasTrades && pnl !== null && (
                    <span className={`text-[11px] font-mono font-bold leading-none ${
                      pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"
                    }`}>
                      {pnl >= 0 ? "+" : ""}
                      {Math.abs(pnl) >= 1000
                        ? `${(pnl / 1000).toFixed(1)}k`
                        : pnl.toFixed(0)}
                    </span>
                  )}
                  {report && !hasTrades && !isFuture && !weekend && (
                    <Minus size={8} className="text-[var(--qd-text-3)] mt-0.5" />
                  )}
                </button>
              );
            })}
          </div>

          {/* Legend */}
          <div className="flex items-center gap-4 mt-4 pt-3 border-t border-[var(--qd-border)]">
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-sm bg-[rgba(52,199,89,0.25)]" />
              <span className="text-[11px] font-mono text-[var(--qd-text-3)]">Profit day</span>
            </div>
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-sm bg-[rgba(255,59,48,0.25)]" />
              <span className="text-[11px] font-mono text-[var(--qd-text-3)]">Loss day</span>
            </div>
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-sm bg-[var(--qd-surface)] border border-[var(--qd-border)]" />
              <span className="text-[11px] font-mono text-[var(--qd-text-3)]">No trades</span>
            </div>
          </div>
        </div>

        {/* Side panel */}
        {selected && (
          <div className="lg:w-80 qd-card p-4 flex flex-col gap-4 animate-in slide-in-from-right-4 duration-200">
            <div className="flex items-start justify-between">
              <div>
                <div className="text-[11px] font-mono text-[var(--qd-text-3)] uppercase tracking-widest mb-0.5">
                  {selected}
                </div>
                <div className="font-head font-bold text-[var(--qd-text)] text-sm">Day Summary</div>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="p-1 rounded hover:bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
              >
                <X size={14} />
              </button>
            </div>

            {selectedReport ? (
              <>
                {/* P&L summary */}
                <div className="grid grid-cols-2 gap-2">
                  <PanelStat
                    label="Realized P&L"
                    value={fmt(selectedReport.total_realized_pnl)}
                    tone={pnlTone(selectedReport.total_realized_pnl)}
                  />
                  <PanelStat
                    label="Unrealized P&L"
                    value={fmt(selectedReport.total_unrealized_pnl)}
                    tone={pnlTone(selectedReport.total_unrealized_pnl)}
                  />
                  <PanelStat label="Trades" value={selectedReport.trades_taken} />
                  <PanelStat label="Signals" value={selectedReport.signals_fired} />
                </div>

                {/* Market regime */}
                {selectedReport.market_regime && (
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-mono text-[var(--qd-text-3)] uppercase">Regime</span>
                    <span className="text-[11px] font-mono font-bold text-[var(--qd-accent)] px-2 py-0.5 rounded bg-[var(--qd-accent)]/10 border border-[var(--qd-accent)]/20">
                      {selectedReport.market_regime}
                    </span>
                  </div>
                )}

                {/* Best / worst */}
                {(selectedReport.best_strategy || selectedReport.worst_strategy) && (
                  <div className="space-y-1.5">
                    {selectedReport.best_strategy && (
                      <div className="flex items-center justify-between text-xs">
                        <span className="flex items-center gap-1 text-[var(--qd-text-2)]">
                          <TrendingUp size={11} className="text-[var(--qd-profit)]" /> Best
                        </span>
                        <span className="font-mono text-[var(--qd-profit)] text-[11px]">
                          {selectedReport.best_strategy.name} · +{fmt(selectedReport.best_strategy.pnl)}
                        </span>
                      </div>
                    )}
                    {selectedReport.worst_strategy && (
                      <div className="flex items-center justify-between text-xs">
                        <span className="flex items-center gap-1 text-[var(--qd-text-2)]">
                          <TrendingDown size={11} className="text-[var(--qd-loss)]" /> Worst
                        </span>
                        <span className="font-mono text-[var(--qd-loss)] text-[11px]">
                          {selectedReport.worst_strategy.name} · {fmt(selectedReport.worst_strategy.pnl)}
                        </span>
                      </div>
                    )}
                  </div>
                )}

                {/* Per-strategy breakdown */}
                {selectedReport.strategies?.length > 0 && (
                  <div>
                    <div className="text-[11px] font-mono uppercase tracking-widest text-[var(--qd-text-3)] mb-2">
                      Strategy Breakdown
                    </div>
                    <div className="space-y-1.5 max-h-64 overflow-y-auto">
                      {selectedReport.strategies.map((s) => (
                        <div
                          key={s.strategy_id}
                          className="flex items-center justify-between px-2 py-1.5 rounded bg-[var(--qd-surface)] border border-[var(--qd-border)]"
                        >
                          <span className="text-[11px] font-mono text-[var(--qd-text-2)] truncate max-w-[120px]" title={s.name}>
                            {s.name}
                          </span>
                          <div className="flex items-center gap-3 text-[11px] font-mono">
                            <span className={s.realized_pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>
                              {s.realized_pnl >= 0 ? "+" : ""}{fmt(s.realized_pnl)}
                            </span>
                            <span className="text-[var(--qd-text-3)]">{s.trade_count}T</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {selectedReport.trades_taken === 0 && (
                  <p className="text-[11px] font-mono text-[var(--qd-text-3)] text-center py-4">
                    No trades on this day.
                  </p>
                )}
              </>
            ) : (
              <p className="text-[11px] font-mono text-[var(--qd-text-3)] text-center py-6">
                No report generated for this day yet.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function PanelStat({ label, value, tone }) {
  const toneClass =
    tone === "profit"
      ? "text-[var(--qd-profit)]"
      : tone === "loss"
      ? "text-[var(--qd-loss)]"
      : "text-[var(--qd-text)]";

  return (
    <div className="flex flex-col gap-0.5 p-2 rounded bg-[var(--qd-surface)] border border-[var(--qd-border)]">
      <span className="text-[11px] font-mono uppercase tracking-widest text-[var(--qd-text-3)]">{label}</span>
      <span className={`text-xs font-mono font-bold ${toneClass}`}>{value}</span>
    </div>
  );
}
