import React, { useCallback, useEffect, useState } from "react";
import { api, formatINR, pctFmt } from "../lib/api";
import { LineChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis, AreaChart, Area } from "recharts";
import { TrendingUp, TrendingDown, Wallet, Activity, Layers, Target } from "lucide-react";
import { Link } from "react-router-dom";

const KPI = ({ label, value, sub, icon: Icon, tone }) => (
  <div className="qd-card p-4 flex flex-col gap-2" data-testid={`kpi-${label.replace(/\s+/g, "-").toLowerCase()}`}>
    <div className="flex items-center justify-between">
      <span className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">{label}</span>
      <Icon size={14} className="text-[var(--qd-text-3)]" strokeWidth={1.5} />
    </div>
    <div className={`font-mono text-2xl font-bold tracking-tight ${tone || "text-white"}`}>{value}</div>
    {sub && <div className="font-mono text-xs text-[var(--qd-text-2)]">{sub}</div>}
  </div>
);

const FundCell = ({ k, v, tone }) => (
  <div>
    <div className="text-[var(--qd-text-3)] text-[9px] uppercase tracking-widest">{k}</div>
    <div className={`mt-0.5 text-sm ${tone === "p" ? "text-[var(--qd-profit)]" : tone === "l" ? "text-[var(--qd-loss)]" : "text-white"}`}>{v}</div>
  </div>
);

