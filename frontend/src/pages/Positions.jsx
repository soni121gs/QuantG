import React, { useCallback, useEffect, useState } from "react";
import { api, formatINR } from "../lib/api";

export default function Positions() {
  const [pos, setPos] = useState([]);

  const load = useCallback(() => api.get("/positions").then((r) => setPos(r.data)), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  const total = pos.reduce((a, p) => a + p.pnl, 0);

  return (
    <div className="space-y-4" data-testid="positions-page">
      <div>
        <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// HOLDINGS</div>
        <h1 className="font-head text-3xl font-bold text-white mt-1">Positions</h1>
      </div>

      <div className="qd-card p-4 flex items-center justify-between">
        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Total Unrealized PnL</span>
        <span className={`font-mono text-2xl font-bold ${total >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`} data-testid="total-pnl">
          {total >= 0 ? "+" : ""}₹{formatINR(total)}
        </span>
      </div>

      <div className="qd-card">
        {pos.length === 0 ? (
          <div className="p-10 text-center font-mono text-sm text-[var(--qd-text-2)]">No open positions.</div>
        ) : (
          <div className="qd-table-wrap"><table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">Symbol</th><th className="px-4 py-2">Qty</th><th className="px-4 py-2">Avg</th><th className="px-4 py-2">LTP</th><th className="px-4 py-2 text-right">PnL</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {pos.map((p) => (
                <tr key={p.symbol} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]" data-testid={`pos-${p.symbol}`}>
                  <td className="px-4 py-2.5 text-white">{p.symbol}</td>
                  <td className="px-4 py-2.5">{p.qty}</td>
                  <td className="px-4 py-2.5">{formatINR(p.avg_price)}</td>
                  <td className="px-4 py-2.5">{formatINR(p.ltp)}</td>
                  <td className={`px-4 py-2.5 text-right ${p.pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{p.pnl >= 0 ? "+" : ""}₹{formatINR(p.pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
    </div>
  );
}
