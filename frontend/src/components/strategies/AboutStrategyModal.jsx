import React from "react";
import { X, Bot, Activity, RefreshCw } from "lucide-react";
import { Button } from "../ui/button";
import { formatINR } from "../../lib/api";

const money = (value) => `INR ${formatINR(value ?? 0)}`;

function Metric({ label, value, tone, compact = false }) {
  return (
    <div className="min-w-0">
      <div className="font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)]">{label}</div>
      <div className={`${compact ? "text-xs" : "text-sm"} mt-1 truncate font-mono font-semibold ${tone || "text-[var(--qd-text)]"}`}>{value}</div>
    </div>
  );
}

export const AboutStrategyModal = ({ s, score, testing, testResult, testRun, onClose }) => {
  const live = s.status === "live";
  const risk = s.visual_config?.risk || {};
  const options = s.visual_config?.options || {};

  // AI score values
  const aiScore = score?.score ?? s.ai_confidence_score;
  const aiReason = score?.reason ?? s.ai_confidence_reason;
  const marketInfo = score?.market;

  // Split reasons
  const telemetryReasons = aiReason ? aiReason.split(";").map(r => r.trim()).filter(Boolean) : [];

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/80 p-3 sm:p-4 md:items-center" onClick={onClose} data-testid="about-strategy-modal">
      <div className="qd-card qd-modal-card w-full max-w-4xl max-h-[92vh] p-4 sm:p-5 md:p-6 flex flex-col gap-4" onClick={(e) => e.stopPropagation()}>

        {/* Header */}
        <div className="flex items-start justify-between gap-3 border-b border-[var(--qd-border)] pb-3">
          <div className="min-w-0">
            <div className="qd-section-title flex items-center gap-1.5">
              <span>{s.instrument_group || "NSE"} / {s.kind}</span>
              <span className="w-1 h-1 rounded-full bg-[var(--qd-text-3)]" />
              <span>{s.strategy_type || "Option Buying"}</span>
            </div>
            <h2 className="mt-1 font-head text-lg font-semibold text-[var(--qd-text)] sm:text-xl">{s.name}</h2>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className={`px-2 py-0.5 rounded text-[11px] font-mono font-bold uppercase tracking-wider ${s.mode === "live" ? "bg-rose-500/10 border border-rose-500/30 text-[var(--qd-loss)]" : "bg-cyan-500/10 border border-cyan-500/30 text-[var(--qd-cyan)]"}`}>
                {s.mode === "live" ? "LIVE PRODUCTION" : "PAPER SIMULATED"}
              </span>
              <span className={`px-2 py-0.5 rounded text-[11px] font-mono font-bold uppercase tracking-wider ${live ? "bg-emerald-500/10 border border-emerald-500/30 text-[var(--qd-profit)]" : "bg-amber-500/10 border border-amber-500/30 text-[var(--qd-warn)]"}`}>
                {s.status?.toUpperCase() || "DRAFT"}
              </span>
            </div>
          </div>
          <button onClick={onClose} className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded border border-[var(--qd-border)] text-[var(--qd-text-3)] transition-colors hover:border-[var(--qd-border-strong)] hover:text-[var(--qd-text)]" aria-label="Close modal">
            <X size={16} />
          </button>
        </div>

        {/* Responsive Grid */}
        <div className="grid grid-cols-1 gap-4 min-h-0 overflow-y-visible md:grid-cols-2">

          {/* Left Column: Info & AI Telemetry */}
          <div className="space-y-4">
            {/* Description */}
            <div className="space-y-2">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold">Strategy Description</h3>
              <div className="rounded-md border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 text-sm text-[var(--qd-text)] leading-relaxed">
                {s.description || "No description provided for this strategy. Configure descriptions in the builder or editor."}
              </div>
            </div>

            {/* Suitability Badge */}
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-3)]">Market Suitability:</span>
              <span className="rounded-full border border-[var(--qd-border-strong)] bg-[var(--qd-surface-3)] px-2.5 py-1 text-xs font-semibold text-[var(--qd-text)]">
                {s.market_suitability || "Any Market Condition"}
              </span>
            </div>

            {/* AI Telemetry System */}
            <div className="space-y-3 border-t border-[var(--qd-border)] pt-4">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold flex items-center gap-1.5">
                <Bot size={14} className="text-[var(--qd-accent)]" /> Live AI Telemetry & Confidence
              </h3>

              {aiScore != null ? (
                <div className="rounded-md border border-[var(--qd-border-strong)] bg-[var(--qd-surface-2)] p-3 space-y-3">
                  {/* Gauge */}
                  <div className="flex items-center justify-between gap-4">
                    <div className="space-y-1">
                      <span className="font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)]">AI Confidence Score</span>
                      <div className="font-head text-2xl font-semibold text-[var(--qd-text)] flex items-baseline gap-1">
                        {aiScore}%
                        <span className="text-xs text-[var(--qd-text-3)] font-mono">confidence</span>
                      </div>
                    </div>
                    {/* Progress Bar (mini gauge) */}
                    <div className="w-24 bg-[var(--qd-surface-3)] rounded-full h-3 overflow-hidden border border-[var(--qd-border)]">
                      <div
                        className={`h-full rounded-full transition-all duration-500 ${
                          aiScore >= 70 ? "bg-[var(--qd-profit)]" : aiScore >= 40 ? "bg-[var(--qd-accent)]" : "bg-[var(--qd-loss)]"
                        }`}
                        style={{ width: `${aiScore}%` }}
                      />
                    </div>
                  </div>

                  {/* Reasons split */}
                  {telemetryReasons.length > 0 && (
                    <div className="space-y-2">
                      <div className="font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)]">Telemetry Signals Checked</div>
                      <div className="flex flex-wrap gap-1.5">
                        {telemetryReasons.map((reasonText, idx) => {
                          const isWarning = reasonText.toLowerCase().includes("error") || reasonText.toLowerCase().includes("paused");
                          return (
                            <span
                              key={idx}
                              className={`px-2 py-0.5 rounded text-[11px] font-mono flex items-center gap-1 border ${
                                isWarning
                                  ? "bg-rose-500/10 border-rose-500/30 text-[var(--qd-loss)]"
                                  : "bg-emerald-500/10 border-emerald-500/30 text-[var(--qd-profit)]"
                              }`}
                            >
                              {isWarning ? "⚠" : "✓"} {reasonText}
                            </span>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* Market Quote */}
                  {marketInfo && (
                    <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <span className="block font-mono text-[11px] text-[var(--qd-text-3)] uppercase tracking-wider">Spot Reference</span>
                        <span className="font-mono font-semibold text-[var(--qd-text)]">
                          {score.symbol}: {money(marketInfo.price)}
                        </span>
                      </div>
                      <div>
                        <span className="block font-mono text-[11px] text-[var(--qd-text-3)] uppercase tracking-wider">24h Change</span>
                        <span className={`font-mono font-semibold ${marketInfo.pct >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                          {marketInfo.pct >= 0 ? "+" : ""}{marketInfo.pct?.toFixed(2)}%
                        </span>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-lg p-4 text-xs text-[var(--qd-text-3)] text-center">
                  No live AI telemetry computed yet. Save runtime limits and enable the strategy to start telemetry scoring.
                </div>
              )}
            </div>
          </div>

          {/* Right Column: Spec & Exits & Relocated Backtest */}
          <div className="space-y-4">
            {/* Specs & Risk Bounds */}
            <div className="space-y-3">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold">Trading Specs & Exit Parameters</h3>
              <div className="rounded-md border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 space-y-3 text-xs">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Allocated Capital</span>
                    <strong className="block text-sm text-[var(--qd-text)] font-mono mt-0.5">{money(s.required_capital)}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Product / Class</span>
                    <strong className="block text-sm text-[var(--qd-text)] font-mono mt-0.5">{risk.product || s.product || "MIS"} / {s.asset_class?.toUpperCase() || "OPTIONS"}</strong>
                  </div>
                </div>

                {options.enabled && (
                  <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <div>
                      <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Structure</span>
                      <strong className="block text-[var(--qd-text)] font-mono mt-0.5">
                        {options.structure === "debit_spread" ? "Debit Spread" : options.structure === "credit_spread" ? "Credit Spread" : "Single Leg"}
                      </strong>
                    </div>
                    {(options.structure === "credit_spread" || options.structure === "debit_spread") && (
                      <>
                        <div>
                          <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">
                            {options.structure === "debit_spread" ? "Long Δ" : "Short Δ"}
                          </span>
                          <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{options.short_delta ?? (options.structure === "credit_spread" ? 0.3 : 0.5)}</strong>
                        </div>
                        <div>
                          <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Width (Strikes)</span>
                          <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{options.spread_width ?? 2}</strong>
                        </div>
                      </>
                    )}
                  </div>
                )}

                <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Target Profit</span>
                    <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{risk.target_pct != null ? `${risk.target_pct}%` : "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Stop Loss</span>
                    <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{risk.stoploss_pct != null ? `${risk.stoploss_pct}%` : "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Trailing SL</span>
                    <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{risk.trailing_sl_enabled ? `Yes (Trig: ${risk.trail_trigger_pct}%)` : "Disabled"}</strong>
                  </div>
                </div>

                <div className="border-t border-[var(--qd-border)] pt-2.5 grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Cooldown</span>
                    <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{risk.cooldown_minutes ? `${risk.cooldown_minutes}m` : "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Max Trades/Day</span>
                    <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{risk.max_trades_day ?? "None"}</strong>
                  </div>
                  <div>
                    <span className="text-[var(--qd-text-3)] uppercase tracking-wider font-mono text-[11px]">Daily Loss Limit</span>
                    <strong className="block text-[var(--qd-text)] font-mono mt-0.5">{risk.daily_loss_limit ? money(risk.daily_loss_limit) : "None"}</strong>
                  </div>
                </div>
              </div>
            </div>

            {/* Relocated Performance Test Section */}
            <div className="space-y-3 border-t border-[var(--qd-border)] pt-4">
              <h3 className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)] font-semibold flex items-center gap-1.5">
                <Activity size={14} className="text-[var(--qd-accent)]" /> Performance & Backtest Test
              </h3>

              <div className="rounded-md border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 space-y-3">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <p className="text-xs text-[var(--qd-text-3)] sm:max-w-[240px]">
                    Run a 60-day historical analysis on index data to verify this strategy's profitability and metrics.
                  </p>
                  <Button
                    onClick={() => testRun(s.id)}
                    disabled={testing === s.id}
                    variant="outline"
                    size="sm"
                    className="flex-shrink-0 justify-center"
                    type="button"
                  >
                    {testing === s.id ? (
                      <>
                        <RefreshCw size={13} className="animate-spin" /> Running...
                      </>
                    ) : (
                      <>
                        <Activity size={13} /> Run Performance Test
                      </>
                    )}
                  </Button>
                </div>

                {/* Backtest Test Results rendering inline */}
                {testResult && (
                  <div className="border-t border-[var(--qd-border)] pt-3 space-y-3">
                    {testResult.error ? (
                      <div className="rounded border border-[var(--qd-loss)] bg-[rgba(255,59,48,0.1)] p-3 text-xs text-[var(--qd-loss)] font-mono font-bold">
                        {testResult.error}
                      </div>
                    ) : testResult.ok ? (
                      <div className="space-y-3 text-xs">
                        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                          <Metric label="Total P&L" value={money(testResult.summary?.total_pnl)} tone={(testResult.summary?.total_pnl || 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"} compact />
                          <Metric label="Return" value={`${testResult.summary?.return_pct?.toFixed(2) || 0}%`} compact />
                          <Metric label="Win Rate" value={`${testResult.summary?.win_rate?.toFixed(1) || 0}%`} compact />
                        </div>
                        <div className="grid grid-cols-1 gap-2 border-t border-[var(--qd-border)] pt-2 sm:grid-cols-3">
                          <Metric label="Trades" value={testResult.summary?.trades || 0} compact />
                          <Metric label="Wins" value={testResult.summary?.wins || 0} tone="text-[var(--qd-profit)]" compact />
                          <Metric label="Losses" value={testResult.summary?.losses || 0} tone="text-[var(--qd-loss)]" compact />
                        </div>
                        <div className="text-[11px] text-[var(--qd-text-3)] font-mono flex justify-between">
                          <span>Data: {testResult.data_source || "-"}</span>
                          <span>Ref: {testResult.symbol_analysed || "-"}</span>
                        </div>
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
            </div>

          </div>

        </div>

      </div>
    </div>
  );
};

export default AboutStrategyModal;
