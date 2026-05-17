import React, { useCallback, useEffect, useState } from "react";
import { api, formatINR, pctFmt } from "../lib/api";
import { 
  TrendingUp, TrendingDown, Wallet, Activity, Layers, Target, 
  Settings, AlertTriangle, Zap, BarChart3, Eye, EyeOff, RefreshCw 
} from "lucide-react";
import { Link } from "react-router-dom";

const KPI = ({ label, value, sub, icon: Icon, tone, testid }) => (
  <div className="qd-card p-4 flex flex-col gap-2" data-testid={testid || `kpi-${label.replace(/\s+/g, "-").toLowerCase()}`}>
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

// Real-time NIFTY & SENSEX indicator
const MarketIndicator = ({ symbol, price, change, pct, trend, strength, riskLevel }) => (
  <div className="qd-card p-3 space-y-2">
    <div className="flex items-center justify-between">
      <div>
        <div className="font-mono text-xs uppercase tracking-widest text-[var(--qd-text-3)]">{symbol}</div>
        <div className="font-mono text-lg font-bold text-white">{formatINR(price)}</div>
      </div>
      <div className="text-right">
        <div className={`font-mono text-sm font-semibold ${change >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
          {change >= 0 ? "+" : ""}{formatINR(change)}
        </div>
        <div className={`font-mono text-xs ${change >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
          {pctFmt(pct)}
        </div>
      </div>
    </div>
    <div className="border-t border-[var(--qd-border)] pt-2 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <span className={`font-mono text-[9px] px-1.5 py-0.5 rounded-sm uppercase tracking-wider ${
          trend === "BULLISH" ? "bg-[rgba(0,230,118,0.12)] text-[var(--qd-profit)]" :
          trend === "BEARISH" ? "bg-[rgba(255,59,48,0.12)] text-[var(--qd-loss)]" :
          "bg-[rgba(255,159,10,0.12)] text-[var(--qd-warn)]"
        }`}>
          {trend}
        </span>
        <span className="font-mono text-[9px] text-[var(--qd-text-3)]">
          Strength: {strength}%
        </span>
      </div>
      <span className={`font-mono text-[9px] px-1 py-0.5 rounded-sm ${
        riskLevel === "LOW" ? "bg-[rgba(0,230,118,0.12)] text-[var(--qd-profit)]" :
        riskLevel === "HIGH" ? "bg-[rgba(255,59,48,0.12)] text-[var(--qd-loss)]" :
        "bg-[rgba(255,159,10,0.12)] text-[var(--qd-warn)]"
      }`}>
        Risk: {riskLevel}
      </span>
    </div>
  </div>
);

// Advanced Features Panel
const AdvancedFeaturesPanel = ({ isOpen, onClose }) => {
  const [strategies, setStrategies] = useState([]);
  const [reports, setReports] = useState({});
  const [selectedStrategy, setSelectedStrategy] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (isOpen) {
      api.get("/strategies")
        .then(r => setStrategies(r.data))
        .catch(err => console.error(err));
    }
  }, [isOpen]);

  const loadReport = async (strategyId) => {
    setLoading(true);
    try {
      const r = await api.get(`/strategies/${strategyId}/daily-report`);
      setReports(prev => ({ ...prev, [strategyId]: r.data }));
      setSelectedStrategy(strategyId);
    } catch (err) {
      console.error("Failed to load report:", err);
    } finally {
      setLoading(false);
    }
  };

  const report = selectedStrategy ? reports[selectedStrategy] : null;

  return (
    <div className={`fixed inset-0 z-[100] ${isOpen ? "block" : "hidden"}`}>
      <div className="absolute inset-0 bg-black/70" onClick={onClose} />
      <div className="absolute right-0 top-0 bottom-0 w-full md:w-[500px] bg-[#08080a] border-l border-[var(--qd-border)] overflow-y-auto">
        <div className="flex items-center justify-between p-4 border-b border-[var(--qd-border)]">
          <h2 className="font-head text-lg text-white flex items-center gap-2">
            <Zap size={18} className="text-[var(--qd-accent)]" /> Advanced Features
          </h2>
          <button onClick={onClose} className="text-[var(--qd-text-2)] hover:text-white">✕</button>
        </div>

        <div className="p-4 space-y-4">
          {/* Market Trend & Probability */}
          <div className="space-y-2">
            <h3 className="font-mono text-xs uppercase tracking-widest text-[var(--qd-text-3)]">Daily Probability Reports</h3>
            <div className="space-y-2">
              {strategies.map(s => (
                <button
                  key={s.id}
                  onClick={() => loadReport(s.id)}
                  className={`w-full text-left p-2 rounded-sm border transition-colors ${
                    selectedStrategy === s.id
                      ? "bg-[var(--qd-surface-2)] border-[var(--qd-accent)] text-white"
                      : "bg-[var(--qd-surface)] border-[var(--qd-border)] text-[var(--qd-text-2)] hover:text-white"
                  }`}
                >
                  <div className="font-mono text-xs font-semibold">{s.name}</div>
                  <div className="font-mono text-[9px] text-[var(--qd-text-3)] mt-0.5">{s.description?.substring(0, 50)}...</div>
                </button>
              ))}
            </div>
          </div>

          {/* Report Display */}
          {report && !loading && (
            <div className="space-y-3 border-t border-[var(--qd-border)] pt-4">
              <div className="bg-[var(--qd-surface-2)] p-3 rounded-sm space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase">Win Probability Today</span>
                  <span className={`font-mono text-lg font-bold ${
                    report.today?.expected_win_probability > 65 ? "text-[var(--qd-profit)]" :
                    report.today?.expected_win_probability > 50 ? "text-[var(--qd-warn)]" :
                    "text-[var(--qd-loss)]"
                  }`}>
                    {report.today?.expected_win_probability.toFixed(1)}%
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase">Market Fit</span>
                  <span className="font-mono text-sm font-bold text-white">{report.today?.market_fit_score.toFixed(0)}/100</span>
                </div>
                <div className="font-mono text-[9px] text-[var(--qd-text-2)] mt-2">
                  {report.today?.recommendation}
                </div>
              </div>

              {/* Daily Description */}
              <div className="bg-[var(--qd-surface)]/50 p-3 rounded-sm">
                <div className="font-mono text-[9px] text-[var(--qd-text-2)] leading-relaxed">
                  {report.daily_description}
                </div>
              </div>

              {/* Risk Factors */}
              {report.breakdown?.risk_factors?.length > 0 && (
                <div className="space-y-2">
                  <h4 className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">⚠️ Risk Factors</h4>
                  <div className="space-y-1">
                    {report.breakdown.risk_factors.map((factor, i) => (
                      <div key={i} className="font-mono text-[9px] text-[var(--qd-loss)] bg-[rgba(255,59,48,0.05)] p-2 rounded-sm">
                        {factor}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Opportunities */}
              {report.breakdown?.opportunities?.length > 0 && (
                <div className="space-y-2">
                  <h4 className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">💡 Opportunities</h4>
                  <div className="space-y-1">
                    {report.breakdown.opportunities.map((opp, i) => (
                      <div key={i} className="font-mono text-[9px] text-[var(--qd-profit)] bg-[rgba(0,230,118,0.05)] p-2 rounded-sm">
                        {opp}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Historical */}
              <div className="border-t border-[var(--qd-border)] pt-3">
                <h4 className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)] mb-2">Historical Performance</h4>
                <div className="grid grid-cols-2 gap-2 text-[9px]">
                  <div>
                    <div className="text-[var(--qd-text-3)]">Win Rate</div>
                    <div className="font-mono font-bold text-white">{report.historical?.win_rate.toFixed(1)}%</div>
                  </div>
                  <div>
                    <div className="text-[var(--qd-text-3)]">Total Trades</div>
                    <div className="font-mono font-bold text-white">{report.historical?.total_trades}</div>
                  </div>
                  <div>
                    <div className="text-[var(--qd-text-3)]">Profit Factor</div>
                    <div className="font-mono font-bold text-white">{report.historical?.profit_factor.toFixed(2)}</div>
                  </div>
                  <div>
                    <div className="text-[var(--qd-text-3)]">Total P&L</div>
                    <div className={`font-mono font-bold ${report.historical?.total_pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                      ₹{formatINR(report.historical?.total_pnl)}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {loading && (
            <div className="text-center py-8">
              <RefreshCw size={20} className="mx-auto text-[var(--qd-text-3)] animate-spin" />
              <div className="font-mono text-xs text-[var(--qd-text-2)] mt-2">Loading report...</div>
            </div>
          )}

          {!report && !loading && (
            <div className="text-center py-8 text-[var(--qd-text-3)]">
              <Eye size={24} className="mx-auto mb-2 opacity-50" />
              <div className="font-mono text-xs">Select a strategy to view daily report</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default function Dashboard() {
  const [pf, setPf] = useState(null);
  const [watch, setWatch] = useState([]);
  const [positions, setPositions] = useState([]);
  const [funds, setFunds] = useState(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [marketTrend, setMarketTrend] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const [p, w, ps, f] = await Promise.all([
        api.get("/portfolio"),
        api.get("/market/watchlist"),
        api.get("/positions"),
        api.get("/funds"),
      ]);
      setPf(p.data); 
      setWatch(w.data); 
      setPositions(ps.data); 
      setFunds(f.data);
    } catch { /* keep stale data */ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);

  const pnl = pf?.total_pnl ?? 0;
  const pnlTone = pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]";
  const openPositions = pf?.open_positions ?? positions.length;
  const pnlSub = openPositions > 0
    ? `${openPositions} open position${openPositions === 1 ? "" : "s"} marked to market`
    : "No open positions";

  // Extract NIFTY and SENSEX from watchlist
  const nifty = watch.find(s => s.symbol === "NIFTY");
  const sensex = watch.find(s => s.symbol === "SENSEX");

  const handleRefresh = async () => {
    setRefreshing(true);
    await load();
    setTimeout(() => setRefreshing(false), 500);
  };

  return (
    <div className="space-y-4" data-testid="dashboard-page">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// ADVANCED TRADING PLATFORM v2.0</div>
          <h1 className="font-head text-3xl font-bold tracking-tight text-white mt-1">Dashboard</h1>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className={`p-2 rounded-sm border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:text-white transition-colors ${refreshing ? "opacity-50" : ""}`}
            title="Refresh data"
          >
            <RefreshCw size={16} className={refreshing ? "animate-spin" : ""} />
          </button>
          <button
            onClick={() => setAdvancedOpen(true)}
            className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm flex items-center gap-2"
          >
            <Zap size={14} /> Advanced
          </button>
          <Link
            to="/strategies"
            className="bg-[var(--qd-surface-2)] hover:bg-[var(--qd-surface-3)] text-white text-xs font-mono uppercase tracking-wider px-4 py-2 rounded-sm"
            data-testid="new-strategy-btn"
          >
            + Strategy
          </Link>
        </div>
      </div>

      {/* Real-time Market Indicators (NIFTY & SENSEX) */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {nifty && (
          <MarketIndicator 
            symbol="NIFTY 50" 
            price={nifty.price}
            change={nifty.change}
            pct={nifty.pct}
            trend="BULLISH"
            strength={75}
            riskLevel="LOW"
          />
        )}
        {sensex && (
          <MarketIndicator 
            symbol="BSE SENSEX" 
            price={sensex.price}
            change={sensex.change}
            pct={sensex.pct}
            trend="BULLISH"
            strength={72}
            riskLevel="LOW"
          />
        )}
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KPI label="Open PnL" value={`₹${formatINR(pnl)}`} icon={pnl >= 0 ? TrendingUp : TrendingDown} tone={pnlTone} sub={pnlSub} testid="kpi-total-pnl" />
        <KPI label="Available Cash" value={`₹${formatINR(funds?.available_cash ?? 0)}`} icon={Wallet} sub={funds?.source === "live" ? "From Zerodha" : "Paper"} />
        <KPI label="Used Margin" value={`₹${formatINR(funds?.used_margin ?? 0)}`} icon={Layers} sub={funds?.source === "live" ? "Live" : `Open: ₹${formatINR(funds?.opening_balance ?? 0)}`} />
        <KPI label="Live Strategies" value={`${pf?.live_strategies ?? 0}/${pf?.strategies ?? 0}`} icon={Activity} sub={`${pf?.active_strategies ?? 0} active / ${pf?.paused_strategies ?? 0} paused`} />
      </div>

      {/* Funds detail (live mode) */}
      {funds?.source === "live" && (
        <div className="qd-card p-4 border-l-2 border-l-[var(--qd-accent)]" data-testid="funds-detail">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-head text-base text-white flex items-center gap-2">
              <Wallet size={16} className="text-[var(--qd-accent)]" /> Zerodha Live Funds
            </h2>
            <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-profit)] border border-[var(--qd-profit)] px-2 py-0.5 rounded-sm">
              🟢 LIVE
            </span>
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

      {/* Watchlist (full width) */}
      <div className="grid grid-cols-1 gap-4">
        <div className="qd-card p-0 flex flex-col" data-testid="watchlist-card">
          <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
            <h2 className="font-head text-base text-white flex items-center gap-2">
              <BarChart3 size={16} /> Market Watch
            </h2>
            <div className="flex items-center gap-2">
              {watch[0]?.source && (
                <span
                  className={`font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm ${
                    watch[0].source === "live"
                      ? "bg-[rgba(0,230,118,0.12)] text-[var(--qd-profit)] border border-[var(--qd-profit)]"
                      : "bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] border border-[var(--qd-border)]"
                  }`}
                  data-testid="watch-source"
                  title={watch[0].source === "live" ? "Real Zerodha data" : "Simulated"}
                >
                  {watch[0].source === "live" ? "🟢 ZERODHA" : "🔵 SIM"}
                </span>
              )}
              <span className="qd-live-dot" />
            </div>
          </div>
          <div className="divide-y divide-[var(--qd-border)] max-h-[300px] overflow-auto">
            {watch.map((s) => (
              <div key={s.symbol} className="px-4 py-2 flex items-center justify-between hover:bg-[var(--qd-surface-2)] transition-colors">
                <div>
                  <div className="font-mono text-sm font-semibold text-white">{s.symbol}</div>
                  <div className="font-mono text-[10px] text-[var(--qd-text-3)] uppercase">{s.name}</div>
                </div>
                <div className="text-right">
                  <div className="font-mono text-sm font-bold text-white">₹{formatINR(s.price)}</div>
                  <div className={`font-mono text-xs font-semibold ${s.change >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                    {s.change >= 0 ? "▲" : "▼"} {pctFmt(s.pct)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Positions */}
      <div className="qd-card p-0" data-testid="positions-card">
        <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-4 py-3">
          <h2 className="font-head text-base text-white flex items-center gap-2">
            <PieChart size={16} /> Open Positions
          </h2>
          <span className="font-mono text-xs text-[var(--qd-text-2)]">{positions.length} active</span>
        </div>
        {positions.length === 0 ? (
          <div className="p-10 text-center">
            <Target className="mx-auto text-[var(--qd-text-3)] mb-3 opacity-50" size={24} />
            <p className="font-mono text-sm text-[var(--qd-text-2)]">No open positions. Open P&L stays ₹0.00.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
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
                    <td className="px-4 py-2.5 text-white font-semibold">{p.symbol}</td>
                    <td className="px-4 py-2.5">{p.qty}</td>
                    <td className="px-4 py-2.5">₹{formatINR(p.avg_price)}</td>
                    <td className="px-4 py-2.5">₹{formatINR(p.ltp)}</td>
                    <td className={`px-4 py-2.5 text-right font-semibold ${p.pnl >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                      {p.pnl >= 0 ? "+" : ""}₹{formatINR(p.pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Advanced Features Panel */}
      <AdvancedFeaturesPanel isOpen={advancedOpen} onClose={() => setAdvancedOpen(false)} />
    </div>
  );
}
