import React, { createContext, useContext, useState, useEffect, useRef, useCallback, useMemo } from "react";
import { api, formatApiErrorDetail } from "../lib/api";

const ExecutionStateContext = createContext(null);

const DEFAULT_POLL_MS = 15000;
// When the browser tab is hidden we slow polling way down instead of stopping
// entirely, so positions are reasonably fresh the moment the user returns.
const HIDDEN_POLL_MS = 60000;

export const ExecutionStateProvider = ({ children }) => {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pollMs, setPollMs] = useState(DEFAULT_POLL_MS);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const timerRef = useRef(null);
  const lastFingerprint = useRef(null);

  const refresh = useCallback(async () => {
    // Guard against overlapping requests: during live market the snapshot
    // endpoint can take longer than the poll interval, and stacking requests
    // is what makes the UI freeze. Skip if one is already in flight.
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const r = await api.get("/execution/snapshot");
      if (!mounted.current) return;
      // Avoid re-rendering the whole app when nothing actually changed.
      // generated_at is re-stamped on every request, so we compare the payload
      // with that timestamp stripped out. Off-hours (no LTP/PnL movement) this
      // skips the state update entirely; during live market the content really
      // does change each poll, so the render happens as needed.
      const { generated_at, ...rest } = r.data || {};
      let fingerprint;
      try {
        fingerprint = JSON.stringify(rest);
      } catch {
        fingerprint = null;
      }
      if (fingerprint && fingerprint === lastFingerprint.current) {
        setError("");
      } else {
        lastFingerprint.current = fingerprint;
        setSnapshot(r.data);
        setError("");
      }
    } catch (e) {
      if (!mounted.current) return;
      setError(formatApiErrorDetail(e.response?.data?.detail) || e.message || "Execution sync failed");
    } finally {
      inFlight.current = false;
      if (mounted.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;

    const tick = async () => {
      await refresh();
      if (!mounted.current) return;
      const delay = document.hidden ? HIDDEN_POLL_MS : pollMs;
      // Recursive timeout (not setInterval) so the next poll is only scheduled
      // after the previous one settles — no request pile-up on a slow network.
      timerRef.current = setTimeout(tick, delay);
    };

    const onVisible = () => {
      if (!document.hidden) {
        // Returning to the tab: refresh immediately and restart the cadence.
        if (timerRef.current) clearTimeout(timerRef.current);
        tick();
      }
    };

    tick();
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      mounted.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh, pollMs]);

  const value = useMemo(() => {
    const positions = snapshot?.positions || [];
    const orders = snapshot?.orders || [];
    const skippedSignals = snapshot?.skipped_signals || [];
    const openOrders = snapshot?.open_orders || [];
    const failedOrders = snapshot?.failed_orders || [];
    const strategyPositions = snapshot?.strategy_positions || [];
    const summary = snapshot?.summary || {};
    const wallet = snapshot?.wallet || {};
    const feedState = snapshot?.feed_state || {};
    const strategyState = snapshot?.strategy_state || {};
    const paperMode = Boolean(snapshot?.paper_mode);
    const executionBroker = snapshot?.execution_broker || "upstox";
    const marketSession = snapshot?.market_session;
    const upstoxDataHealth = snapshot?.upstox_data_health;
    const brokerReconciliation = snapshot?.broker_reconciliation;
    const generatedAt = snapshot?.generated_at;

    return {
      snapshot,
      loading,
      error,
      refresh,
      pollMs,
      setPollMs,
      positions,
      orders,
      skippedSignals,
      openOrders,
      failedOrders,
      strategyPositions,
      summary,
      wallet,
      feedState,
      strategyState,
      paperMode,
      executionBroker,
      marketSession,
      upstoxDataHealth,
      brokerReconciliation,
      generatedAt,
    };
  }, [snapshot, loading, error, refresh, pollMs]);

  return (
    <ExecutionStateContext.Provider value={value}>
      {children}
    </ExecutionStateContext.Provider>
  );
};

export const useExecutionStateContext = () => {
  const ctx = useContext(ExecutionStateContext);
  if (!ctx) {
    throw new Error("useExecutionStateContext must be used within an ExecutionStateProvider");
  }
  return ctx;
};
