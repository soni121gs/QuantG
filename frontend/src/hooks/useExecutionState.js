import { useExecutionStateContext } from "../contexts/ExecutionStateContext";

/**
 * Backward-compatible hook that taps into the global ExecutionStateContext.
 * Eliminates duplicate network queries by sharing a single snapshot.
 */
export function useExecutionState() {
  const context = useExecutionStateContext();

  return {
    snapshot: context.snapshot,
    positions: context.positions,
    orders: context.orders,
    skippedSignals: context.skippedSignals,
    openOrders: context.openOrders,
    failedOrders: context.failedOrders,
    strategyPositions: context.strategyPositions,
    summary: context.summary,
    loading: context.loading,
    error: context.error,
    refresh: context.refresh,
    paperMode: context.paperMode,
    executionBroker: context.executionBroker,
    marketSession: context.marketSession,
    upstoxDataHealth: context.upstoxDataHealth,
    brokerReconciliation: context.brokerReconciliation,
    wallet: context.wallet,
    feedState: context.feedState,
    strategyState: context.strategyState,
    generatedAt: context.generatedAt,
  };
}

export default useExecutionState;
