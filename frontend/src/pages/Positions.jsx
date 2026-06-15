import React, { useState } from "react";
import { Link } from "react-router-dom";
import { api, formatINR } from "../lib/api";
import { useExecutionState } from "../hooks/useExecutionState";
import { LogOut, Loader2, AlertTriangle } from "lucide-react";
import { toast } from "sonner";

const statusTone = (status) => {
  const s = (status || "").toUpperCase();
  if (s === "FILLED" || s === "OPEN" || s === "COMPLETE") return "text-[var(--qd-profit)]";
  if (s === "PENDING" || s === "PENDING_BROKER") return "text-[var(--qd-warn)]";
  if (s === "FAILED" || s === "REJECTED" || s === "ORPHAN" || s === "STALE") return "text-[var(--qd-loss)]";
  return "text-[var(--qd-text-2)]";
};

export default function Positions() {
  const { positions, summary, loading, error, refresh, paperMode, executionBroker } = useExecutionState({ pollMs: 15000 });
  const [exiting, setExiting] = useState(null);
  const [expanded, setExpanded] = useState(null);

  const exit = async (symbol) => {
    if (!window.confirm(`Square off your full ${symbol} position at market price?`)) return;
    setExiting(symbol);
    try {
      const r = await api.post(`/positions/${symbol}/exit`);
      toast.success(`Exit order placed for ${symbol} (${r.data.side} ${r.data.qty})`);
      await refresh();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Exit failed");
    } finally {
      setExiting(null);
    }
  };

  const total = summary.total_unrealized_pnl ?? positions.reduce((a, p) => a + (p.pnl || 0), 0);
  const active = positions.filter((p) => intQty(p.qty) !== 0);

  return (
    <div className="space-y-4" data-testid="positions-page">
      <div>
        <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// HOLDINGS</div>
        <h1 className="font-head text-3xl font-bold text-[var(--qd-text)] mt-1">Positions</h1>
        <p className="text-xs text-[var(--qd-text-2)] mt-1 font-mono">
          Synced from {paperMode ? "paper book" : executionBroker} · SL/TP from strategy ledger
        </p>
      </div>

      {error && (
        <div className="qd-card border-l-2 border-l-[var(--qd-warn)] p-3 flex gap-2 items-start">
          <AlertTriangle size={16} className="text-[var(--qd-warn)] mt-0.5" />
          <span className="text-sm text-[var(--qd-text-2)]">{error}</span>
        </div>
      )}

      <div className="qd-card p-4 flex items-center justify-between">
        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Total Unrealized PnL</span>
        <span className={`font-mono text-2xl font-bold ${total >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`} data-testid="total-pnl">
          {total >= 0 ? "+" : ""}₹{formatINR(total)}
        </span>
      </div>

      <div className="qd-card">
        {loading && active.length === 0 ? (
          <div className="p-10 text-center font-mono text-sm text-[var(--qd-text-2)]">Loading execution state…</div>
        ) : active.length === 0 ? (
          <div className="p-10 text-center space-y-3">
            <p className="font-mono text-sm text-[var(--qd-text-2)]">No open positions.</p>
            <Link
              to="/strategies"
              className="inline-flex items-center gap-2 rounded border border-[var(--qd-border)] px-4 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-accent)] transition-colors"
            >
              Go to Strategies
            </Link>
          </div>
        ) : (
          <div className="qd-table-wrap">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                  <th className="px-4 py-2">Strategy</th>
                  <th className="px-4 py-2">Symbol</th>
                  <th className="px-4 py-2">Qty</th>
                  <th className="px-4 py-2">Avg</th>
                  <th className="px-4 py-2">LTP</th>
                  <th className="px-4 py-2">Target</th>
                  <th className="px-4 py-2">Stop</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2 text-right">PnL</th>
                  <th className="px-4 py-2 text-right">Mode</th>
                  <th className="px-4 py-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {active.map((p) => {
                  const qty = intQty(p.qty);
                  const pnlTone = (p.pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]";
                  const longPos = qty > 0;
                  const isSpread = p.structure === "credit_spread";
                  const maxLossTotal = p.max_loss_total ?? (p.max_loss != null ? p.max_loss * Math.abs(qty) : null);
                  const stableKey = `${p.symbol}-${p.strategy_id || ""}`;
                  const legs = p.legs || [];
                  const isOpen = expanded === stableKey;
                  return (
                    <React.Fragment key={stableKey}>
                    <tr className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]" data-testid={`pos-${p.symbol}`}>
                      <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{p.strategy_name || p.strategy_id || "broker"}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-text)]">
                        {p.symbol}
                        {isSpread && (
                          <button
                            type="button"
                            onClick={() => setExpanded(isOpen ? null : stableKey)}
                            className="ml-2 text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm bg-[rgba(0,122,255,0.12)] text-[var(--qd-accent)] hover:bg-[rgba(0,122,255,0.2)]"
                            data-testid={`spread-toggle-${p.symbol}`}
                          >
                            spread {isOpen ? "▾" : "▸"}
                          </button>
                        )}
                        {isSpread && (
                          <div className="text-[10px] text-[var(--qd-text-3)] mt-0.5">
                            credit ₹{formatINR(p.net_credit)}{maxLossTotal != null ? ` · max loss ₹${formatINR(maxLossTotal)}` : ""}
                          </div>
                        )}
                        {!isSpread && p.greeks && p.greeks.delta != null && (
                          <div className="text-[10px] text-[var(--qd-text-3)] mt-0.5">
                            δ {Number(p.greeks.delta).toFixed(2)}
                            {p.target_delta != null ? ` (tgt ${Number(p.target_delta).toFixed(2)})` : ""}
                            {p.greeks.theta != null ? ` · θ ${Number(p.greeks.theta).toFixed(1)}` : ""}
                            {p.greeks.iv != null ? ` · IV ${Number(p.greeks.iv).toFixed(1)}` : ""}
                          </div>
                        )}
                      </td>
                      <td className={`px-4 py-2.5 ${isSpread ? "text-[var(--qd-text-2)]" : longPos ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{isSpread ? qty : `${longPos ? "+" : ""}${qty}`}</td>
                      <td className="px-4 py-2.5">{formatINR(p.avg_price)}</td>
                      <td className="px-4 py-2.5">{formatINR(p.ltp)}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-profit)]">{p.take_profit != null ? formatINR(p.take_profit) : "—"}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-loss)]">{p.stop_loss != null ? formatINR(p.stop_loss) : "—"}</td>
                      <td className={`px-4 py-2.5 text-[10px] uppercase ${statusTone(p.execution_status || p.ledger_status)}`}>{p.execution_status || p.ledger_status || "—"}</td>
                      <td className={`px-4 py-2.5 text-right ${pnlTone}`}>{(p.pnl || 0) >= 0 ? "+" : ""}₹{formatINR(p.pnl)}</td>
                      <td className="px-4 py-2.5 text-right">
                        <span className={`text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm ${
                          p.mode === "live" ? "bg-[rgba(255,59,48,0.12)] text-[var(--qd-loss)]" : "bg-[rgba(255,159,10,0.12)] text-[var(--qd-warn)]"
                        }`}>{p.mode || (paperMode ? "paper" : "live")}</span>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        {isSpread ? (
                          <span className="text-[9px] uppercase tracking-widest px-2 py-1 rounded-sm bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] ml-auto inline-block" title="Both legs auto-managed by TP 50% / SL 2x / EOD square-off">
                            Auto TP/SL
                          </span>
                        ) : (
                          <button
                            onClick={() => exit(p.symbol)}
                            disabled={exiting === p.symbol}
                            className={`text-[10px] uppercase tracking-wider px-3 py-1 rounded-sm disabled:opacity-50 flex items-center gap-1 ml-auto ${
                              longPos ? "bg-[var(--qd-loss)] text-white" : "bg-[var(--qd-profit)] text-black"
                            }`}
                            data-testid={`exit-${p.symbol}`}
                          >
                            {exiting === p.symbol ? <Loader2 size={12} className="animate-spin" /> : <LogOut size={12} />}
                            Exit
                          </button>
                        )}
                      </td>
                    </tr>
                    {isSpread && isOpen && (
                      <tr className="bg-[var(--qd-surface-2)]" data-testid={`spread-detail-${p.symbol}`}>
                        <td colSpan={11} className="px-4 py-3">
                          {(() => {
                            const short = legs.find((l) => l.role === "short");
                            const long = legs.find((l) => l.role === "long");
                            const netDelta = short?.delta != null && long?.delta != null ? -Number(short.delta) + Number(long.delta) : null;
                            const netTheta = short?.theta != null && long?.theta != null ? -Number(short.theta) + Number(long.theta) : null;
                            const maxProfitTotal = p.net_credit != null ? p.net_credit * Math.abs(qty) : null;
                            const pnl = Number(p.pnl || 0);
                            let markerPct = 50;
                            if (maxProfitTotal != null && maxLossTotal != null && maxProfitTotal + maxLossTotal > 0) {
                              markerPct = Math.max(0, Math.min(100, ((pnl + maxLossTotal) / (maxProfitTotal + maxLossTotal)) * 100));
                            }
                            const legRow = (leg) =>
                              leg ? (
                                <div className="flex items-center gap-3 text-[11px]">
                                  <span className={`w-10 font-semibold ${leg.side === "SELL" ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"}`}>{leg.side}</span>
                                  <span className="text-[var(--qd-text)] w-40">{leg.strike} {leg.option_type}</span>
                                  <span className="text-[var(--qd-text-2)]">@ ₹{formatINR(leg.premium)}</span>
                                  {leg.delta != null && <span className="text-[var(--qd-text-3)]">δ {Number(leg.delta).toFixed(2)}</span>}
                                </div>
                              ) : null;
                            return (
                              <div className="space-y-2 max-w-2xl">
                                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">// SPREAD LEGS{p.direction ? ` — ${p.direction}` : ""}</div>
                                {legRow(short)}
                                {legRow(long)}
                                <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-[11px] text-[var(--qd-text-2)]">
                                  <span>Net credit ₹{formatINR(p.net_credit)}</span>
                                  {maxLossTotal != null && <span>Max loss ₹{formatINR(maxLossTotal)}</span>}
                                  {netDelta != null && <span>Net δ {netDelta.toFixed(2)}</span>}
                                  {netTheta != null && <span>Net θ {netTheta.toFixed(1)}</span>}
                                  <span>Value {p.ltp != null ? `₹${formatINR(p.ltp)}` : "—"} · TP {p.take_profit != null ? `₹${formatINR(p.take_profit)}` : "—"} · SL {p.stop_loss != null ? `₹${formatINR(p.stop_loss)}` : "—"}</span>
                                </div>
                                {maxProfitTotal != null && maxLossTotal != null && (
                                  <div>
                                    <div className="relative h-2 overflow-hidden rounded-full bg-[var(--qd-surface-3)]">
                                      <div className="absolute inset-y-0 left-0 bg-[color-mix(in_srgb,var(--qd-loss)_25%,transparent)]" style={{ width: "50%" }} />
                                      <div className="absolute inset-y-0 right-0 bg-[color-mix(in_srgb,var(--qd-profit)_25%,transparent)]" style={{ width: "50%" }} />
                                      <div className="absolute top-[-2px] bottom-[-2px] w-0.5 bg-[var(--qd-text)]" style={{ left: `${markerPct}%` }} />
                                    </div>
                                    <div className="mt-0.5 flex justify-between text-[9px] text-[var(--qd-text-3)]">
                                      <span>-₹{formatINR(maxLossTotal)}</span>
                                      <span className={pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>P&amp;L ₹{formatINR(pnl)}</span>
                                      <span>+₹{formatINR(maxProfitTotal)}</span>
                                    </div>
                                  </div>
                                )}
                              </div>
                            );
                          })()}
                        </td>
                      </tr>
                    )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function intQty(value) {
  const n = parseInt(value, 10);
  return Number.isNaN(n) ? 0 : n;
}
