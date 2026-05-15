import React, { useCallback, useEffect, useState } from "react";
import { api, formatINR } from "../lib/api";
import { LogOut, Loader2 } from "lucide-react";
import { toast } from "sonner";

export default function Positions() {
  const [pos, setPos] = useState([]);
  const [exiting, setExiting] = useState(null);

  const load = useCallback(() => api.get("/positions").then((r) => setPos(r.data)), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  const exit = async (symbol) => {
    if (!window.confirm(`Square off your full ${symbol} position at market price?`)) return;
    setExiting(symbol);
    try {
      const r = await api.post(`/positions/${symbol}/exit`);
      toast.success(`Exit order placed for ${symbol} (${r.data.side} ${r.data.qty})`);
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Exit failed");
    } finally { setExiting(null); }
  };

  const total = pos.reduce((a, p) => a + p.pnl, 0);

  return (
    <div className="space-y-4" data-testid="positions-page">
      <div>
        <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// HOLDINGS</div>
        <h1 className="font-head text-3xl font-bold text-white mt-1">Positions</h1>
        <p className="text-xs text-[var(--qd-text-2)] mt-1 font-mono">
          Click <span className="text-white">Exit</span> on any row to manually square off — works in both paper and live modes.
        </p>
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
                <th className="px-4 py-2">Symbol</th>
                <th className="px-4 py-2">Qty</th>
                <th className="px-4 py-2">Avg</th>
                <th className="px-4 py-2">LTP</th>
                <th className="px-4 py-2 text-right">PnL</th>
                <th className="px-4 py-2 text-right">Mode</th>
                <th className="px-4 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {pos.map((p) => {
                const pnlTone = p.pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]";
                const longPos = p.qty > 0;
                return (
                  <tr key={p.symbol} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]" data-testid={`pos-${p.symbol}`}>
                    <td className="px-4 py-2.5 text-white">{p.symbol}</td>
                    <td className={`px-4 py-2.5 ${longPos ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{longPos ? "+" : ""}{p.qty}</td>
                    <td className="px-4 py-2.5">{formatINR(p.avg_price)}</td>
                    <td className="px-4 py-2.5">{formatINR(p.ltp)}</td>
                    <td className={`px-4 py-2.5 text-right ${pnlTone}`}>{p.pnl >= 0 ? "+" : ""}₹{formatINR(p.pnl)}</td>
                    <td className="px-4 py-2.5 text-right">
                      <span className={`text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm ${
                        p.mode === "live" ? "bg-[rgba(255,59,48,0.12)] text-[var(--qd-loss)]" : "bg-[rgba(255,159,10,0.12)] text-[var(--qd-warn)]"
                      }`}>{p.mode}</span>
                    </td>
                    <td className="px-4 py-2.5 text-right">
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
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
        )}
      </div>
    </div>
  );
}