export default function Dashboard() {
  const [pf, setPf] = useState(null);
  const [watch, setWatch] = useState([]);
  const [positions, setPositions] = useState([]);
  const [funds, setFunds] = useState(null);

  const load = useCallback(async () => {
    try {
      const [p, w, ps, f] = await Promise.all([
        api.get("/portfolio"),
        api.get("/market/watchlist"),
        api.get("/positions"),
        api.get("/funds"),
      ]);
      setPf(p.data); setWatch(w.data); setPositions(ps.data); setFunds(f.data);
    } catch { /* keep stale data */ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);

  const pnl = pf?.total_pnl ?? 0;
  const pnlTone = pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]";

  return (
    <div className="space-y-4" data-testid="dashboard-page">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// COMMAND CENTER</div>
          <h1 className="font-head text-3xl font-bold tracking-tight text-white mt-1">Dashboard</h1>
        </div>
        <Link
          to="/python"
          className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm"
          data-testid="new-strategy-btn"
        >
          + New Strategy
        </Link>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KPI label="Total PnL" value={`₹${formatINR(pnl)}`} icon={pnl >= 0 ? TrendingUp : TrendingDown} tone={pnlTone} sub={pctFmt((pnl / 100000) * 100)} />
        <KPI label="Available Cash" value={`₹${formatINR(funds?.available_cash ?? 0)}`} icon={Wallet} sub={funds?.source === "live" ? "From Zerodha" : "Paper"} />
        <KPI label="Used Margin" value={`₹${formatINR(funds?.used_margin ?? 0)}`} icon={Layers} sub={funds?.source === "live" ? "Live" : `Open: ₹${formatINR(funds?.opening_balance ?? 0)}`} />
        <KPI label="Live Strategies" value={`${pf?.live_strategies ?? 0}/${pf?.strategies ?? 0}`} icon={Activity} sub={`${pf?.orders ?? 0} orders all-time`} />
      </div>

      {/* Funds detail (live mode) */}
      {funds?.source === "live" && (
        <div className="qd-card p-4" data-testid="funds-detail">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-head text-base text-white flex items-center gap-2"><Wallet size={16} /> Funds & Margins</h2>
            <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-profit)] border border-[var(--qd-profit)] px-2 py-0.5 rounded-sm">ZERODHA LIVE</span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 font-mono text-xs">
            <FundCell k="Opening Balance" v={`₹${formatINR(funds.opening_balance)}`} />
            <FundCell k="Available" v={`₹${formatINR(funds.available_cash)}`} tone="p" />
            <FundCell k="Used" v={`₹${formatINR(funds.used_margin)}`} tone="l" />
            <FundCell k="Intraday Payin" v={`₹${formatINR(funds.intraday_payin)}`} />
            <FundCell k="M2M Realised" v={`₹${formatINR(funds.m2m_realised)}`} tone={funds.m2m_realised >= 0 ? "p" : "l"} />
            <FundCell k="M2M Unrealised" v={`₹${formatINR(funds.m2m_unrealised)}`} tone={funds.m2m_unrealised >= 0 ? "p" : "l"} />
            <FundCell k="SPAN" v={`₹${formatINR(funds.span)}`} />
            <FundCell k="Delivery" v={`₹${formatINR(funds.delivery_margin)}`} />
          </div>
        </div>
      )}

      {/* Chart + Watchlist */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
        <div className="qd-card p-4 lg:col-span-8 flex flex-col" data-testid="equity-card">
          <div className="flex items-center justify-between mb-2">
            <h2 className="font-head text-lg text-white">Equity Curve</h2>
            <span className="font-mono text-xs text-[var(--qd-text-2)]">30D • Paper Capital ₹1,00,000</span>
          </div>
          <div className="h-72">
            <ResponsiveContainer>
              <AreaChart data={pf?.equity_curve || []}>
                <defs>
                  <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#007AFF" stopOpacity={0.4} />
                    <stop offset="100%" stopColor="#007AFF" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="date" stroke="#666" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} tickFormatter={(d) => d.slice(5)} />
                <YAxis stroke="#666" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} domain={["dataMin - 1000", "dataMax + 1000"]} />
                <Tooltip
                  contentStyle={{ background: "#121212", border: "1px solid #2a2a2e", borderRadius: 2, fontFamily: "JetBrains Mono", fontSize: 11 }}
                  labelStyle={{ color: "#a3a3a3" }}
                  itemStyle={{ color: "#fff" }}
                />
                <Area type="monotone" dataKey="equity" stroke="#007AFF" fill="url(#eq)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="qd-card p-0 lg:col-span-4 flex flex-col" data-testid="watchlist-card">
          <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
            <h2 className="font-head text-base text-white">Market Watch</h2>
            <div className="flex items-center gap-2">
              {watch[0]?.source && (
                <span
                  className={`font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm ${
                    watch[0].source === "live"
                      ? "bg-[rgba(0,230,118,0.12)] text-[var(--qd-profit)] border border-[var(--qd-profit)]"
                      : "bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] border border-[var(--qd-border)]"
                  }`}
                  data-testid="watch-source"
                  title={watch[0].source === "live" ? "Real Zerodha data" : "Simulated — connect Zerodha for live"}
                >
                  {watch[0].source === "live" ? "ZERODHA" : "SIM"}
                </span>
              )}
              <span className="qd-live-dot" />
            </div>
          </div>
          <div className="divide-y divide-[var(--qd-border)] max-h-[300px] overflow-auto">
            {watch.map((s) => (
              <div key={s.symbol} className="px-4 py-2 flex items-center justify-between hover:bg-[var(--qd-surface-2)] transition-colors">
                <div>
                  <div className="font-mono text-sm text-white">{s.symbol}</div>
                  <div className="font-mono text-[10px] text-[var(--qd-text-3)] uppercase">{s.name}</div>
                </div>
                <div className="text-right">
                  <div className="font-mono text-sm text-white">{formatINR(s.price)}</div>
                  <div className={`font-mono text-xs ${s.change >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>{pctFmt(s.pct)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Positions */}
      <div className="qd-card p-0" data-testid="positions-card">
        <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
          <h2 className="font-head text-base text-white">Open Positions</h2>
          <span className="font-mono text-xs text-[var(--qd-text-2)]">{positions.length} active</span>
        </div>
        {positions.length === 0 ? (
          <div className="p-10 text-center">
            <Target className="mx-auto text-[var(--qd-text-3)] mb-3" />
            <p className="font-mono text-sm text-[var(--qd-text-2)]">No open positions. Place your first paper trade.</p>
          </div>
        ) : (
          <div className="qd-table-wrap">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">Symbol</th>
                <th className="px-4 py-2">Qty</th>
                <th className="px-4 py-2">Avg</th>
                <th className="px-4 py-2">LTP</th>
                <th className="px-4 py-2 text-right">PnL</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {positions.map((p) => (
                <tr key={p.symbol} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                  <td className="px-4 py-2.5 text-white">{p.symbol}</td>
                  <td className="px-4 py-2.5">{p.qty}</td>
                  <td className="px-4 py-2.5">{formatINR(p.avg_price)}</td>
                  <td className="px-4 py-2.5">{formatINR(p.ltp)}</td>
                  <td className={`px-4 py-2.5 text-right ${p.pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                    {p.pnl >= 0 ? "+" : ""}₹{formatINR(p.pnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </div>
  );
}
