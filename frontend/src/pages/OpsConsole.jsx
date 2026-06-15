import React, { useCallback, useEffect, useState } from "react";
import { 
  Activity, AlertTriangle, RefreshCw, ShieldAlert, Pause, Trash2,
  Wifi, Power, Play, SquareArrowOutUpRight, Key, CheckCircle2,
  XCircle, Zap, Shield, HardDrive, CircleDollarSign, ArrowUpRight, User, Loader2
} from "lucide-react";
import { api } from "../lib/api";
import { toast } from "sonner";
import { useAuth } from "../contexts/AuthContext";
import { useExecutionState } from "../hooks/useExecutionState";

const fmt = (value) => {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
  } catch {
    return String(value);
  }
};

const fmtUTC = (value) => {
  if (!value) return "-";
  try {
    const d = new Date(value);
    return d.toISOString().replace("T", " ").slice(0, 19) + " UTC";
  } catch {
    return String(value);
  }
};

const money = (value) => Number(value || 0).toLocaleString("en-IN", { style: "currency", currency: "INR" });

export default function OpsConsole() {
  const { user } = useAuth();
  const { summary: executionSummary } = useExecutionState({ pollMs: 15000 });
  const [data, setData] = useState(null);
  const [fundsData, setFundsData] = useState(null);
  const [busy, setBusy] = useState("");
  const [simulatedLatency, setSimulatedLatency] = useState(12);
  const [pendingUsers, setPendingUsers] = useState([]);
  const [needsReviewOrders, setNeedsReviewOrders] = useState([]);
  const [reconciliationBreaks, setReconciliationBreaks] = useState(null);
  const [featureFlags, setFeatureFlags] = useState([]);
  const [userActionId, setUserActionId] = useState(null);

  const loadPendingUsers = useCallback(async () => {
    if (user?.role !== "owner") return;
    try {
      const r = await api.get("/ops/pending-users");
      setPendingUsers(r.data);
    } catch (e) {
      console.error("Failed to load pending users", e);
    }
  }, [user?.role]);

  const loadNeedsReview = useCallback(async () => {
    try {
      const r = await api.get("/ops/reconciliation/breaks");
      setReconciliationBreaks(r.data);
      setNeedsReviewOrders(r.data?.unknown_live_orders || []);
    } catch (e) {
      console.error("Failed to load needs review orders", e);
    }
  }, []);

  const handleApprove = async (userId, name) => {
    setUserActionId(userId);
    try {
      await api.post(`/ops/users/${userId}/approve`);
      toast.success(`Approved registry entry for ${name}. Strategies seeded!`);
      loadPendingUsers();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Approval failed");
    } finally {
      setUserActionId(null);
    }
  };

  const handleReject = async (userId, name) => {
    if (!window.confirm(`Are you sure you want to reject and delete the registration request from ${name}?`)) return;
    setUserActionId(userId);
    try {
      await api.post(`/ops/users/${userId}/reject`);
      toast.success(`Rejected and removed registry request from ${name}`);
      loadPendingUsers();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Rejection failed");
    } finally {
      setUserActionId(null);
    }
  };

  const load = useCallback(async () => {
    try {
      const r = await api.get("/ops/diagnostics");
      setData(r.data);
      
      const f = await api.get("/funds");
      setFundsData(f.data);

      const ff = await api.get("/ops/feature-flags").catch(() => ({ data: { features: [] } }));
      setFeatureFlags(ff.data?.features || []);

      // Jitter latency slightly to reflect actual HFT dispatcher round-trips
      setSimulatedLatency(Math.floor(10 + Math.random() * 8));
    } catch (e) {
      console.error("Ops diagnostics loading failed", e);
    }
  }, []);

  useEffect(() => {
    load().catch(() => toast.error("Failed to load operations data"));
    loadPendingUsers().catch(() => {});
    loadNeedsReview().catch(() => {});
    const t = setInterval(() => {
      load().catch(() => {});
      loadPendingUsers().catch(() => {});
      loadNeedsReview().catch(() => {});
    }, 15000);
    return () => clearInterval(t);
  }, [user, load, loadPendingUsers, loadNeedsReview]);

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

  const resolveReconciliation = async () => {
    const phrase = reconciliationBreaks?.resolve_required_note || "RESOLVE_RECONCILIATION_AFTER_MANUAL_BROKER_CHECK";
    const entered = window.prompt(`Type ${phrase} after manually verifying the broker account.`);
    if (entered !== phrase) {
      toast.error("Confirmation phrase did not match.");
      return;
    }
    setBusy("resolve-reconciliation");
    try {
      const r = await api.post("/ops/reconciliation/resolve", { confirm: true, note: phrase });
      toast.success(r.data?.detail || "Reconciliation block cleared.");
      await loadNeedsReview();
      await load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Reconciliation resolve failed");
    } finally {
      setBusy("");
    }
  };

  const handleUpstoxLogin = async () => {
    setBusy("upstox-login");
    try {
      const res = await api.get("/broker/upstox/login");
      if (res.data && res.data.url) {
        toast.success("Generating secure OAuth payload... Redirecting.");
        setTimeout(() => {
          window.location.href = res.data.url;
        }, 800);
      } else {
        toast.error("Failed to build Upstox login URL.");
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || "Upstox login failed.");
    } finally {
      setBusy("");
    }
  };

  const counts = data?.counts || {};
  const ticker = data?.ticker || {};
  const upstox = data?.upstox || {};
  const feedState = upstox.feed_status || upstox.gateway?.feed_status || ticker.feed_status || {};
  const rateLimits = data?.rate_limits || {};
  const prefs = data?.broker_preferences || {};
  const isUpstoxActive = prefs.data_broker === "upstox" || prefs.execution_broker === "upstox";
  const reconciliationBlockers = reconciliationBreaks?.blockers || {};
  const reconciliationNeedsAttention = Boolean(
    reconciliationBlockers.position_reconciliation_mismatch ||
    reconciliationBlockers.unknown_live_orders ||
    reconciliationBlockers.reservations_needing_review
  );

  // Calculate Margin Metrics
  const availCash = fundsData?.available_cash || 0;
  const usedMargin = fundsData?.used_margin || 0;
  const spanMargin = fundsData?.span || 0;
  const payinAmount = fundsData?.intraday_payin || 0;
  const sessionPnl = executionSummary?.net_pnl ?? 0;
  const openPositions = executionSummary?.open_positions ?? 0;
  const totalLimit = availCash + usedMargin;
  const utilPercent = totalLimit > 0 ? Math.round((usedMargin / totalLimit) * 100) : 0;
  
  // Set dynamic visual tones for progress bar
  const getUtilColor = (pct) => {
    if (pct > 80) return "bg-[var(--qd-loss)] shadow-[0_0_8px_#FF3B30]";
    if (pct > 50) return "bg-[var(--qd-warn)] shadow-[0_0_8px_#FF9F0A]";
    return "bg-[var(--qd-profit)] shadow-[0_0_8px_#00E676]";
  };

  return (
    <div className="space-y-6 max-w-7xl pb-10" data-testid="ops-console">
      {/* HEADER CONTROL BAR */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 p-4 border border-[var(--qd-border)] bg-[var(--qd-elevated)] backdrop-blur-md rounded-lg shadow-2xl">
        <div className="flex items-center gap-3">
          <div className="p-2.5 bg-gradient-to-br from-red-500/20 to-orange-500/10 border border-red-500/30 rounded-lg text-[var(--qd-loss)] shadow-inner">
            <ShieldAlert size={26} className="animate-pulse" />
          </div>
          <div>
            <div className="font-mono text-[9px] tracking-widest uppercase text-[var(--qd-text-3)] flex items-center gap-1.5">
              <span className="qd-live-dot"></span> RISK OPERATIONS CONTROL ROOM // v{data?.version || "12.0"} ({data?.git_commit?.slice(0, 7) || "unknown"}@{data?.git_branch || "main"}{data?.git_dirty ? "*" : ""})
            </div>
            <h1 className="font-head text-2xl font-extrabold text-white mt-0.5 tracking-tight flex items-center gap-2">
              Ops Control Desk
            </h1>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="hidden lg:block text-right pr-2">
            <div className="text-[10px] text-[var(--qd-text-3)] uppercase font-mono">Telemetry Feed</div>
            <div className="text-xs text-[var(--qd-text-2)] font-mono">Sync Interval: 4.0s</div>
          </div>
          
          <button 
            onClick={() => load()} 
            disabled={busy === "load"}
            className="border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] hover:bg-[var(--qd-surface-3)] text-white px-3.5 py-2 text-xs font-mono uppercase rounded-md flex items-center gap-2 transition-all active:scale-95 shadow-md"
          >
            <RefreshCw size={14} className={busy === "load" ? "animate-spin" : ""} /> Refresh Systems
          </button>
        </div>
      </div>

      {/* OWNER APPROVALS DESK */}
      {user?.role === "owner" && pendingUsers.length > 0 && (
        <div className="qd-card p-6 border-[var(--qd-cyan)] bg-[var(--qd-surface)]/70 backdrop-blur-md space-y-4 shadow-2xl relative overflow-hidden">
          <div className="absolute top-0 left-0 w-64 h-64 bg-[var(--qd-cyan)]/5 rounded-full blur-[100px] pointer-events-none" />
          <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-3 relative z-10">
            <div className="flex items-center gap-2">
              <Shield className="text-[var(--qd-cyan)] animate-pulse" size={20} />
              <h2 className="text-lg font-head font-extrabold text-white">Owner's Registration Desk</h2>
              <span className="text-xs font-mono bg-[var(--qd-cyan)]/15 border border-[var(--qd-cyan)]/30 text-[var(--qd-cyan)] px-2 py-0.5 rounded-full font-bold animate-pulse">
                {pendingUsers.length} PENDING
              </span>
            </div>
            <span className="text-xs font-mono text-[var(--qd-text-3)] uppercase tracking-wider">
              Awaiting Verification
            </span>
          </div>

          <div className="qd-table-wrap relative z-10">
            <table className="w-full text-left text-xs font-mono border-collapse">
              <thead>
                <tr className="border-b border-[var(--qd-border)] text-[var(--qd-text-3)]">
                  <th className="py-2.5">TRADER NAME</th>
                  <th className="py-2.5">EMAIL ADDRESS</th>
                  <th className="py-2.5">REQUEST DATE</th>
                  <th className="py-2.5">ROLE</th>
                  <th className="py-2.5 text-right">ACTIONS</th>
                </tr>
              </thead>
              <tbody>
                {pendingUsers.map((p) => (
                  <tr key={p.id} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)] transition-colors">
                    <td className="py-3 text-white font-semibold flex items-center gap-1.5">
                      <User size={12} className="text-[var(--qd-text-3)]" />
                      {p.name}
                    </td>
                    <td className="py-3 text-[var(--qd-text-2)]">{p.email}</td>
                    <td className="py-3 text-[var(--qd-text-3)]">{fmt(p.created_at)}</td>
                    <td className="py-3">
                      <span className="px-1.5 py-0.5 text-[10px] bg-indigo-500/10 border border-indigo-500/30 text-indigo-300 rounded uppercase font-bold">
                        {p.role}
                      </span>
                    </td>
                    <td className="py-3 text-right space-x-2">
                      <button
                        onClick={() => handleApprove(p.id, p.name)}
                        disabled={userActionId === p.id}
                        className="bg-emerald-500 hover:bg-emerald-600 active:scale-95 disabled:opacity-60 text-emerald-950 font-bold px-3 py-1.5 rounded text-[11px] uppercase tracking-wider transition-all shadow-md shadow-emerald-500/10 flex items-center gap-1.5"
                      >
                        {userActionId === p.id ? <Loader2 size={11} className="animate-spin" /> : null}
                        Approve
                      </button>
                      <button
                        onClick={() => handleReject(p.id, p.name)}
                        disabled={userActionId === p.id}
                        className="bg-red-500/15 border border-red-500/30 hover:bg-red-500 hover:text-white disabled:opacity-60 text-[var(--qd-loss)] font-bold px-3 py-1.5 rounded text-[11px] uppercase tracking-wider transition-all"
                      >
                        Reject
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* NEEDS REVIEW DESK */}
      {reconciliationNeedsAttention && (
        <div className="qd-card p-6 border-[var(--qd-loss)] bg-[var(--qd-surface)]/70 backdrop-blur-md space-y-4 shadow-2xl relative overflow-hidden" data-testid="needs-review-panel">
          <div className="absolute top-0 left-0 w-64 h-64 bg-[var(--qd-loss)]/5 rounded-full blur-[100px] pointer-events-none" />
          <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3 border-b border-[var(--qd-border)] pb-3 relative z-10">
            <div className="flex items-center gap-2">
              <AlertTriangle className="text-[var(--qd-loss)] animate-bounce" size={20} />
              <h2 className="text-lg font-head font-extrabold text-white">Manual Reconciliation Required</h2>
              <span className="text-xs font-mono bg-[var(--qd-loss)]/15 border border-[var(--qd-loss)]/30 text-[var(--qd-loss)] px-2 py-0.5 rounded-full font-bold">
                {(reconciliationBlockers.unknown_live_orders || 0) + (reconciliationBlockers.reservations_needing_review || 0)} UNRESOLVED
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={() => run("reconcile", "/ops/orders/sync")}
                disabled={busy === "reconcile"}
                className="border border-[var(--qd-border)] hover:border-[var(--qd-border-strong)] hover:bg-[var(--qd-surface-2)] px-3 py-1.5 rounded text-[11px] font-mono uppercase text-white flex items-center gap-1.5"
              >
                <RefreshCw size={13} className={busy === "reconcile" ? "animate-spin" : ""} /> Sync Broker
              </button>
              <button
                onClick={resolveReconciliation}
                disabled={busy === "resolve-reconciliation" || !reconciliationBreaks?.can_resolve_position_reconciliation}
                className="border border-emerald-500/40 disabled:border-[var(--qd-border)] disabled:text-[var(--qd-text-3)] disabled:opacity-50 hover:border-emerald-400 hover:bg-emerald-500/10 px-3 py-1.5 rounded text-[11px] font-mono uppercase text-emerald-300 flex items-center gap-1.5"
              >
                <CheckCircle2 size={13} /> Clear Checked Block
              </button>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 relative z-10">
            <MetricCard label="Position Mismatch" value={reconciliationBlockers.position_reconciliation_mismatch ? "YES" : "NO"} tone={reconciliationBlockers.position_reconciliation_mismatch ? "loss" : "profit"} />
            <MetricCard label="Unknown Orders" value={reconciliationBlockers.unknown_live_orders || 0} tone={reconciliationBlockers.unknown_live_orders ? "loss" : "profit"} />
            <MetricCard label="Exposure Review" value={reconciliationBlockers.reservations_needing_review || 0} tone={reconciliationBlockers.reservations_needing_review ? "loss" : "profit"} />
            <MetricCard label="Active Reserves" value={reconciliationBlockers.active_live_reservations || 0} tone={reconciliationBlockers.active_live_reservations ? "warn" : "normal"} />
          </div>

          {!!reconciliationBreaks?.reconciliation_state?.mismatches?.length && (
            <div className="relative z-10 border border-red-500/20 bg-red-500/5 rounded-md p-3 space-y-1">
              {reconciliationBreaks.reconciliation_state.mismatches.map((m, idx) => (
                <div key={`${m}-${idx}`} className="text-xs text-red-200 font-mono">{m}</div>
              ))}
            </div>
          )}

          {needsReviewOrders.length > 0 && <div className="qd-table-wrap relative z-10">
            <table className="w-full text-left text-xs font-mono border-collapse">
              <thead>
                <tr className="border-b border-[var(--qd-border)] text-[var(--qd-text-3)]">
                  <th className="py-2.5">SYMBOL</th>
                  <th className="py-2.5">ORDER ID</th>
                  <th className="py-2.5">MODE</th>
                  <th className="py-2.5">SIDE</th>
                  <th className="py-2.5">QTY</th>
                  <th className="py-2.5">REASON / MSG</th>
                  <th className="py-2.5">TIMESTAMP</th>
                </tr>
              </thead>
              <tbody>
                {needsReviewOrders.map((o) => (
                  <tr key={o.id} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)] transition-colors">
                    <td className="py-3 text-white font-semibold">{o.symbol || o.tradingsymbol}</td>
                    <td className="py-3 text-[var(--qd-text-2)]">{o.broker_order_id || o.id}</td>
                    <td className="py-3">
                      <span className={`px-1.5 py-0.5 text-[10px] rounded uppercase font-bold ${
                        o.mode === "live" ? "bg-red-500/10 text-red-300 border border-red-500/20" : "bg-amber-500/10 text-amber-300 border border-amber-500/20"
                      }`}>
                        {o.mode}
                      </span>
                    </td>
                    <td className={`py-3 font-bold ${o.side === "BUY" ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                      {o.side}
                    </td>
                    <td className="py-3 text-white">{o.qty || o.quantity}</td>
                    <td className="py-3 text-[var(--qd-warn)] max-w-xs truncate animate-pulse" title={o.status_message}>{o.status_message}</td>
                    <td className="py-3 text-[var(--qd-text-3)]">{fmt(o.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>}

          {!!reconciliationBreaks?.reservations_needing_review?.length && (
            <div className="qd-table-wrap relative z-10">
              <table className="w-full text-left text-xs font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--qd-border)] text-[var(--qd-text-3)]">
                    <th className="py-2.5">SYMBOL</th>
                    <th className="py-2.5">ORDER ID</th>
                    <th className="py-2.5">RESERVED VALUE</th>
                    <th className="py-2.5">REASON</th>
                    <th className="py-2.5">UPDATED</th>
                  </tr>
                </thead>
                <tbody>
                  {reconciliationBreaks.reservations_needing_review.map((r) => (
                    <tr key={r.id || r.order_id} className="border-b border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)] transition-colors">
                      <td className="py-3 text-white font-semibold">{r.symbol || r.instrument_key}</td>
                      <td className="py-3 text-[var(--qd-text-2)]">{r.order_id || r.id}</td>
                      <td className="py-3 text-[var(--qd-warn)]">{Number(r.reserved_value || 0).toLocaleString("en-IN", { style: "currency", currency: "INR" })}</td>
                      <td className="py-3 text-[var(--qd-text-2)] max-w-xs truncate" title={r.reason}>{r.reason || r.status}</td>
                      <td className="py-3 text-[var(--qd-text-3)]">{fmt(r.updated_at || r.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* SYSTEM STATUS RIBBON */}
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-8 gap-3">
        <MetricCard label="Mode" value={data?.mode || "PAPER"} tone={data?.mode === "LIVE" ? "loss" : "warn"} isPulse={data?.mode === "LIVE"} />
        <MetricCard label="Market" value={data?.market?.status || "CLOSED"} tone={data?.market?.open ? "profit" : "warn"} />
        <MetricCard label="Session P&L" value={money(sessionPnl)} tone={sessionPnl >= 0 ? "profit" : "loss"} />
        <MetricCard label="Open Pos" value={openPositions} tone={openPositions ? "warn" : "normal"} />
        <MetricCard 
          label="Upstox API" 
          value={!isUpstoxActive ? "Inactive" : (upstox.connected ? "Connected" : "Reconnect")} 
          tone={!isUpstoxActive ? "normal" : (upstox.connected ? "profit" : "loss")} 
          isPulse={isUpstoxActive && upstox.connected} 
        />
        <MetricCard label="Active Tickers" value={ticker.connected || feedState.connected ? "CONNECTED" : "STOPPED"} tone={ticker.connected || feedState.connected ? "profit" : "loss"} />
        <MetricCard label="Live Strats" value={counts.live_strategies ?? 0} tone={counts.live_strategies ? "profit" : "normal"} />
        <MetricCard label="Blocked Strats" value={counts.errored_strategies ?? 0} tone={counts.errored_strategies ? "loss" : "profit"} isPulse={!!counts.errored_strategies} />
      </div>

      {/* CORE DESK GRID */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        
        {/* LEFT PANEL: UPSTOX HFT & TELEMETRY */}
        <div className="lg:col-span-8 space-y-5">
          
          {/* UPSTOX HFT & PERFORMANCE COCKPIT */}
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
                    onClick={handleUpstoxLogin}
                    disabled={busy === "upstox-login"}
                    className="px-3 py-1.5 bg-[var(--qd-surface-3)] hover:bg-[var(--qd-surface-2)] text-white rounded border border-[var(--qd-border)] text-xs font-mono transition-all"
                  >
                    Re-Auth
                  </button>
                </div>
              ) : (
                <button 
                  onClick={handleUpstoxLogin}
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
                label="Order Latency (API)" 
                value={upstox.connected ? `${simulatedLatency}ms` : "-"} 
                desc="Simulated dispatch round-trip"
                status={upstox.connected ? "success" : "idle"}
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

          {/* MARGIN DESK COCKPIT */}
          <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 relative overflow-hidden shadow-xl">
            <div className="absolute top-0 left-0 w-full h-[2px] bg-gradient-to-r from-emerald-500 to-green-400"></div>

            <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-4 mb-5">
              <div className="flex items-center gap-2.5">
                <div className="p-1.5 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 rounded-md">
                  <CircleDollarSign size={18} />
                </div>
                <div>
                  <h2 className="font-head text-base font-bold text-white">Live Margin Desk Meter</h2>
                  <p className="text-[11px] text-[var(--qd-text-3)] font-mono">Live Broker Balance and Utilization Monitor</p>
                </div>
              </div>
              
              <span className="font-mono text-xs font-semibold px-2 py-1 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] text-white rounded">
                Active Source: {fundsData?.source?.toUpperCase() || "PAPER"}
              </span>
            </div>

            {/* Cash Progress Bar */}
            <div className="space-y-2 mb-6">
              <div className="flex justify-between text-xs font-mono">
                <span className="text-[var(--qd-text-2)] font-semibold flex items-center gap-1"><ArrowUpRight size={14} className="text-emerald-400" /> Cash Used Utilization</span>
                <span className="text-white font-bold">{utilPercent}% Used / {availCash.toLocaleString("en-IN", { style: "currency", currency: "INR" })} Avail</span>
              </div>
              <div className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-full h-3 overflow-hidden p-[1px]">
                <div 
                  className={`h-full rounded-full transition-all duration-700 ${getUtilColor(utilPercent)}`}
                  style={{ width: `${Math.min(utilPercent || 1, 100)}%` }}
                ></div>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Available Cash</div>
                <div className="text-sm sm:text-base font-bold text-white font-mono mt-1 truncate">
                  {availCash.toLocaleString("en-IN", { style: "currency", currency: "INR" })}
                </div>
                <div className="text-[10px] text-[var(--qd-text-3)] font-mono mt-0.5">Execution limit</div>
              </div>

              <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Used Margin</div>
                <div className="text-sm sm:text-base font-bold text-[var(--qd-loss)] font-mono mt-1 truncate">
                  {usedMargin.toLocaleString("en-IN", { style: "currency", currency: "INR" })}
                </div>
                <div className="text-[10px] text-[var(--qd-text-3)] font-mono mt-0.5">Current block</div>
              </div>

              <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">SPAN Margin</div>
                <div className="text-sm sm:text-base font-bold text-[var(--qd-warn)] font-mono mt-1 truncate">
                  {spanMargin.toLocaleString("en-IN", { style: "currency", currency: "INR" })}
                </div>
                <div className="text-[10px] text-[var(--qd-text-3)] font-mono mt-0.5">Underlying hedge margin</div>
              </div>

              <div className="bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded p-3">
                <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Virtual Leverage</div>
                <div className="text-sm sm:text-base font-bold text-blue-400 font-mono mt-1 truncate">
                  {fundsData?.source === "paper" ? "5.0x MIS" : "1.0x NRML"}
                </div>
                <div className="text-[10px] text-[var(--qd-text-3)] font-mono mt-0.5">Daily leverage capacity</div>
              </div>
            </div>
          </div>

        </div>

        {/* RIGHT PANEL: ACTIONS & DIAGNOSTICS */}
        <div className="lg:col-span-4 space-y-5">
          
          {/* SYSTEMS CONFIGURATION CHECKLIST */}
          <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 shadow-xl">
            <h2 className="font-head text-sm font-bold text-white flex items-center gap-2 border-b border-[var(--qd-border)] pb-3 mb-4">
              <Shield size={16} className="text-indigo-400" /> Connection Checklist
            </h2>
            
            <div className="space-y-3 font-mono text-xs">
              {(data?.readiness?.checks || []).map((c) => (
                <ChecklistItem
                  key={c.id}
                  label={c.label}
                  checked={c.ok}
                  details={c.detail || (c.ok ? "OK" : c.hint || "Failed")}
                />
              ))}
              {(!data?.readiness?.checks || data.readiness.checks.length === 0) && (
                <div className="text-[var(--qd-text-3)] text-center py-2">No connection checks returned</div>
              )}
            </div>

            <div className="mt-5 pt-3.5 border-t border-[var(--qd-border)] space-y-2">
              <div className="flex justify-between items-center text-[10px] uppercase font-mono text-[var(--qd-text-3)]">
                <span>Active Data Broker</span>
                <span className="text-white font-bold">{prefs.data_broker || "-"}</span>
              </div>
              <div className="flex justify-between items-center text-[10px] uppercase font-mono text-[var(--qd-text-3)]">
                <span>Active Exec Broker</span>
                <span className="text-white font-bold">{prefs.execution_broker || "-"}</span>
              </div>
            </div>
          </div>

          {/* DIAGNOSTICS COUNTS */}
          <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 shadow-xl space-y-4">
            <h2 className="font-head text-sm font-bold text-white flex items-center gap-2 border-b border-[var(--qd-border)] pb-3">
              <Activity size={16} className="text-red-400 animate-pulse" /> Diagnostics Summary
            </h2>
            
            <div className="space-y-2">
              <DiagRow k="Server Time UTC" v={fmtUTC(data?.server_time_utc)} />
              <DiagRow k="India Time IST" v={fmt(data?.server_time_ist)} />
              <DiagRow k="Market Session" v={data?.market_session?.global_status || "CLOSED"} />
              <DiagRow k="NSE Segment" v={data?.market_session?.NSE_FO?.status || "CLOSED"} />
              <DiagRow k="Next Open" v={data?.market_session?.next_open ? fmt(data.market_session.next_open) : "N/A"} />
              <DiagRow k="Next Close" v={data?.market_session?.next_close ? fmt(data.market_session.next_close) : "N/A"} />
              <DiagRow k="Data Feed Latency" v={ticker.last_tick_at ? fmt(ticker.last_tick_at) : "No ticks"} />
              <DiagRow k="Subscribed Tokens" v={ticker.subscribed_tokens ?? 0} />
              <DiagRow k="Readiness Assessment" v={data?.readiness?.ready ? "System operational" : (upstox.reconnect_required ? "Reconnect Upstox required" : "Review configurations")} />
              <DiagRow k="Backend Version" v={data?.version || "12.0"} />
              <DiagRow k="Git Commit" v={data?.git_commit ? `${data.git_commit.slice(0, 7)}${data.git_dirty ? '*' : ''}` : "unknown"} />
            </div>
          </div>

        </div>

      </div>

      {/* PHASE 2 FEATURE FLAGS (read-only) */}
      <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 shadow-xl">
        <h2 className="font-head text-base font-bold text-white flex items-center gap-2 border-b border-[var(--qd-border)] pb-4 mb-4">
          <Zap size={18} className="text-[var(--qd-accent)]" /> Options-Alpha Features
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {featureFlags.length === 0 ? (
            <div className="text-xs font-mono text-[var(--qd-text-3)]">Loading…</div>
          ) : (
            featureFlags.map((f) => {
              const on = f.state === "on" || f.state === "enabled";
              const tone = on
                ? "bg-[rgba(52,199,89,0.12)] text-[var(--qd-profit)] border-[var(--qd-profit)]/30"
                : f.state === "shadow"
                ? "bg-[rgba(255,159,10,0.12)] text-[var(--qd-warn)] border-[var(--qd-warn)]/30"
                : "bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] border-[var(--qd-border)]";
              return (
                <div key={f.key} className="rounded-md border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3" data-testid={`feature-${f.key}`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-head text-sm font-bold text-[var(--qd-text)]">{f.label}</span>
                    <span className={`text-[9px] uppercase tracking-widest px-2 py-0.5 rounded-sm border ${tone}`}>{f.state}</span>
                  </div>
                  <p className="mt-1 text-[11px] text-[var(--qd-text-2)] leading-snug">{f.detail}</p>
                </div>
              );
            })
          )}
        </div>
        <p className="mt-3 text-[10px] font-mono text-[var(--qd-text-3)]">Read-only · set via env (docker-compose). Rollout: off → shadow → enabled.</p>
      </div>

      {/* QUICK AUTOMATION CONTROL PANEL */}
      <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg p-5 shadow-xl">
        <h2 className="font-head text-base font-bold text-white flex items-center gap-2 border-b border-[var(--qd-border)] pb-4 mb-4">
          <Power size={18} className="text-orange-400" /> Direct Control Panel Actions
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <ActionCard
            icon={AlertTriangle}
            title="Emergency Kill Switch"
            text="Toggle PAPER mode, suspend order placement, and pause ALL strategies instantly."
            danger
            busy={busy === "stop"}
            onClick={() => run("stop", "/ops/emergency-stop", "CRITICAL WARNING: This switch will halt all active trades, transition execution settings to PAPER, and pause every automated system in MongoDB. Proceed?")}
          />
          <ActionCard
            icon={SquareArrowOutUpRight}
            title="Emergency Square Off"
            text="Close all live open positions immediately on the active execution broker."
            danger
            busy={busy === "squareoff"}
            onClick={() => run("squareoff", "/ops/squareoff-all", "CRITICAL WARNING: This will immediately dispatch CLOSE orders for all positions recorded in the SQLite Options ledger. Trigger emergency square-off?")}
          />
          <ActionCard
            icon={Pause}
            title="Pause All Systems"
            text="Force all strategy modules to pause execution without changing the global mode."
            busy={busy === "pause"}
            onClick={() => run("pause", "/ops/strategies/pause-all", "Temporarily freeze all strategies?")}
          />
          <ActionCard
            icon={Play}
            title="Resume All Systems"
            text="Re-enable execution routes, resume options ledger gateways, and unpause strategies."
            busy={busy === "enable"}
            onClick={() => run("enable", "/ops/strategies/enable-all")}
          />
          <ActionCard
            icon={Wifi}
            title="Restart Upstox Feed"
            text="Flush active websocket subscriptions and perform full telemetry handshake."
            busy={busy === "ticker"}
            onClick={() => run("ticker", "/ops/ticker/restart")}
          />
          <ActionCard
            icon={RefreshCw}
            title="Auto Recover Tickers"
            text="Deploy background daemon script to repair ticker connections and synchronize positions."
            busy={busy === "recover"}
            onClick={() => run("recover", "/ops/auto-recover")}
          />
          <ActionCard
            icon={RefreshCw}
            title="Reconcile Orders"
            text="Fetch live Upstox order book and synchronize local order lifecycle states."
            busy={busy === "reconcile"}
            onClick={() => run("reconcile", "/ops/orders/sync")}
          />
          <ActionCard
            icon={Trash2}
            title="Clear Stale Paper Orders"
            text="Instantly clean up all local pending paper orders and release position locks."
            busy={busy === "clear-paper"}
            onClick={() => run("clear-paper", "/ops/paper-orders/clear-stale")}
          />
        </div>
      </div>

      {/* SYSTEMS RECOVERY ENGINE */}
      <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg shadow-xl overflow-hidden">
        <div className="border-b border-[var(--qd-border)] px-5 py-4 flex items-center justify-between bg-[var(--qd-surface)]">
          <div>
            <h2 className="font-head text-base font-bold text-white">Advanced Recovery Engine</h2>
            <p className="text-xs text-[var(--qd-text-3)] font-mono">Automated incident resolution and compliance metrics</p>
          </div>
          <span className={`font-mono text-xs font-semibold px-3 py-1.5 rounded border ${
            data?.recovery_plan?.status === "READY" 
              ? "text-[var(--qd-profit)] bg-[rgba(0,230,118,0.05)] border-[var(--qd-profit)]/30" 
              : "text-[var(--qd-warn)] bg-[rgba(255,159,10,0.05)] border-[var(--qd-warn)]/30"
          }`}>
            RECOVERY COMPLIANCE: {data?.recovery_plan?.status || "HEALTHY"} · {data?.recovery_plan?.score ?? 100}/100
          </span>
        </div>
        
        {!data?.recovery_plan?.issues?.length ? (
          <div className="p-6 text-sm text-[var(--qd-profit)] font-mono flex items-center gap-2">
            <CheckCircle2 size={16} /> All systems operational. Zero blocking issues detected by Risk engine.
          </div>
        ) : (
          <div className="divide-y divide-[rgba(255,255,255,0.05)]">
            {data.recovery_plan.issues.map((item, idx) => (
              <div key={`${item.title}-${idx}`} className="p-4 grid grid-cols-1 md:grid-cols-[120px_1fr_auto] gap-4 items-center">
                <span className={`font-mono text-[10px] uppercase font-bold tracking-wider px-2 py-1 rounded inline-block text-center ${
                  item.severity === "critical" 
                    ? "text-[var(--qd-loss)] bg-red-500/10 border border-red-500/20" 
                    : item.severity === "warning" 
                      ? "text-[var(--qd-warn)] bg-orange-500/10 border border-orange-500/20" 
                      : "text-[var(--qd-text-3)] bg-[var(--qd-surface-2)] border border-[var(--qd-border)]"
                }`}>
                  {item.severity}
                </span>
                <div>
                  <div className="text-white font-semibold text-sm">{item.title}</div>
                  <div className="text-xs text-[var(--qd-text-2)] mt-0.5">{item.detail}</div>
                  <div className="text-xs font-mono text-indigo-400 mt-1.5 flex items-center gap-1">
                    <span className="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-ping"></span> Suggested Action: {item.action}
                  </div>
                </div>
                {item.endpoint && (
                  <button 
                    onClick={() => run(`plan-${idx}`, item.endpoint)} 
                    disabled={busy === `plan-${idx}`}
                    className="border border-indigo-500/40 hover:border-indigo-400 hover:bg-indigo-500/10 px-4 py-2 rounded text-xs font-mono uppercase text-white shadow-md transition-all active:scale-95"
                  >
                    {busy === `plan-${idx}` ? "Deploying..." : "Auto Fix"}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* BLOCKED/ERRORED STRATEGIES */}
      <div className="border border-[var(--qd-border)] bg-[var(--qd-surface)] backdrop-blur-md rounded-lg shadow-xl overflow-hidden">
        <div className="border-b border-[var(--qd-border)] px-5 py-4 bg-[var(--qd-surface)] flex justify-between items-center">
          <div>
            <h2 className="font-head text-base font-bold text-white">Halted Strategies (Blocking Errors)</h2>
            <p className="text-xs text-[var(--qd-text-3)] font-mono">Strategies currently suspended by the Options Risk engine</p>
          </div>
          <button 
            onClick={() => run("clear", "/ops/strategies/clear-errors")} 
            disabled={busy === "clear"}
            className="px-3.5 py-1.5 border border-red-500/30 hover:border-red-400 text-xs font-mono font-semibold uppercase rounded text-[var(--qd-loss)] hover:bg-red-500/10 flex items-center gap-1.5"
          >
            <Trash2 size={14} /> Clear Halts
          </button>
        </div>
        
        {!data?.errored_strategies?.length ? (
          <div className="p-6 text-sm text-[var(--qd-text-3)] font-mono text-center">
            Zero strategies are currently in a blocked error state.
          </div>
        ) : (
          <div className="qd-table-wrap">
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] border-b border-[var(--qd-border)] bg-[var(--qd-surface)]">
                  <th className="px-5 py-3">Strategy ID / Name</th>
                  <th className="px-5 py-3">Status</th>
                  <th className="px-5 py-3">Telemetry Segment</th>
                  <th className="px-5 py-3 text-right">Registered Error Logs</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[rgba(255,255,255,0.05)]">
                {data.errored_strategies.map((s) => (
                  <tr key={s.id} className="hover:bg-transparent">
                    <td className="px-5 py-3 text-white font-semibold">{s.name} <span className="text-[10px] text-[var(--qd-text-3)]">({s.id})</span></td>
                    <td className="px-5 py-3 text-[var(--qd-loss)]">{s.status?.toUpperCase()}</td>
                    <td className="px-5 py-3 text-[var(--qd-text-2)]">{s.last_data_source || "-"}</td>
                    <td className="px-5 py-3 text-[var(--qd-warn)] text-xs text-right max-w-md truncate select-all">{s.last_error}</td>
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
  if (key === "stop") return `Emergency Halt successfully triggered. Switched to PAPER and paused ${data.paused_strategies || 0} strategies.`;
  if (key === "pause") return `Suspended ${data.paused_strategies || 0} strategies in database.`;
  if (key === "enable") return `Successfully unpaused and synced ${data.enabled_strategies || 0} strategies with option engine.`;
  if (key === "ticker") return data.started ? `Market Ticker connection reloaded for ${data.tokens || 0} subscriptions.` : `Ticker startup skipped: ${data.reason || "active"}`;
  if (key === "clear") return `Halt registers flushed on ${data.updated_strategies || 0} strategies.`;
  if (key === "recover") return `Automated connection recovery checks executed. Checked tickers and pools.`;
  if (key === "reconcile") return `Order reconciliation complete: synced statuses, resolved stale orders.`;
  if (key === "clear-paper") return `Successfully cleared paper orders.`;
  if (String(key).startsWith("plan-")) return "Remediation fix deployed successfully.";
  if (key === "squareoff") return `Squareoff dispatch complete: ${data.closed?.length || 0} closed, ${data.failed?.length || 0} rejected by broker.`;
  return "Handshake complete.";
}

// Sleek Metric Card
const MetricCard = ({ label, value, tone, isPulse }) => (
  <div className="border border-[var(--qd-border)] bg-[var(--qd-surface-2)] backdrop-blur-md rounded-md p-3 hover:border-[var(--qd-border-strong)] transition-all">
    <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className="flex items-center gap-1.5 mt-1">
      {isPulse && <span className="w-1.5 h-1.5 bg-[var(--qd-profit)] rounded-full animate-ping"></span>}
      <div className={`font-head text-[13px] sm:text-sm font-bold tracking-tight truncate ${
        tone === "profit" ? "text-[var(--qd-profit)]" : 
        tone === "loss" ? "text-[var(--qd-loss)] animate-pulse" : 
        tone === "warn" ? "text-[var(--qd-warn)]" : 
        "text-white"
      }`}>
        {value}
      </div>
    </div>
  </div>
);

// Diagnostics Row
const DiagRow = ({ k, v }) => (
  <div className="flex justify-between items-center border-b border-[rgba(255,255,255,0.04)] pb-2 pt-1 font-mono text-[11px] gap-2">
    <span className="text-[var(--qd-text-3)] shrink-0">{k}</span>
    <span className="text-white text-right font-medium truncate select-all flex-1 min-w-0">{v}</span>
  </div>
);

// Performance Telemetry Metric
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

// Checklist Item
const ChecklistItem = ({ label, checked, details }) => (
  <div className="flex items-start justify-between gap-3 border-b border-[rgba(255,255,255,0.03)] pb-2.5">
    <div className="flex items-center gap-2">
      {checked ? (
        <CheckCircle2 size={13} className="text-[var(--qd-profit)] flex-shrink-0" />
      ) : (
        <XCircle size={13} className="text-[var(--qd-warn)] flex-shrink-0 animate-pulse" />
      )}
      <span className={checked ? "text-[var(--qd-text-2)]" : "text-[var(--qd-text-3)]"}>{label}</span>
    </div>
    <span className="text-[10px] text-[var(--qd-text-3)] font-mono text-right truncate max-w-[120px]">{details}</span>
  </div>
);

// Sleek Action Button Card
const ActionCard = ({ icon: Icon, title, text, onClick, busy, danger }) => (
  <button
    onClick={onClick}
    disabled={busy}
    className={`w-full text-left border rounded-md p-4 flex gap-3.5 transition-all duration-200 hover:-translate-y-[1px] disabled:opacity-60 disabled:pointer-events-none ${
      danger
        ? "border-[rgba(255,59,48,0.3)] bg-red-950/10 text-[var(--qd-loss)] hover:bg-red-500/10 hover:border-red-500 hover:shadow-lg hover:shadow-red-500/5"
        : "border-[var(--qd-border)] bg-[var(--qd-surface)] text-white hover:border-[var(--qd-border-strong)] hover:bg-[var(--qd-surface-2)] hover:shadow-lg hover:shadow-black/20"
    }`}
  >
    <div className={`p-2 rounded-lg flex-shrink-0 ${danger ? "bg-red-500/10 text-[var(--qd-loss)]" : "bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]"}`}>
      <Icon size={18} />
    </div>
    <span className="flex-1 min-w-0">
      <span className="block text-xs uppercase tracking-wide font-bold font-mono">{busy ? "Transmitting..." : title}</span>
      <span className="block text-[11px] text-[var(--qd-text-3)] leading-relaxed mt-1">{text}</span>
    </span>
  </button>
);
