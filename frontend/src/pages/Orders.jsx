import React, { useEffect, useState } from "react";
import { api, formatINR, formatApiErrorDetail } from "../lib/api";
import { useExecutionState } from "../hooks/useExecutionState";
import { AlertTriangle } from "lucide-react";
import { toast } from "sonner";

const OPEN_STATUSES = ["NEW", "PLACED", "OPEN", "PARTIAL_FILL", "EXIT_PENDING", "PENDING", "PENDING_BROKER", "TRIGGER PENDING", "MODIFY PENDING", "VALIDATION PENDING"];
const FILLED_STATUSES = ["FILLED", "CLOSED", "COMPLETE"];
const REJECTED_STATUSES = ["CANCELLED", "REJECTED", "FAILED", "BROKER_NOT_FOUND", "STALE"];

export default function Orders() {
  const { orders, error, refresh, executionBroker } = useExecutionState({ pollMs: 15000 });
  const [watch, setWatch] = useState([]);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("ALL");
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
  const [symbolResults, setSymbolResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    api.get("/market/watchlist").then((r) => setWatch(r.data)).catch(() => {});
  }, []);

  const visibleOrders = orders.filter((o) => o.visibility !== "hidden");
  const filtered = visibleOrders.filter((o) => {
    const status = o.execution_status || o.status;
    if (filter === "ALL") return true;
    if (filter === "OPEN") return OPEN_STATUSES.includes(status);
    if (filter === "COMPLETE") return FILLED_STATUSES.includes(status);
    if (filter === "CANCELLED") return REJECTED_STATUSES.includes(status);
    if (filter === "FAILED") return ["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE"].includes(status);
    return true;
  });

  const submit = async (e) => {
    e.preventDefault();
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
    }
  };

  const collectInstrumentSymbols = (node, out = []) => {
    if (!node || out.length > 20) return out;
    if (Array.isArray(node)) {
      node.forEach((x) => collectInstrumentSymbols(x, out));
      return out;
    }
    if (typeof node === "object") {
      const symbol = node.trdSym || node.trading_symbol || node.tradingSymbol || node.symbol || node.pSymbolName || node.ts;
      if (symbol && !out.some((x) => x.symbol === symbol)) {
        out.push({ symbol, token: node.instrument_token || node.instrumentToken || node.token || node.tk });
      }
      Object.values(node).forEach((x) => collectInstrumentSymbols(x, out));
    }
    return out;
  };

  const searchInstrumentSymbol = async () => {
    if (!form.symbol.trim()) return;
    setSearching(true);
    try {
      setSymbolResults(collectInstrumentSymbols([]));
    } catch (e) {
      alert(e.response?.data?.detail || "Instrument search failed");
    } finally {
      setSearching(false);
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

  return (
    <div className="space-y-4" data-testid="orders-page">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// EXECUTION</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1">Orders</h1>
          <p className="text-xs text-[var(--qd-text-2)] mt-1 font-mono">Broker: {executionBroker}</p>
        </div>
        <div className="flex gap-2">
          <button onClick={syncBroker} disabled={syncing} className="border border-[var(--qd-border)] hover:border-[var(--qd-profit)] disabled:opacity-50 text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm">
            Sync with Broker
          </button>
        </div>
      </div>

      {error && (
        <div className="qd-card border-l-2 border-l-[var(--qd-warn)] p-3 flex gap-2">
          <AlertTriangle size={16} className="text-[var(--qd-warn)]" />
          <span className="text-sm text-[var(--qd-text-2)]">{error}</span>
        </div>
      )}

      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex gap-1" data-testid="order-filter">
          {["ALL", "OPEN", "COMPLETE", "CANCELLED", "FAILED"].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 text-[10px] font-mono uppercase tracking-widest rounded-sm ${
                filter === f ? "bg-[var(--qd-accent)] text-white" : "border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:text-white"
              }`}
              data-testid={`filter-${f.toLowerCase()}`}
            >
              {f} {f !== "ALL" && `- ${visibleOrders.filter((o) => {
                const status = o.execution_status || o.status;
                if (f === "OPEN") return OPEN_STATUSES.includes(status);
                if (f === "COMPLETE") return FILLED_STATUSES.includes(status);
                if (f === "FAILED") return ["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE"].includes(status);
                return REJECTED_STATUSES.includes(status);
              }).length}`}
            </button>
          ))}
        </div>
        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">
          showing {filtered.length} of {visibleOrders.length}
        </span>
      </div>

      <div className="qd-card">
        {filtered.length === 0 ? (
          <div className="p-10 text-center font-mono text-sm text-[var(--qd-text-2)]">No orders match this filter.</div>
        ) : (
          <div className="qd-table-wrap"><table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">Time</th><th className="px-4 py-2">Strategy</th><th className="px-4 py-2">Symbol</th><th className="px-4 py-2">Seg</th><th className="px-4 py-2">Side</th><th className="px-4 py-2">Qty</th><th className="px-4 py-2">Price</th><th className="px-4 py-2">SL</th><th className="px-4 py-2">TP</th><th className="px-4 py-2">Status</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {filtered.map((o) => {
                const status = o.execution_status || o.status;
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
                  <td className={`px-4 py-2.5 ${
                    FILLED_STATUSES.includes(status) ? "text-[var(--qd-profit)]" :
                    REJECTED_STATUSES.includes(status) ? "text-[var(--qd-loss)]" :
                    "text-[var(--qd-warn)]"
                  }`} title={o.status_message || ""}>{status}</td>
                </tr>
              );})}
            </tbody>
          </table></div>
        )}
      </div>

      {/* Emergency order entry is intentionally hidden in the Upstox-only algo flow. */}
      {open && (
        <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" onClick={() => setOpen(false)}>
          <form onClick={(e) => e.stopPropagation()} onSubmit={submit} className="qd-card w-full max-w-md p-6 space-y-3" data-testid="order-modal">
            <h2 className="font-head text-xl text-white">Place Order</h2>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Exchange</label>
                <select
                  value={form.exchange}
                  onChange={(e) => {
                    const exchange = e.target.value;
                    setSymbolResults([]);
                    setForm({
                      ...form,
                      exchange,
                      product: ["MCX", "NFO", "BFO", "CDS"].includes(exchange) ? "NRML" : form.product,
                      symbol: ["NSE", "BSE"].includes(exchange) ? (watch[0]?.symbol || "RELIANCE") : "",
                    });
                  }}
                  className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm"
                  data-testid="order-exchange"
                >
                  {["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"].map((x) => <option key={x}>{x}</option>)}
                </select>
              </div>
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Product</label>
                <select value={form.product} onChange={(e) => setForm({ ...form, product: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-product">
                  {["MIS", "CNC", "NRML"].map((x) => <option key={x}>{x}</option>)}
                </select>
              </div>
            </div>
            <div className={["NSE", "BSE"].includes(form.exchange) ? "" : "hidden"}>
              <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Symbol</label>
              <select value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid={["NSE", "BSE"].includes(form.exchange) ? "order-symbol" : "order-symbol-watch-hidden"}>
                {watch.map((s) => <option key={s.symbol} value={s.symbol}>{`${s.symbol} - INR ${formatINR(s.price)}`}</option>)}
              </select>
            </div>
            {!["NSE", "BSE"].includes(form.exchange) && (
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Symbol</label>
                <div className="flex gap-2 mt-1">
                  <input value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })} placeholder="Exact trading symbol" className="flex-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-symbol" />
                  <button type="button" onClick={searchInstrumentSymbol} disabled={searching || !form.symbol.trim()} className="border border-[var(--qd-border)] hover:border-[var(--qd-accent)] disabled:opacity-40 text-white px-3 py-2 text-xs font-mono uppercase rounded-sm">
                    Search
                  </button>
                </div>
                {symbolResults.length > 0 && (
                  <div className="mt-2 max-h-28 overflow-y-auto border border-[var(--qd-border)] rounded-sm">
                    {symbolResults.map((r) => (
                      <button key={`${r.symbol}-${r.token || ""}`} type="button" onClick={() => setForm({ ...form, symbol: r.symbol })} className="w-full text-left px-3 py-1.5 text-xs font-mono text-[var(--qd-text-2)] hover:text-white hover:bg-[var(--qd-surface-2)]">
                        {r.symbol}{r.token ? ` - ${r.token}` : ""}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <button type="button" onClick={() => setForm({ ...form, side: "BUY" })} className={`py-2 font-mono text-xs uppercase rounded-sm ${form.side === "BUY" ? "qd-btn-buy" : "border border-[var(--qd-border)] text-white"}`} data-testid="side-buy">BUY</button>
              <button type="button" onClick={() => setForm({ ...form, side: "SELL" })} className={`py-2 font-mono text-xs uppercase rounded-sm ${form.side === "SELL" ? "qd-btn-sell" : "border border-[var(--qd-border)] text-white"}`} data-testid="side-sell">SELL</button>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Qty</label>
                <input type="number" min="1" value={form.qty} onChange={(e) => setForm({ ...form, qty: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-qty" />
              </div>
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Type</label>
                <select value={form.order_type} onChange={(e) => setForm({ ...form, order_type: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-type">
                  <option>MARKET</option><option>LIMIT</option>
                </select>
              </div>
            </div>
            {form.order_type === "LIMIT" && (
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Limit Price</label>
                <input type="number" value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-price" />
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Stop loss (app)</label>
                <input type="number" value={form.stop_loss} onChange={(e) => setForm({ ...form, stop_loss: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" />
              </div>
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Take profit (app)</label>
                <input type="number" value={form.take_profit} onChange={(e) => setForm({ ...form, take_profit: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" />
              </div>
            </div>
            <div className="flex gap-2 pt-2">
              <button type="button" onClick={() => setOpen(false)} className="flex-1 border border-[var(--qd-border)] hover:border-white text-white py-2 text-xs font-mono uppercase rounded-sm">Cancel</button>
              <button type="submit" className={`flex-1 py-2 text-xs font-mono uppercase rounded-sm ${form.side === "BUY" ? "qd-btn-buy" : "qd-btn-sell"}`} data-testid="submit-order">
                Confirm {form.side}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

