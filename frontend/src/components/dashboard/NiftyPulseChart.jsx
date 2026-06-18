import React from "react";
import { formatINR } from "../../lib/api";

const buildSparkPath = (series, w, h, pad = 6) => {
  if (!series || series.length < 2) return "";
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min || 1;
  const innerW = w - pad * 2;
  const innerH = h - pad * 2;
  const step = innerW / (series.length - 1);
  return series
    .map((v, i) => {
      const x = pad + i * step;
      const y = pad + innerH - ((v - min) / span) * innerH;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
};

export const NiftyPulseChart = ({ niftyLtp, sensexLtp, candles = [], marketOpen, label, feedLabel }) => {
  const series = (candles || []).map((c) => Number(c.close ?? c.c)).filter((n) => Number.isFinite(n));
  const hasChart = series.length >= 2;
  const first = series[0];
  const last = series[series.length - 1];
  const liveLtp = Number(niftyLtp) || last || 0;
  const change = hasChart ? last - first : 0;
  const pct = hasChart && first ? (change / first) * 100 : 0;
  const positive = change >= 0;
  const W = 360;
  const H = 96;
  const PAD = 6;
  const line = buildSparkPath(series, W, H, PAD);
  const step = series.length > 1 ? (W - PAD * 2) / (series.length - 1) : 0;
  const lastX = PAD + (series.length - 1) * step;
  const area = hasChart ? `${line} L${lastX.toFixed(1)} ${H} L${PAD} ${H} Z` : "";

  return (
    <div className="qd-market-pulse" data-testid="nifty-pulse">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="qd-section-title">NIFTY 50</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="font-mono text-2xl font-bold tracking-tight text-[var(--qd-text)]">{liveLtp ? formatINR(liveLtp) : "—"}</span>
            {hasChart && (
              <span className={`font-mono text-xs font-bold ${positive ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                {positive ? "▲" : "▼"} {Math.abs(pct).toFixed(2)}%
              </span>
            )}
          </div>
        </div>
        <span className={`qd-pulse-status ${marketOpen ? "is-open" : "is-closed"}`}>{marketOpen ? "Live" : "Closed"}</span>
      </div>

      {hasChart ? (
        <svg viewBox={`0 0 ${W} ${H}`} className="mt-3 h-24 w-full" preserveAspectRatio="none">
          <defs>
            <linearGradient id="niftyPulseLine" x1="0" x2="1" y1="0" y2="0">
              <stop offset="0%" stopColor="var(--qd-accent)" />
              <stop offset="100%" stopColor={positive ? "var(--qd-profit)" : "var(--qd-loss)"} />
            </linearGradient>
          </defs>
          <path d={area} fill="var(--qd-accent)" opacity="0.08" />
          <path d={line} fill="none" stroke="url(#niftyPulseLine)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        </svg>
      ) : (
        <div className="mt-3 flex h-24 items-center justify-center rounded-[var(--qd-radius-sm)] border border-dashed border-[var(--qd-border)] px-3 text-center text-[11px] text-[var(--qd-text-3)]">
          {marketOpen ? "Waiting for NIFTY candles…" : "Intraday chart unavailable (market closed or analytics token not set)"}
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 gap-3 border-t border-[var(--qd-border)] pt-3">
        <div>
          <div className="qd-section-title text-[9px]">SENSEX</div>
          <div className="mt-1 font-mono text-sm font-semibold text-[var(--qd-text)]">{sensexLtp ? formatINR(sensexLtp) : "—"}</div>
        </div>
        <div className="text-right">
          <div className="qd-section-title text-[9px]">Feed</div>
          <div className="mt-1 truncate font-mono text-xs text-[var(--qd-text-2)]" title={feedLabel || label}>{feedLabel || label || "—"}</div>
        </div>
      </div>
    </div>
  );
};

export default NiftyPulseChart;
