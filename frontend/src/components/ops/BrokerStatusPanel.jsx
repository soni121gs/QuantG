import React from "react";
import { Zap, CheckCircle2, Key } from "lucide-react";

const fmt = (value) => {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
  } catch {
    return String(value);
  }
};

const TelemetryMetric = ({ label, value, desc, status, isPulse }) => {
  const getStatusColor = () => {
    if (status === "success") return "text-[var(--qd-profit)]";
    if (status === "warn") return "text-[var(--qd-warn)]";
    if (status === "error") return "text-[var(--qd-loss)]";
    return "text-[var(--qd-text-3)]";
  };
  
  return (
    <div className="bg-[var(--qd-surface)] border border-[rgba(255,255,255,0.04)] rounded-md p-3">
      <div className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">{label}</div>
      <div className="flex items-center gap-1.5 mt-1">
        {isPulse && <span className="w-1.5 h-1.5 bg-[var(--qd-profit)] rounded-full animate-ping"></span>}
        <div className={`font-head text-xs sm:text-sm font-bold ${getStatusColor()}`}>{value}</div>
      </div>
      <div className="text-[9px] text-[var(--qd-text-3)] mt-0.5 font-mono">{desc}</div>
    </div>
  );
};

export default function BrokerStatusPanel({ data, busy, onUpstoxLogin }) {
  const upstox = data?.upstox || {};
  const ticker = data?.ticker || {};
  const feedState = upstox.feed_status || upstox.gateway?.feed_status || data?.ticker?.feed_status || {};
  const prefs = data?.broker_preferences || {};
  const isUpstoxActive = prefs.data_broker === "upstox" || prefs.execution_broker === "upstox";

  return (
    <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 relative overflow-hidden shadow-xl">
      {/* Top decorative gradient glow */}
      <div className="absolute top-0 left-0 w-full h-[2px] bg-gradient-to-r from-blue-500 via-indigo-500 to-purple-600"></div>
      
      <div className="flex flex-col sm:flex-row sm:items-center justify-between border-b border-[var(--qd-border)] pb-4 mb-4 gap-3">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 bg-blue-500/10 border border-blue-500/20 text-blue-400 rounded-md">
            <Zap size={18} className="animate-pulse" />
          </div>
          <div>
            <h2 className="font-head text-base font-bold text-white">Upstox API v2 HFT & Telemetry</h2>
            <p className="text-[11px] text-[var(--qd-text-3)] font-mono">High-Frequency Order Routing Engine</p>
          </div>
        </div>

        {upstox.connected ? (
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[10px] font-mono font-semibold text-[var(--qd-profit)] bg-[rgba(0,230,118,0.1)] border border-[var(--qd-profit)]/30 rounded-full">
              <CheckCircle2 size={12} /> {upstox.feed_running ? "HFT READY" : "AUTH OK / FEED STOPPED"}
            </span>
            <button 
              onClick={onUpstoxLogin}
              disabled={busy === "upstox-login"}
              className="px-3 py-1.5 bg-[var(--qd-surface-3)] hover:bg-[var(--qd-surface-2)] text-white rounded border border-[var(--qd-border)] text-xs font-mono transition-all"
            >
              Re-Auth
            </button>
          </div>
        ) : (
          <button 
            onClick={onUpstoxLogin}
            disabled={busy === "upstox-login"}
            className="px-4 py-2 bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-[var(--qd-accent-contrast)] text-xs font-bold font-mono uppercase tracking-wider rounded-md border border-[var(--qd-border-strong)] transition-all flex items-center gap-2 active:scale-95"
          >
            <Key size={14} /> Authorize Upstox
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
        <TelemetryMetric 
          label="OAuth Integration" 
          value={upstox.keys_saved ? (upstox.connected ? "Authorized" : "Ready / Need Login") : "Not configured"} 
          desc={upstox.connected ? `Last auth: ${fmt(upstox.last_auth_time)}` : (upstox.message || "Reconnect Upstox required")}
          status={upstox.connected ? "success" : upstox.keys_saved ? "warn" : "error"}
        />
        <TelemetryMetric 
          label="HFT Dispatcher" 
          value={upstox.feed_running ? "ACTIVE PULSE" : "STOPPED"} 
          desc={feedState.last_error || upstox.reason || "Websocket feed state"}
          status={upstox.feed_running ? "success" : "error"}
          isPulse={upstox.feed_running}
        />
        <TelemetryMetric 
          label="SQLite DB Engine" 
          value="Ledger Hooked" 
          desc="runtime_state.sqlite3 pool"
          status="success"
        />
        <TelemetryMetric
          label="Last Market Tick"
          value={ticker.last_tick_at ? fmt(ticker.last_tick_at) : (upstox.feed_running ? "Awaiting tick" : "-")}
          desc={`${ticker.subscribed_tokens ?? 0} tokens subscribed`}
          status={ticker.last_tick_at ? "success" : (upstox.feed_running ? "warn" : "idle")}
          isPulse={!!ticker.last_tick_at}
        />
      </div>

      {/* Performance Telemetry Detail */}
      <div className="mt-5 p-3.5 bg-[var(--qd-elevated)] border border-[var(--qd-border)] rounded-md space-y-2.5 font-mono text-xs">
        <div className="flex justify-between items-center text-[var(--qd-text-3)] border-b border-[var(--qd-border)] pb-1">
          <span>Telemetry Parameter</span>
          <span>Registry State</span>
        </div>
        <div className="flex justify-between items-center text-[var(--qd-text-2)]">
          <span>Broker Gateway Provider</span>
          <span className="text-white">Upstox API v2 REST/WS Gateway</span>
        </div>
        <div className="flex justify-between items-center text-[var(--qd-text-2)]">
          <span>API Endpoint Gateway</span>
          <span className="text-blue-400 select-all font-semibold">https://api-hft.upstox.com</span>
        </div>
        <div className="flex justify-between items-center text-[var(--qd-text-2)]">
          <span>Encrypted Access Token</span>
          <span className="text-white select-all">{upstox.token_present ? `${upstox.token_state || "present"} / ${upstox.token_valid ? "valid" : "not valid"}` : "missing"}</span>
        </div>
        <div className="flex justify-between items-center text-[var(--qd-text-2)]">
          <span>WebSocket Tick Dispatcher</span>
          <span className="text-white">{upstox.feed_running ? "running" : "stopped"}</span>
        </div>
      </div>
    </div>
  );
}
