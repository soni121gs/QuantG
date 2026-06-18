import React, { createContext, useContext, useState, useEffect, useRef, useCallback } from "react";
import { api, formatApiErrorDetail } from "../lib/api";

const ExecutionStateContext = createContext(null);

const DEFAULT_POLL_MS = 15000;

export const ExecutionStateProvider = ({ children }) => {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pollMs, setPollMs] = useState(DEFAULT_POLL_MS);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const r = await api.get("/execution/snapshot");
      if (!mounted.current) return;
      setSnapshot(r.data);
      setError("");
    } catch (e) {
      if (!mounted.current) return;
      setError(formatApiErrorDetail(e.response?.data?.detail) || e.message || "Execution sync failed");
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

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
  const wallet = snapshot?.wallet || {};
  const feedState = snapshot?.feed_state || {};
  const strategyState = snapshot?.strategy_state || {};
  const paperMode = Boolean(snapshot?.paper_mode);
  const executionBroker = snapshot?.execution_broker || "upstox";
  const marketSession = snapshot?.market_session;
  const upstoxDataHealth = snapshot?.upstox_data_health;
  const brokerReconciliation = snapshot?.broker_reconciliation;
  const generatedAt = snapshot?.generated_at;

  const value = {
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
