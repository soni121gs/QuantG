import React, { useEffect, useRef, useState } from "react";
import { api, formatINR, formatApiErrorDetail } from "../lib/api";
import { useExecutionState } from "../hooks/useExecutionState";
import { AlertTriangle, Loader2, LogOut, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { Button } from "../components/ui/button";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";
import { reasonLabel } from "../lib/reasonLabels";

const OPEN_STATUSES = ["NEW", "PLACED", "OPEN", "PARTIAL_FILL", "EXIT_PENDING", "PENDING", "PENDING_BROKER", "TRIGGER PENDING", "MODIFY PENDING", "VALIDATION PENDING"];
const FILLED_STATUSES = ["FILLED", "PAPER_FILLED", "CLOSED", "COMPLETE"];
const REJECTED_STATUSES = ["CANCELLED", "REJECTED", "FAILED", "BROKER_NOT_FOUND", "STALE"];

const statusTone = (status) => {
  const s = (status || "").toUpperCase();
  if (s === "FILLED" || s === "OPEN" || s === "COMPLETE") return "text-[var(--qd-profit)]";
  if (s === "PENDING" || s === "PENDING_BROKER") return "text-[var(--qd-warn)]";
  if (s === "FAILED" || s === "REJECTED" || s === "ORPHAN" || s === "STALE") return "text-[var(--qd-loss)]";
  return "text-[var(--qd-text-2)]";
};

function intQty(value) {
  const n = parseInt(value, 10);
  return Number.isNaN(n) ? 0 : n;
}

function orderPnl(order) {
  const value = order.pnl ?? order.realized_pnl ?? order.net_pnl ?? order.profit_loss ?? order.unrealized_pnl ?? order.p_and_l;
  return value === null || value === undefined || value === "" ? null : Number(value);
}

function FlashingValue({ value, children }) {
  const prevValueRef = useRef(value);
  const [flashClass, setFlashClass] = useState("");

  useEffect(() => {
    if (prevValueRef.current !== undefined && prevValueRef.current !== value) {
      const isUp = Number(value) > Number(prevValueRef.current);
      setFlashClass(isUp ? "qd-blink-up" : "qd-blink-dn");
      prevValueRef.current = value;
      const t = setTimeout(() => setFlashClass(""), 700);
      return () => clearTimeout(t);
    }
  }, [value]);

  return <span className={`inline-block px-1 rounded transition-colors ${flashClass}`}>{children}</span>;
}

export default function Orders() {
  const { orders, positions, skippedSignals, summary, loading, error, refresh, paperMode, executionBroker } = useExecutionState({ pollMs: 15000 });
  const [watch, setWatch] = useState([]);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("ALL");
  const [exiting, setExiting] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [form, setForm] = useState({
    symbol: "RELIANCE",
    exchange: "NSE",
    side: "BUY",
    qty: 1,
    order_type: "MARKET",
    price: "",
    product: "MIS",
    stop_loss: "",
    take_profit: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    api.get("/market/watchlist").then((r) => setWatch(r.data)).catch(() => {});
  }, []);

  const visibleOrders = orders.filter((o) => o.visibility !== "hidden");
  const filteredOrders = visibleOrders.filter((o) => {
    const status = o.execution_status || o.status;
    if (filter === "ALL") return true;
    if (filter === "OPEN") return OPEN_STATUSES.includes(status);
    if (filter === "COMPLETE") return FILLED_STATUSES.includes(status);
    if (filter === "CANCELLED") return REJECTED_STATUSES.includes(status);
    if (filter === "FAILED") return ["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE"].includes(status);
    if (filter === "SKIPPED") return false;
    return true;
  });
  const filtered = filter === "SKIPPED" ? skippedSignals : filteredOrders;
  const activePositions = positions.filter((p) => intQty(p.qty) !== 0);
  const totalUnrealized = summary.total_unrealized_pnl ?? positions.reduce((a, p) => a + (p.pnl || 0), 0);

  const filterCount = (f) => {
    if (f === "SKIPPED") return skippedSignals.reduce((sum, row) => sum + Number(row.count || 1), 0);
    return visibleOrders.filter((o) => {
      const status = o.execution_status || o.status;
      if (f === "OPEN") return OPEN_STATUSES.includes(status);
      if (f === "COMPLETE") return FILLED_STATUSES.includes(status);
      if (f === "FAILED") return ["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE"].includes(status);
      return REJECTED_STATUSES.includes(status);
    }).length;
  };

  const submit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await api.post("/orders", {
        ...form,
        qty: +form.qty,
        price: form.price ? +form.price : null,
        stop_loss: form.stop_loss ? +form.stop_loss : null,
        take_profit: form.take_profit ? +form.take_profit : null,
      });
      setOpen(false);
      await refresh();
      toast.success("Order submitted");
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Order failed");
    } finally {
      setSubmitting(false);
    }
  };

  const syncBroker = async () => {
    setSyncing(true);
    try {
      await api.post("/ops/orders/sync");
      await refresh();
      toast.success("Broker sync complete");
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Broker sync failed");
    } finally {
      setSyncing(false);
    }
  };

  const exit = async (symbol) => {
    if (!window.confirm(`Square off your full ${symbol} position at market price?`)) return;
    setExiting(symbol);
    try {
      const r = await api.post(`/positions/${symbol}/exit`);
      toast.success(`Exit order placed for ${symbol} (${r.data.side} ${r.data.qty})`);
      await refresh();
    } catch (e) {
      toast.error(formatApiErrorDetail(e.response?.data?.detail) || "Exit failed");
    } finally {
      setExiting(null);
    }
  };

  return (
    <div className="space-y-4" data-testid="orders-page">
      <PageHeader
        eyebrow="Execution"
        title="Execution"
        subtitle="Open positions, order ledger, skipped signals, broker state, and app-managed protection levels."
        badge={<StatusBadge tone="neutral">Broker: {executionBroker}</StatusBadge>}
        actions={
          <Button onClick={syncBroker} disabled={syncing} variant="outline" size="sm" data-testid="sync-broker-btn">
            <ShieldCheck size={14} /> {syncing ? "Syncing" : "Sync with Broker"}
          </Button>
        }
      />

      {error && (
        <div className="qd-card border-l-2 border-l-[var(--qd-warn)] p-3 flex gap-2">
          <AlertTriangle size={16} className="text-[var(--qd-warn)]" />
          <span className="text-sm text-[var(--qd-text-2)]">{error}</span>
        </div>
      )}

      <section className="space-y-3" data-testid="positions-section">
        <div className="qd-card p-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Positions</div>
            <div className="mt-1 text-xs text-[var(--qd-text-2)] font-mono">
              Synced from {paperMode ? "paper book" : executionBroker} - SL/TP from strategy ledger
            </div>
          </div>
          <div className="text-left sm:text-right">
            <span className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Total Unrealized P&L</span>
            <div className={`font-mono text-2xl font-bold ${totalUnrealized >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`} data-testid="total-pnl">
              {totalUnrealized >= 0 ? "+" : ""}₹{formatINR(totalUnrealized)}
            </div>
          </div>
        </div>

        <div className="qd-card">
          {loading && activePositions.length === 0 ? (
            <div className="p-10 text-center font-mono text-sm text-[var(--qd-text-2)]">Loading execution state...</div>
          ) : activePositions.length === 0 ? (
            <div className="p-8 text-center font-mono text-sm text-[var(--qd-text-2)]">No open positions.</div>
          ) : (
            <div className="qd-table-wrap">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
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
                  {activePositions.map((p) => {
                    const qty = intQty(p.qty);
                    const pnlTone = (p.pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]";
                    const longPos = qty > 0;
                    const isSpread = p.structure === "credit_spread" || p.structure === "debit_spread";
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
                                className="ml-2 text-[11px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm bg-[rgba(0,122,255,0.12)] text-[var(--qd-accent)] hover:bg-[rgba(0,122,255,0.2)]"
                                data-testid={`spread-toggle-${p.symbol}`}
                              >
                                spread {isOpen ? "v" : ">"}
                              </button>
                            )}
                            {isSpread && (
                              <div className="text-[11px] text-[var(--qd-text-3)] mt-0.5">
                                {p.structure === "debit_spread" ? `debit ₹${formatINR(p.net_debit)}` : `credit ₹${formatINR(p.net_credit)}`}{maxLossTotal != null ? ` - max loss ₹${formatINR(maxLossTotal)}` : ""}
                              </div>
                            )}
                            {!isSpread && p.greeks && p.greeks.delta != null && (
                              <div className="text-[11px] text-[var(--qd-text-3)] mt-0.5">
                                delta {Number(p.greeks.delta).toFixed(2)}
                                {p.target_delta != null ? ` (tgt ${Number(p.target_delta).toFixed(2)})` : ""}
                                {p.greeks.theta != null ? ` - theta ${Number(p.greeks.theta).toFixed(1)}` : ""}
                                {p.greeks.iv != null ? ` - IV ${Number(p.greeks.iv).toFixed(1)}` : ""}
                              </div>
                            )}
                          </td>
                          <td className={`px-4 py-2.5 ${isSpread ? "text-[var(--qd-text-2)]" : longPos ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{isSpread ? qty : `${longPos ? "+" : ""}${qty}`}</td>
                          <td className="px-4 py-2.5">{formatINR(p.avg_price)}</td>
                          <td className="px-4 py-2.5"><FlashingValue value={p.ltp}>{formatINR(p.ltp)}</FlashingValue></td>
                          <td className="px-4 py-2.5 text-[var(--qd-profit)]">{p.take_profit != null ? formatINR(p.take_profit) : "-"}</td>
                          <td className="px-4 py-2.5 text-[var(--qd-loss)]">{p.stop_loss != null ? formatINR(p.stop_loss) : "-"}</td>
                          <td className={`px-4 py-2.5 text-[11px] uppercase ${statusTone(p.execution_status || p.ledger_status)}`}>{p.execution_status || p.ledger_status || "-"}</td>
                          <td className={`px-4 py-2.5 text-right ${pnlTone}`}><FlashingValue value={p.pnl}>{(p.pnl || 0) >= 0 ? "+" : ""}₹{formatINR(p.pnl)}</FlashingValue></td>
                          <td className="px-4 py-2.5 text-right">
                            <span className={`text-[11px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm ${p.mode === "live" ? "bg-[rgba(255,59,48,0.12)] text-[var(--qd-loss)]" : "bg-[rgba(255,159,10,0.12)] text-[var(--qd-warn)]"}`}>
                              {p.mode || (paperMode ? "paper" : "live")}
                            </span>
                          </td>
                          <td className="px-4 py-2.5 text-right">
                            {isSpread ? (
                              <span className="text-[11px] uppercase tracking-widest px-2 py-1 rounded-sm bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] ml-auto inline-block" title="Both legs auto-managed by TP / SL / EOD square-off">
                                Auto TP/SL
                              </span>
                            ) : (
                              <button
                                onClick={() => exit(p.symbol)}
                                disabled={exiting === p.symbol}
                                className={`text-[11px] uppercase tracking-wider px-3 py-1 rounded-sm disabled:opacity-50 flex items-center gap-1 ml-auto ${longPos ? "bg-[var(--qd-loss)] text-white" : "bg-[var(--qd-profit)] text-black"}`}
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
                              <div className="space-y-2 max-w-3xl">
                                <div className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Spread Legs{p.direction ? ` - ${p.direction}` : ""}</div>
                                {legs.map((leg, idx) => (
                                  <div key={`${leg.instrument_key || leg.symbol || idx}`} className="flex flex-wrap items-center gap-3 text-[11px]">
                                    <span className={`w-10 font-semibold ${leg.side === "SELL" ? "text-[var(--qd-loss)]" : "text-[var(--qd-profit)]"}`}>{leg.side}</span>
                                    <span className="text-[var(--qd-text)] min-w-40">{leg.strike} {leg.option_type}</span>
                                    <span className="text-[var(--qd-text-2)]">@ ₹{formatINR(leg.premium)}</span>
                                    {leg.delta != null && <span className="text-[var(--qd-text-3)]">delta {Number(leg.delta).toFixed(2)}</span>}
                                  </div>
                                ))}
                                <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-[11px] text-[var(--qd-text-2)]">
                                  <span>{p.structure === "debit_spread" ? `Net debit ₹${formatINR(p.net_debit)}` : `Net credit ₹${formatINR(p.net_credit)}`}</span>
                                  {maxLossTotal != null && <span>Max loss ₹{formatINR(maxLossTotal)}</span>}
                                  <span>Value {p.ltp != null ? `₹${formatINR(p.ltp)}` : "-"} - TP {p.take_profit != null ? `₹${formatINR(p.take_profit)}` : "-"} - SL {p.stop_loss != null ? `₹${formatINR(p.stop_loss)}` : "-"}</span>
                                  <span className={(p.pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>P&L ₹{formatINR(p.pnl)}</span>
                                </div>
                              </div>
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
      </section>

      <div className="qd-filter-bar flex items-center justify-between flex-wrap gap-2">
        <div className="flex gap-1 flex-wrap" data-testid="order-filter">
          {["ALL", "OPEN", "COMPLETE", "CANCELLED", "FAILED", "SKIPPED"].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 text-[11px] font-mono uppercase tracking-widest rounded-sm ${
                filter === f ? "qd-force-white bg-[var(--qd-accent)]" : "border border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
              }`}
              data-testid={`filter-${f.toLowerCase()}`}
            >
              {f === "SKIPPED" ? "Skipped Signals" : f} {f !== "ALL" && `- ${filterCount(f)}`}
            </button>
          ))}
        </div>
        <span className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">
          showing {filtered.length} of {filter === "SKIPPED" ? skippedSignals.length : visibleOrders.length}
        </span>
      </div>

      <section className="qd-card" data-testid="orders-section">
        {filtered.length === 0 ? (
          <div className="p-10 text-center space-y-3">
            <p className="font-mono text-sm text-[var(--qd-text-2)]">No orders match this filter.</p>
            {filter !== "ALL" && (
              <button
                onClick={() => setFilter("ALL")}
                className="inline-flex items-center gap-2 rounded border border-[var(--qd-border)] px-4 py-2 font-mono text-xs uppercase tracking-wider text-[var(--qd-text)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-accent)] transition-colors"
              >
                Clear filter
              </button>
            )}
          </div>
        ) : (
          <div className="qd-table-wrap">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                  <th className="px-4 py-2">Time</th>
                  <th className="px-4 py-2">Strategy</th>
                  <th className="px-4 py-2">Symbol</th>
                  <th className="px-4 py-2">Seg</th>
                  <th className="px-4 py-2">Side</th>
                  <th className="px-4 py-2">Qty</th>
                  <th className="px-4 py-2">Price</th>
                  <th className="px-4 py-2">SL</th>
                  <th className="px-4 py-2">TP</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2 text-right">PnL</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {filter === "SKIPPED" && filtered.map((o) => {
                  const rl = reasonLabel(o.reason_code || o.skip_reason || "preflight");
                  return (
                    <tr key={o.id || o.dedupe_key} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]" data-testid={`skipped-${o.id || o.dedupe_key}`}>
                      <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{o.last_seen_at ? new Date(o.last_seen_at).toLocaleTimeString("en-IN", { hour12: false }) : "-"}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{o.strategy_name || o.strategy_id || "strategy"}</td>
                      <td className="px-4 py-2.5 text-white" title={rl.hint || rl.raw}>{o.symbol}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{o.segment || o.exchange || "-"}</td>
                      <td className={`px-4 py-2.5 font-semibold ${o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{o.side}</td>
                      <td className="px-4 py-2.5">{o.count || 1}</td>
                      <td className="px-4 py-2.5">{formatINR(o.price || 0)}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-loss)]">-</td>
                      <td className="px-4 py-2.5 text-[var(--qd-profit)]">-</td>
                      <td className="px-4 py-2.5 text-[var(--qd-warn)]" title={rl.label}>SKIPPED</td>
                      <td className="px-4 py-2.5 text-right text-[var(--qd-text-3)]">-</td>
                    </tr>
                  );
                })}
                {filter !== "SKIPPED" && filtered.map((o) => {
                  const status = o.execution_status || o.status;
                  const isRejected = REJECTED_STATUSES.includes(status);
                  const rejectReason = o.reject_reason || o.error_message || o.status_message || "";
                  const pnl = orderPnl(o);
                  const reasonText = isRejected && rejectReason ? reasonLabel(rejectReason).label : undefined;
                  return (
                    <tr key={o.id} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]" data-testid={`order-${o.id}`}>
                      <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{o.created_at ? new Date(o.created_at).toLocaleTimeString("en-IN", { hour12: false }) : "-"}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{o.strategy_name || o.strategy_id || (String(o.source || "").includes("strategy:") ? String(o.source).split("strategy:").pop() : "manual")}</td>
                      <td className="px-4 py-2.5 text-white">{o.symbol}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{o.segment || o.exchange || "-"}</td>
                      <td className={`px-4 py-2.5 font-semibold ${o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{o.side}</td>
                      <td className="px-4 py-2.5">{o.qty}</td>
                      <td className="px-4 py-2.5">{formatINR(o.price)}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-loss)]">{o.stop_loss != null ? formatINR(o.stop_loss) : "-"}</td>
                      <td className="px-4 py-2.5 text-[var(--qd-profit)]">{o.take_profit != null ? formatINR(o.take_profit) : "-"}</td>
                      <td className={`px-4 py-2.5 ${FILLED_STATUSES.includes(status) ? "text-[var(--qd-profit)]" : isRejected ? "text-[var(--qd-loss)]" : "text-[var(--qd-warn)]"}`} title={reasonText}>
                        {status}
                      </td>
                      <td className={`px-4 py-2.5 text-right font-semibold ${pnl == null ? "text-[var(--qd-text-3)]" : pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                        {pnl == null ? "-" : `${pnl >= 0 ? "+" : ""}₹${formatINR(pnl)}`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {open && (
        <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" onClick={() => setOpen(false)}>
          <form onClick={(e) => e.stopPropagation()} onSubmit={submit} className="qd-card w-full max-w-md max-h-[90vh] qd-modal-card p-6 space-y-3" data-testid="order-modal">
            <h2 className="font-head text-xl text-white">Place Order</h2>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Exchange</label>
                <select
                  value={form.exchange}
                  onChange={(e) => {
                    const exchange = e.target.value;
                    setForm({
                      ...form,
                      exchange,
                      product: ["NFO", "BFO"].includes(exchange) ? "NRML" : form.product,
                      symbol: ["NSE", "BSE"].includes(exchange) ? (watch[0]?.symbol || "RELIANCE") : "",
                    });
                  }}
                  className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm"
                  data-testid="order-exchange"
                >
                  {["NSE", "BSE", "NFO", "BFO"].map((x) => <option key={x}>{x}</option>)}
                </select>
              </div>
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Product</label>
                <select value={form.product} onChange={(e) => setForm({ ...form, product: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-product">
                  {["MIS", "CNC", "NRML"].map((x) => <option key={x}>{x}</option>)}
                </select>
              </div>
            </div>
            <div className={["NSE", "BSE"].includes(form.exchange) ? "" : "hidden"}>
              <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Symbol</label>
              <select value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid={["NSE", "BSE"].includes(form.exchange) ? "order-symbol" : "order-symbol-watch-hidden"}>
                {watch.map((s) => <option key={s.symbol} value={s.symbol}>{`${s.symbol} - INR ${formatINR(s.price)}`}</option>)}
              </select>
            </div>
            {!["NSE", "BSE"].includes(form.exchange) && (
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Symbol</label>
                <input
                  value={form.symbol}
                  onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })}
                  placeholder="Exact trading symbol e.g. NIFTY25JAN24C23000"
                  className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm"
                  data-testid="order-symbol"
                />
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <button type="button" onClick={() => setForm({ ...form, side: "BUY" })} className={`py-2 font-mono text-xs uppercase rounded-sm ${form.side === "BUY" ? "qd-btn-buy" : "border border-[var(--qd-border)] text-white"}`} data-testid="side-buy">BUY</button>
              <button type="button" onClick={() => setForm({ ...form, side: "SELL" })} className={`py-2 font-mono text-xs uppercase rounded-sm ${form.side === "SELL" ? "qd-btn-sell" : "border border-[var(--qd-border)] text-white"}`} data-testid="side-sell">SELL</button>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Qty</label>
                <input type="number" min="1" value={form.qty} onChange={(e) => setForm({ ...form, qty: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-qty" />
              </div>
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Type</label>
                <select value={form.order_type} onChange={(e) => setForm({ ...form, order_type: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-type">
                  <option>MARKET</option><option>LIMIT</option>
                </select>
              </div>
            </div>
            {form.order_type === "LIMIT" && (
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Limit Price</label>
                <input type="number" value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-price" />
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Stop loss (app)</label>
                <input type="number" value={form.stop_loss} onChange={(e) => setForm({ ...form, stop_loss: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" />
              </div>
              <div>
                <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Take profit (app)</label>
                <input type="number" value={form.take_profit} onChange={(e) => setForm({ ...form, take_profit: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" />
              </div>
            </div>
            <div className="flex gap-2 pt-2">
              <button type="button" onClick={() => setOpen(false)} className="flex-1 border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] text-white py-2 text-xs font-mono uppercase rounded-sm">Cancel</button>
              <button type="submit" disabled={submitting} className={`flex-1 py-2 text-xs font-mono uppercase rounded-sm flex items-center justify-center gap-2 disabled:opacity-60 ${form.side === "BUY" ? "qd-btn-buy" : "qd-btn-sell"}`} data-testid="submit-order">
                {submitting ? <><Loader2 size={14} className="animate-spin" /> Placing...</> : `Confirm ${form.side}`}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
