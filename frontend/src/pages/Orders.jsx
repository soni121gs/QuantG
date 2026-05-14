import React, { useEffect, useState } from "react";
import { api, formatINR } from "../lib/api";
import { useNavigate } from "react-router-dom";
import { ShoppingCart } from "lucide-react";

export default function Orders() {
  const [orders, setOrders] = useState([]);
  const [watch, setWatch] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ symbol: "RELIANCE", side: "BUY", qty: 1, order_type: "MARKET", price: "" });
  const navigate = useNavigate();

  const load = () => Promise.all([
    api.get("/orders").then((r) => setOrders(r.data)),
    api.get("/market/watchlist").then((r) => setWatch(r.data)),
  ]);
  useEffect(() => { load(); }, []);

  const submit = async (e) => {
    e.preventDefault();
    try {
      await api.post("/orders", { ...form, qty: +form.qty, price: form.price ? +form.price : null });
      setOpen(false); load();
    } catch (e) { alert(e.response?.data?.detail || "Order failed"); }
  };

  return (
    <div className="space-y-4" data-testid="orders-page">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// EXECUTION</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1">Orders</h1>
        </div>
        <button onClick={() => setOpen(true)} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2" data-testid="place-order-btn">
          <ShoppingCart size={14} /> Place Order
        </button>
      </div>

      <div className="qd-card">
        {orders.length === 0 ? (
          <div className="p-10 text-center font-mono text-sm text-[var(--qd-text-2)]">No orders yet. Place your first trade.</div>
        ) : (
          <div className="qd-table-wrap"><table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">Time</th><th className="px-4 py-2">Symbol</th><th className="px-4 py-2">Side</th><th className="px-4 py-2">Qty</th><th className="px-4 py-2">Price</th><th className="px-4 py-2">Type</th><th className="px-4 py-2">Status</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {orders.map((o) => (
                <tr key={o.id} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]" data-testid={`order-${o.id}`}>
                  <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{new Date(o.created_at).toLocaleTimeString("en-IN", { hour12: false })}</td>
                  <td className="px-4 py-2.5 text-white">{o.symbol}</td>
                  <td className={`px-4 py-2.5 font-semibold ${o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{o.side}</td>
                  <td className="px-4 py-2.5">{o.qty}</td>
                  <td className="px-4 py-2.5">{formatINR(o.price)}</td>
                  <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{o.order_type}</td>
                  <td className="px-4 py-2.5 text-[var(--qd-profit)]">{o.status}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>

      {/* Place Order Modal */}
      {open && (
        <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" onClick={() => setOpen(false)}>
          <form onClick={(e) => e.stopPropagation()} onSubmit={submit} className="qd-card w-full max-w-md p-6 space-y-3" data-testid="order-modal">
            <h2 className="font-head text-xl text-white">Place Order</h2>
            <div>
              <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Symbol</label>
              <select value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="order-symbol">
                {watch.map((s) => <option key={s.symbol} value={s.symbol}>{`${s.symbol} — ₹${formatINR(s.price)}`}</option>)}
              </select>
            </div>
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
