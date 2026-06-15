// Friendly labels for engine exit / skip / reject reason codes.
// Phase 2 added several codes (theta-decay, spread-tp/sl, IV_RANK_*, ORDERFLOW_*)
// that otherwise render as raw strings. reasonLabel() normalises a raw code or
// message to { label, tone, hint, raw }. Tones map to the app's CSS color vars.

const TONE_CLASS = {
  profit: "text-[var(--qd-profit)]",
  loss: "text-[var(--qd-loss)]",
  warn: "text-[var(--qd-warn)]",
  info: "text-[var(--qd-accent)]",
  neutral: "text-[var(--qd-text-2)]",
};

export function reasonToneClass(tone) {
  return TONE_CLASS[tone] || TONE_CLASS.neutral;
}

// Keyed by a normalised reason (text before ":", minute suffix stripped, lowercased).
const MAP = {
  // ── Phase 2 exits ──
  "theta-decay": { label: "Closed early · theta decay", tone: "warn", hint: "No progress while premium was bleeding to time decay." },
  "theta-no-progress": { label: "Closed early · no progress", tone: "warn", hint: "Trade stalled within the no-progress window; exited before more decay." },
  "spread-tp": { label: "Spread target hit (50% credit)", tone: "profit", hint: "Captured ~50% of the credit collected." },
  "spread-sl": { label: "Spread stop (2× credit)", tone: "loss", hint: "Spread loss reached ~2× the credit collected." },
  // ── exits (existing) ──
  "take-profit": { label: "Take profit", tone: "profit" },
  "stop-loss": { label: "Stop loss", tone: "loss" },
  "trailing-sl": { label: "Trailing stop", tone: "profit", hint: "Locked-in profit via trailing stop." },
  "time-exit": { label: "Time exit", tone: "warn", hint: "Held to the max time and squared off." },
  "intraday-squareoff": { label: "EOD square-off", tone: "warn", hint: "Intraday positions force-closed near 15:10 IST." },
  "risk-trigger": { label: "Risk exit", tone: "loss" },
  // ── Phase 2 entry gates ──
  "iv_rank_high": { label: "Blocked · IV too rich", tone: "info", hint: "IV rank above the buy cap — option premium is expensive." },
  "iv_rank_gate": { label: "Blocked · IV too rich", tone: "info", hint: "IV-rank gate blocked option buying." },
  "iv_rank_shadow": { label: "Shadow · would block (IV rich)", tone: "neutral", hint: "IV-rank gate is in shadow mode — logged only, not enforced." },
  "orderflow_weak": { label: "Blocked · weak order flow", tone: "info", hint: "Not enough net buying pressure (tbq/tsq) to confirm the entry." },
  "orderflow_gate": { label: "Blocked · weak order flow", tone: "info" },
  "orderflow_shadow": { label: "Shadow · would block (flow)", tone: "neutral", hint: "Order-flow gate is in shadow mode — logged only." },
  "theta_guard": { label: "Blocked · low volatility", tone: "info", hint: "Underlying ATR% below threshold — a bought option would just bleed theta." },
  "credit_spreads_disabled": { label: "Spread off · enable to trade", tone: "neutral", hint: "Strategy is set to credit_spread but CREDIT_SPREADS_ENABLED is off." },
  // ── throttles (existing) ──
  "max_trades_reached": { label: "Daily trade cap reached", tone: "neutral" },
  "max-trades-reached": { label: "Daily trade cap reached", tone: "neutral" },
  "cooldown": { label: "Cooldown active", tone: "neutral" },
  "cooldown-active": { label: "Cooldown active", tone: "neutral" },
  "strategy_signal_spam": { label: "Signal spam throttled", tone: "neutral" },
  // ── sizing / data ──
  "rejected_risk_sizing": { label: "Blocked · position sizing", tone: "warn", hint: "Capital/margin/risk cap reduced size below one lot." },
  "skipped_option_quality_low": { label: "Skipped · low quote quality", tone: "warn" },
  "option_quality_low": { label: "Skipped · low quote quality", tone: "warn" },
  "price_unavailable": { label: "Skipped · no price", tone: "warn" },
  "feed_disconnected": { label: "Skipped · feed offline", tone: "loss" },
  "skipped_quote_stale": { label: "Skipped · stale quote", tone: "warn" },
  "stale_quote": { label: "Skipped · stale quote", tone: "warn" },
  "live_preflight_not_ready": { label: "Blocked · live preflight", tone: "warn" },
  "execution_skipped": { label: "Skipped · execution error", tone: "loss" },
  "duplicate_exit": { label: "Duplicate exit (ignored)", tone: "neutral" },
  "duplicate_signal": { label: "Duplicate signal (ignored)", tone: "neutral" },
  "symbol_group_active_position_exists": { label: "Position already open", tone: "neutral" },
};

function humanize(raw) {
  return String(raw)
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function reasonLabel(raw) {
  if (!raw) return { label: "—", tone: "neutral", hint: "", raw: "" };
  const full = String(raw).trim();
  let key = full.split(":")[0].trim().toLowerCase();
  key = key.replace(/-\d+m$/, ""); // theta-decay-12m → theta-decay

  if (MAP[key]) return { ...MAP[key], raw: full };
  const alt = key.replace(/\s+/g, "_");
  if (MAP[alt]) return { ...MAP[alt], raw: full };
  for (const k of Object.keys(MAP)) {
    if (key.startsWith(k)) return { ...MAP[k], raw: full };
  }
  // Unknown: if it's already a human sentence, show it; else humanize the code.
  if (/\s/.test(full)) return { label: full, tone: "neutral", hint: full, raw: full };
  return { label: humanize(key), tone: "neutral", hint: full, raw: full };
}
