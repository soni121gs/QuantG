import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatApiErrorDetail } from "../lib/api";

const DEFAULT_POLL_MS = 10000;

/**
 * Single source of truth for live execution UI (positions, orders, SL/TP).
 * Polls GET /execution/snapshot and mirrors backend execution_status fields.
 */
export function useExecutionState({ pollMs = DEFAULT_POLL_MS, syncOnLoad = false } = {}) {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const r = await api.get("/execution/snapshot", { params: { sync: syncOnLoad } });
      if (!mounted.current) return;
      setSnapshot(r.data);
      setError("");
    } catch (e) {
      if (!mounted.current) return;
      setError(formatApiErrorDetail(e.response?.data?.detail) || e.message || "Execution sync failed");
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [syncOnLoad]);

  useEffect(() => {
    mounted.current = true;
    refresh();
    const timer = setInterval(refresh, pollMs);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [refresh, pollMs]);

  const positions = snapshot?.positions || [];
  const orders = snapshot?.orders || [];
  const skippedSignals = snapshot?.skipped_signals || [];
  const openOrders = snapshot?.open_orders || [];
  const failedOrders = snapshot?.failed_orders || [];
  const strategyPositions = snapshot?.strategy_positions || [];
  const summary = snapshot?.summary || {};

  return {
    snapshot,
    positions,
    orders,
    skippedSignals,
    openOrders,
    failedOrders,
    strategyPositions,
    summary,
    loading,
    error,
    refresh,
    paperMode: Boolean(snapshot?.paper_mode),
    executionBroker: snapshot?.execution_broker || "upstox",
    marketSession: snapshot?.market_session,
    generatedAt: snapshot?.generated_at,
  };
}

export default useExecutionState;
