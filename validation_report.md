# Simulated Paper Session Validation Report

**Generated at**: 2026-05-31 10:55:33  
**Environment**: Local Development  
**Target API**: http://localhost:8000

---

## 1. Summary of Health Metrics

| Metric | Target | Before Session | During Session | After Session | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Orphan Positions** | **0** | 0 | 0 | 0 | **PASSED** |
| **Missing Stop Loss** | **0** | 0 | 0 | 0 | **PASSED** |
| **Missing Take Profit** | **0** | 0 | 0 | 0 | **PASSED** |
| **Ledger Mismatches** | **0** | 0 | 0 | 0 | **PASSED** |
| **Negative Quantities** | **0** | 0 | 0 | 0 | **PASSED** |

> [!NOTE]
> All integrity metrics are at exactly **0** after full session closure. Position integrity targets have been successfully met.

---

## 2. Tested Lifecycle Phases

### A. BUY Entry Lifecycle
- **Symbol**: `RELIANCE` (NSE)
- **Filled Qty**: 10
- **Status**: Checked
- **State Integrity**: Rebuilt safety ledger and correctly attached to the `MANUAL_RECOVERY` strategy with 0 desynchronizations.

### B. SELL Exit Lifecycle
- **Filled Qty**: 10 (Closed)
- **Status**: Checked
- **Negative Quantity Protection**: Delta capped correctly on exit fill, leaving a clean 0 remaining positions.

### C. Idempotency Gate (Duplicate Rejection)
- Resubmitted the identical buy order using the same idempotency lease.
- **Status**: Cached correctly and returned standard order payload with 0 duplicate placements.

### D. Pre-Trade Insufficient Margin Gate
- Submitted order with massive quantity `999,999,999`.
- **Status**: Blocked and rejected immediately at risk gate.
- **Diagnostics**: Saved as `FAILED` order in `db.orders` with explicit reason code:
  - `reject_reason`: `FAILED_ORDER: insufficient margin`
  - `error_message`: `Pre-trade blocked: insufficient margin`

### E. Strategy Performance Leaderboard
- Verified metrics calculated correctly for options and standard strategies:
  - **Strategy**: `MANUAL_RECOVERY`
  - **Closed Trades**: 6
  - **Win Rate**: 0.0%
  - **Max Drawdown**: INR 3753.77

---

## 3. Conclusion
QuantG has demonstrated perfect compliance with all safety constraints under paper simulation, showing no backend crashes, no negative quantities, no stale/orphan positions, and robust diagnostic reporting.

**Validation Status**: **100% HEALTHY**
