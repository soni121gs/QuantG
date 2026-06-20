import React from "react";
import { formatINR } from "../../lib/api";

const money = (value) => `INR ${formatINR(value ?? 0)}`;
const toneClass = (value) => ((value ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]");
const profitFactorLabel = (value) => (value == null ? "No losses" : Number(value).toFixed(2));

export const StrategyPerformanceTable = ({ rows }) => (
  <div className="overflow-x-auto">
    <table className="w-full text-left text-xs">
      <thead className="bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] uppercase font-mono">
        <tr>
          <th className="px-4 py-3">Rank</th>
          <th className="px-4 py-3">Strategy</th>
          <th className="px-4 py-3 text-right">Trades</th>
          <th className="px-4 py-3 text-right">Win</th>
          <th className="px-4 py-3 text-right">7D</th>
          <th className="px-4 py-3 text-right">30D</th>
          <th className="px-4 py-3 text-right">Net P&L</th>
          <th className="px-4 py-3 text-right">PF</th>
          <th className="px-4 py-3 text-right">Risk Adj</th>
          <th className="px-4 py-3 text-right">DD</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-[var(--qd-border)]">
        {rows.length === 0 ? (
          <tr>
            <td colSpan="10" className="px-4 py-8 text-center text-[var(--qd-text-3)]">
              No closed strategy trades yet.
            </td>
          </tr>
        ) : (
          rows.map((row) => {
            const lifetime = row.lifetime || {};
            const last7 = row.last_7_days || {};
            const last30 = row.last_30_days || {};
            return (
              <tr key={row.strategy_id} className="hover:bg-[var(--qd-surface)]/30">
                <td className="px-4 py-3 font-mono text-[var(--qd-text-2)]">#{row.rank}</td>
                <td className="px-4 py-3">
                  <div className="font-semibold text-white">{row.strategy_name}</div>
                  <div className="mt-1 font-mono text-[11px] uppercase text-[var(--qd-text-3)]">
                    {row.recommendation || "NO_DATA"}
                  </div>
                </td>
                <td className="px-4 py-3 text-right font-mono text-white">{lifetime.total_trades || 0}</td>
                <td className="px-4 py-3 text-right font-mono text-white">
                  {Number(lifetime.win_rate || 0).toFixed(1)}%
                </td>
                <td className={`px-4 py-3 text-right font-mono ${toneClass(last7.net_pnl)}`}>
                  {money(last7.net_pnl)}
                </td>
                <td className={`px-4 py-3 text-right font-mono ${toneClass(last30.net_pnl)}`}>
                  {money(last30.net_pnl)}
                </td>
                <td className={`px-4 py-3 text-right font-mono font-bold ${toneClass(lifetime.net_pnl)}`}>
                  {money(lifetime.net_pnl)}
                </td>
                <td className="px-4 py-3 text-right font-mono text-white">
                  {profitFactorLabel(lifetime.profit_factor)}
                </td>
                <td className={`px-4 py-3 text-right font-mono ${toneClass(row.risk_adjusted_return)}`}>
                  {Number(row.risk_adjusted_return || 0).toFixed(2)}
                </td>
                <td className="px-4 py-3 text-right font-mono text-[var(--qd-loss)]">
                  {money(lifetime.max_drawdown)}
                </td>
              </tr>
            );
          })
        )}
      </tbody>
    </table>
  </div>
);

export default StrategyPerformanceTable;
