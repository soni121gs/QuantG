import React from "react";
import { Play, Pause, Shield } from "lucide-react";
import { formatINR } from "../../lib/api";

const money = (value) => `INR ${formatINR(value ?? 0)}`;
const toneClass = (value) => ((value ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]");
const asStatus = (value) => String(value || "").toUpperCase();
const hasQty = (value) => Math.abs(parseInt(value || 0, 10)) > 0;

const shortId = (value) => {
  const text = String(value || "");
  return text.length > 10 ? `${text.slice(0, 6)}...${text.slice(-4)}` : text;
};

const PENDING_POSITION_STATES = ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "EXITING"];
const PROBLEM_ORDER_STATES = ["FAILED", "REJECTED", "BROKER_NOT_FOUND", "STALE"];
const BROKER_OPEN_ORDER_STATES = [
  "NEW", "PLACED", "OPEN", "PARTIAL_FILL", "PENDING", "PENDING_BROKER", 
  "TRIGGER PENDING", "MODIFY PENDING", "VALIDATION PENDING", "EXIT_PENDING"
];

export const Field = ({ label, value, tone }) => (
  <div className="min-w-0">
    <div className="qd-section-title text-[10px]">{label}</div>
    <div className={`mt-0.5 truncate font-mono text-xs font-semibold ${tone || "text-white"}`}>{value}</div>
  </div>
);

export const StatusPill = ({ children, tone = "neutral" }) => {
  const tones = {
    good: "border-[rgba(0,230,118,0.38)] bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]",
    bad: "border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.1)] text-[var(--qd-loss)]",
    warn: "border-[rgba(255,159,10,0.4)] bg-[rgba(255,159,10,0.1)] text-[var(--qd-warn)]",
    neutral: "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]",
  };
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase ${tones[tone]}`}>
      {children}
    </span>
  );
};

export const QualityPill = ({ score, readiness }) => {
  const n = Number(score || 0);
  const tone = readiness === "PASS" || n >= 70
    ? "border-[rgba(0,230,118,0.38)] bg-[rgba(0,230,118,0.1)] text-[var(--qd-profit)]"
    : readiness === "BLOCK" || n < 45
      ? "border-[rgba(255,59,48,0.42)] bg-[rgba(255,59,48,0.1)] text-[var(--qd-loss)]"
      : "border-[rgba(255,209,102,0.38)] bg-[rgba(255,209,102,0.1)] text-[var(--qd-warn)]";
  return <span className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold ${tone}`}>Q {n}</span>;
};

export const StrategyLedgerRow = ({ row, onToggle, onExit }) => {
  const position = row.position;
  const pendingOrder = row.pendingOrder;
  const failedOrder = row.failedOrder;
  const quality = pendingOrder?.quality_score ?? failedOrder?.quality_score ?? position?.quality_score;
  const qualityReadiness = pendingOrder?.quality_readiness ?? failedOrder?.quality_readiness ?? position?.quality_readiness;
  const status = position?.execution_status || position?.ledger_status || (pendingOrder ? pendingOrder.status : row.state || "FLAT");
  const positionOpen = position && hasQty(position.qty);
  const problem = failedOrder || asStatus(position?.execution_status) === "BROKER_NOT_FOUND";
  const warning = !problem && (pendingOrder || PENDING_POSITION_STATES.includes(asStatus(status)));
  const tone = problem ? "bad" : warning ? "warn" : positionOpen ? "good" : row.status === "live" ? "neutral" : "neutral";
  const pnl = position?.pnl ?? row.active_position?.unrealized_pnl ?? row.daily_pnl?.realized_pnl ?? 0;
  const slMissing = positionOpen && position?.stop_loss == null;
  const tpMissing = positionOpen && position?.take_profit == null;
  const live = row.status === "live";
  const idleLabel = live ? (row.state === "SCANNING" ? "Scanning" : row.state || "Live") : "Flat";
  const telemetry = row.telemetry || {};
  const detail = problem
    ? failedOrder?.status_message || telemetry.last_error
    : pendingOrder
      ? pendingOrder.status_message || "Waiting for broker order book sync"
      : telemetry.last_error || telemetry.last_filter_reason || telemetry.last_data_reason || telemetry.last_data_source;

  return (
    <div className="grid gap-2 border-t border-[var(--qd-border)] px-3 py-2 lg:grid-cols-[minmax(200px,1.1fr)_minmax(240px,1.35fr)_minmax(170px,0.9fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="truncate text-xs font-semibold text-white">{row.name}</div>
        <div className="mt-0.5 font-mono text-[10px] uppercase text-[var(--qd-text-3)]">{shortId(row.strategy_id)}</div>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Field label="Position" value={positionOpen ? position.symbol : "Flat"} tone={positionOpen ? "text-white" : "text-[var(--qd-text-3)]"} />
        <Field label="Qty" value={positionOpen ? position.qty : "0"} tone={positionOpen ? "text-[var(--qd-profit)]" : "text-[var(--qd-text-3)]"} />
        <Field label="SL" value={position?.stop_loss != null ? money(position.stop_loss) : "-"} tone={slMissing ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"} />
        <Field label="TP" value={position?.take_profit != null ? money(position.take_profit) : "-"} tone={tpMissing ? "text-[var(--qd-warn)]" : "text-[var(--qd-text-2)]"} />
      </div>

      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={tone}>{positionOpen ? status : pendingOrder ? "Order pending" : problem ? "Needs check" : idleLabel}</StatusPill>
          {quality != null && <QualityPill score={quality} readiness={qualityReadiness} />}
          {slMissing && <StatusPill tone="bad">No SL</StatusPill>}
          {pendingOrder && <StatusPill tone="warn">Broker sync</StatusPill>}
        </div>
        <div className={`mt-1 font-mono text-[11px] font-semibold ${toneClass(pnl)}`}>{money(pnl)}</div>
        {detail && <div className="mt-0.5 max-w-[260px] truncate text-[10px] text-[var(--qd-text-3)]" title={detail}>{detail}</div>}
      </div>

      <div className="flex items-center justify-end gap-2">
        <button
          onClick={() => onToggle(row.strategy_id)}
          className={`flex h-8 w-8 items-center justify-center rounded border transition-all ${
            live
              ? "border-[rgba(255,159,10,0.4)] text-[var(--qd-warn)] hover:bg-[rgba(255,159,10,0.1)]"
              : "border-[rgba(0,230,118,0.4)] text-[var(--qd-profit)] hover:bg-[rgba(0,230,118,0.1)]"
          }`}
          title={live ? "Pause Strategy" : "Resume Strategy"}
          data-testid={`dashboard-toggle-${row.strategy_id}`}
        >
          {live ? <Pause size={13} /> : <Play size={13} />}
        </button>
        <button
          onClick={() => onExit(row.strategy_id)}
          disabled={!positionOpen}
          className="flex h-8 w-8 items-center justify-center rounded border border-[var(--qd-warn)] text-[var(--qd-warn)] transition-all hover:bg-[var(--qd-warn)] hover:text-black disabled:cursor-not-allowed disabled:opacity-35"
          title={positionOpen ? "Square Off Strategy Positions" : "No open position for this strategy"}
          data-testid={`dashboard-exit-${row.strategy_id}`}
        >
          <Shield size={13} />
        </button>
      </div>
    </div>
  );
};

export default StrategyLedgerRow;
