import React, { useEffect, useState } from "react";
import { Activity, AlertTriangle, RefreshCw, ShieldAlert, Pause, Trash2, Wifi, Power, Play, SquareArrowOutUpRight } from "lucide-react";
import { api } from "../lib/api";
import { toast } from "sonner";

const fmt = (value) => {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
  } catch {
    return String(value);
  }
};

export default function OpsConsole() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState("");

  const load = async () => {
    const r = await api.get("/ops/diagnostics");
    setData(r.data);
  };

  useEffect(() => {
    load().catch(() => {});
    const t = setInterval(() => load().catch(() => {}), 5000);
    return () => clearInterval(t);
  }, []);

  const run = async (key, url, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(key);
    try {
      const r = await api.post(url, {});
      toast.success(actionMessage(key, r.data));
      await load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Action failed");
    } finally {
      setBusy("");
    }
  };

  const counts = data?.counts || {};
  const ticker = data?.ticker || {};
  const zerodha = data?.zerodha || {};
  const kotak = data?.kotak_neo || {};
  const upstox = data?.upstox || {};
  const rateLimits = data?.rate_limits || {};
  const prefs = data?.broker_preferences || {};

  return (
    <div className="space-y-4 max-w-6xl" data-testid="ops-console">
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-3">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// RECOVERY</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1 flex items-center gap-2">
            <ShieldAlert size={24} /> Ops Console
          </h1>
        </div>
        <button onClick={() => load()} className="border border-[var(--qd-border)] hover:border-white text-white px-3 py-2 text-xs font-mono uppercase rounded-sm flex items-center gap-2">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-8 gap-3">
        <Metric label="Mode" value={data?.mode || "-"} tone={data?.mode === "LIVE" ? "loss" : "warn"} />
        <Metric label="Market" value={data?.market?.status || "-"} tone={data?.market?.open ? "profit" : "warn"} />
        <Metric label="Zerodha" value={zerodha.connected ? "Connected" : zerodha.reason || "-"} tone={zerodha.connected ? "profit" : "warn"} />
        <Metric label="Kotak" value={kotak.connected ? "Ready" : kotak.keys_saved ? "Saved" : "Setup"} tone={kotak.connected ? "profit" : "warn"} />
        <Metric label="Upstox" value={upstox.connected ? "Ready" : upstox.keys_saved ? "Saved" : "Setup"} tone={upstox.connected ? "profit" : "warn"} />
        <Metric label="Ticker" value={ticker.connected ? "CONNECTED" : ticker.connecting ? "CONNECTING" : "DOWN"} tone={ticker.connected ? "profit" : "warn"} />
        <Metric label="Live Strats" value={counts.live_strategies ?? 0} />
        <Metric label="Errors" value={counts.errored_strategies ?? 0} tone={counts.errored_strategies ? "loss" : "profit"} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="qd-card p-4 space-y-3">
          <h2 className="font-head text-lg text-white flex items-center gap-2"><Power size={16} /> Live Market Actions</h2>
          <Action
            icon={AlertTriangle}
            title="Emergency Stop"
            text="Switch to paper mode and pause every live strategy."
            danger
            busy={busy === "stop"}
            onClick={() => run("stop", "/ops/emergency-stop", "Emergency stop now? This pauses all live strategies and switches to PAPER.")}
          />
          <Action
            icon={SquareArrowOutUpRight}
            title="Square Off All"
            text="Close all open positions using the current mode and broker."
            danger
            busy={busy === "squareoff"}
            onClick={() => run("squareoff", "/ops/squareoff-all", "Square off every open position now?")}
          />
          <Action
            icon={Pause}
            title="Pause All Strategies"
            text="Pause automation without changing your trading mode."
            busy={busy === "pause"}
            onClick={() => run("pause", "/ops/strategies/pause-all", "Pause all live strategies?")}
          />
          <Action
            icon={Play}
            title="Enable All Strategies"
            text="Set strategies live and re-enable option-engine gates."
            busy={busy === "enable"}
            onClick={() => run("enable", "/ops/strategies/enable-all", "Enable all strategies and clear disabled gates?")}
          />
          <Action
            icon={Wifi}
            title="Restart Zerodha Ticker"
            text="Reconnect the Kite websocket and resubscribe symbols."
            busy={busy === "ticker"}
            onClick={() => run("ticker", "/ops/ticker/restart")}
          />
          <Action
            icon={Trash2}
            title="Clear Strategy Errors"
            text="Clear old visible errors after you have fixed the cause."
            busy={busy === "clear"}
            onClick={() => run("clear", "/ops/strategies/clear-errors")}
          />
          <Action
            icon={RefreshCw}
            title="Auto Recover"
            text="Run safe fixes: order sync, ticker restart, and connected order feeds."
            busy={busy === "recover"}
            onClick={() => run("recover", "/ops/auto-recover")}
          />
          <Action
            icon={RefreshCw}
            title="Sync Broker Orders"
            text="Refresh Zerodha order statuses and repair completed local rows."
            busy={busy === "orders"}
            onClick={() => run("orders", "/ops/orders/sync")}
          />
        </div>

        <div className="qd-card p-4 space-y-3">
          <h2 className="font-head text-lg text-white flex items-center gap-2"><Activity size={16} /> Diagnostics</h2>
          <Row k="Server IST" v={fmt(data?.server_time_ist)} />
          <Row k="Ticker last connect" v={fmt(ticker.last_connect_at)} />
          <Row k="Ticker last tick" v={fmt(ticker.last_tick_at)} />
          <Row k="Subscribed tokens" v={ticker.subscribed_tokens ?? 0} />
          <Row k="Websocket URL" v={ticker.websocket_url || "-"} />
          <Row k="Ticker error" v={ticker.last_error || "-"} />
          <Row k="Zerodha expiry" v={fmt(zerodha.expires_at)} />
          <Row k="Kotak Neo" v={kotak.keys_saved ? `Configured${kotak.sdk_available ? "" : " / SDK missing"}` : "Not configured"} />
          <Row k="Upstox" v={upstox.keys_saved ? `Configured${upstox.sdk_available ? "" : " / SDK missing"}` : "Not configured"} />
          <Row k="Brokers" v={`data ${prefs.data_broker || "-"} / exec ${prefs.execution_broker || "-"} / fallback ${prefs.fallback_broker || "-"}`} />
          <Row k="History cache" v={`${rateLimits.kite_history_cache_entries ?? 0} entries`} />
          <Row k="Readiness" v={data?.readiness?.ready ? "Ready" : "Needs attention"} />
        </div>
      </div>

      <div className="qd-card">
        <div className="border-b border-[var(--qd-border)] px-4 py-3 flex items-center justify-between gap-3">
          <h2 className="font-head text-base text-white">Live Recovery Plan</h2>
          <span className={`font-mono text-xs ${data?.recovery_plan?.status === "READY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"}`}>
            {data?.recovery_plan?.status || "-"} · {data?.recovery_plan?.score ?? 0}/100
          </span>
        </div>
        {!data?.recovery_plan?.issues?.length ? (
          <div className="p-5 text-sm text-[var(--qd-profit)] font-mono">No blocking live issues detected.</div>
        ) : (
          <div className="divide-y divide-[var(--qd-border)]">
            {data.recovery_plan.issues.map((item, idx) => (
              <div key={`${item.title}-${idx}`} className="p-4 grid grid-cols-1 md:grid-cols-[110px_1fr_auto] gap-3 items-start">
                <span className={`font-mono text-[10px] uppercase ${item.severity === "critical" ? "text-[var(--qd-loss)]" : item.severity === "warning" ? "text-[var(--qd-warn)]" : "text-[var(--qd-text-3)]"}`}>{item.severity}</span>
                <div>
                  <div className="text-white font-semibold">{item.title}</div>
                  <div className="text-xs text-[var(--qd-text-2)] mt-1">{item.detail}</div>
                  <div className="text-xs font-mono text-[var(--qd-accent)] mt-2">{item.action}</div>
                </div>
                {item.endpoint && (
                  <button onClick={() => run(`plan-${idx}`, item.endpoint)} className="border border-[var(--qd-border)] hover:border-white px-3 py-2 rounded-sm text-xs font-mono uppercase text-white">
                    Fix
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="qd-card">
        <div className="border-b border-[var(--qd-border)] px-4 py-3">
          <h2 className="font-head text-base text-white">Strategy Errors</h2>
        </div>
        {!data?.errored_strategies?.length ? (
          <div className="p-6 text-sm text-[var(--qd-text-2)] font-mono">No strategy errors currently reported.</div>
        ) : (
          <div className="qd-table-wrap">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                  <th className="px-4 py-2">Strategy</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2">Data</th>
                  <th className="px-4 py-2">Error</th>
                </tr>
              </thead>
              <tbody>
                {data.errored_strategies.map((s) => (
                  <tr key={s.id} className="border-t border-[var(--qd-border)]">
                    <td className="px-4 py-2 text-white">{s.name}</td>
                    <td className="px-4 py-2 font-mono text-[var(--qd-text-2)]">{s.status}</td>
                    <td className="px-4 py-2 font-mono text-[var(--qd-text-2)]">{s.last_data_source || "-"}</td>
                    <td className="px-4 py-2 text-[var(--qd-warn)] text-xs">{s.last_error}</td>
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

function actionMessage(key, data) {
  if (key === "stop") return `Emergency stop done. Paused ${data.paused_strategies || 0} strategies.`;
  if (key === "pause") return `Paused ${data.paused_strategies || 0} strategies.`;
  if (key === "enable") return `Enabled ${data.enabled_strategies || 0} strategies.`;
  if (key === "ticker") return data.started ? `Ticker restart requested for ${data.tokens || 0} symbols.` : `Ticker not started: ${data.reason || "unknown"}`;
  if (key === "clear") return `Cleared errors on ${data.updated_strategies || 0} strategies.`;
  if (key === "orders") return data.ok ? `Synced ${data.sync?.checked || 0} broker orders, fixed ${data.stale?.fixed || 0}.` : `Order sync skipped: ${data.reason || "unknown"}`;
  if (key === "recover") return `Recovery ran ${data.actions?.length || 0} safe checks.`;
  if (String(key).startsWith("plan-")) return "Recovery action complete.";
  if (key === "squareoff") return `Square-off sent: ${data.closed?.length || 0} closed, ${data.failed?.length || 0} failed.`;
  return "Done";
}

const Metric = ({ label, value, tone }) => (
  <div className="qd-card p-3">
    <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className={`font-head text-xl mt-1 ${tone === "profit" ? "text-[var(--qd-profit)]" : tone === "loss" ? "text-[var(--qd-loss)]" : tone === "warn" ? "text-[var(--qd-warn)]" : "text-white"}`}>{value}</div>
  </div>
);

const Row = ({ k, v }) => (
  <div className="flex justify-between gap-4 border-b border-[var(--qd-border)] pb-2">
    <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{k}</span>
    <span className="text-xs text-white text-right break-all">{v}</span>
  </div>
);

const Action = ({ icon: Icon, title, text, onClick, busy, danger }) => (
  <button
    onClick={onClick}
    disabled={busy}
    className={`w-full text-left border rounded-sm p-3 flex items-center gap-3 transition-colors disabled:opacity-60 ${
      danger
        ? "border-[var(--qd-loss)] text-[var(--qd-loss)] hover:bg-[rgba(255,59,48,0.08)]"
        : "border-[var(--qd-border)] text-white hover:border-white hover:bg-[var(--qd-surface-2)]"
    }`}
  >
    <Icon size={18} />
    <span className="flex-1">
      <span className="block text-sm font-semibold">{busy ? "Working..." : title}</span>
      <span className="block text-xs text-[var(--qd-text-2)] mt-0.5">{text}</span>
    </span>
  </button>
);
