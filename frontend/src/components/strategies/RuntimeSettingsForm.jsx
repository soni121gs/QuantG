import React, { useState, useEffect } from "react";
import { Shield, RefreshCw, CheckCircle2 } from "lucide-react";

export const RuntimeSettingsForm = ({ s, saving, onSubmit }) => {
  const risk = s.visual_config?.risk || {};
  const options = s.visual_config?.options || {};
  
  const [form, setForm] = useState({
    target_pct: risk.target_pct ?? "",
    stoploss_pct: risk.stoploss_pct ?? "",
    trailing_sl_enabled: risk.trailing_sl_enabled ?? false,
    trail_trigger_pct: risk.trail_trigger_pct ?? "",
    trail_step_pct: risk.trail_step_pct ?? "",
    cooldown_minutes: risk.cooldown_minutes ?? "",
    max_trades_day: risk.max_trades_day ?? "",
    daily_loss_limit: risk.daily_loss_limit ?? "",
    required_capital: s.required_capital ?? risk.required_capital ?? "",
    time_exit_minutes: risk.time_exit_minutes ?? "",
    indicator_exit_enabled: risk.indicator_exit_enabled ?? false,
    exit_mode: risk.exit_mode ?? "SQUARE_OFF",
    strategy_category: risk.strategy_category ?? "intraday",
    broker: s.broker ?? "upstox",
    mode: s.mode ?? "paper",
    product: s.visual_config?.options?.product ?? s.product ?? "MIS",
    structure: s.visual_config?.options?.structure ?? "single_leg",
    spread_width: s.visual_config?.options?.spread_width ?? 2,
    short_delta: s.visual_config?.options?.short_delta ?? 0.30,
  });

  useEffect(() => {
    const freshRisk = s.visual_config?.risk || {};
    setForm({
      target_pct: freshRisk.target_pct ?? "",
      stoploss_pct: freshRisk.stoploss_pct ?? "",
      trailing_sl_enabled: freshRisk.trailing_sl_enabled ?? false,
      trail_trigger_pct: freshRisk.trail_trigger_pct ?? "",
      trail_step_pct: freshRisk.trail_step_pct ?? "",
      cooldown_minutes: freshRisk.cooldown_minutes ?? "",
      max_trades_day: freshRisk.max_trades_day ?? "",
      daily_loss_limit: freshRisk.daily_loss_limit ?? "",
      required_capital: s.required_capital ?? freshRisk.required_capital ?? "",
      time_exit_minutes: freshRisk.time_exit_minutes ?? "",
      indicator_exit_enabled: freshRisk.indicator_exit_enabled ?? false,
      exit_mode: freshRisk.exit_mode ?? "SQUARE_OFF",
      strategy_category: freshRisk.strategy_category ?? "intraday",
      broker: s.broker ?? "upstox",
      mode: s.mode ?? "paper",
      product: s.visual_config?.options?.product ?? s.product ?? "MIS",
      structure: s.visual_config?.options?.structure ?? "single_leg",
      spread_width: s.visual_config?.options?.spread_width ?? 2,
      short_delta: s.visual_config?.options?.short_delta ?? 0.30,
    });
  }, [s]);

  const handleSubmit = (e) => {
    e.preventDefault();
    const payload = {
      target_pct: form.target_pct !== "" ? parseFloat(form.target_pct) : null,
      stoploss_pct: form.stoploss_pct !== "" ? parseFloat(form.stoploss_pct) : null,
      trailing_sl_enabled: !!form.trailing_sl_enabled,
      trail_trigger_pct: form.trail_trigger_pct !== "" ? parseFloat(form.trail_trigger_pct) : null,
      trail_step_pct: form.trail_step_pct !== "" ? parseFloat(form.trail_step_pct) : null,
      cooldown_minutes: form.cooldown_minutes !== "" ? parseInt(form.cooldown_minutes) : null,
      max_trades_day: form.max_trades_day !== "" ? parseInt(form.max_trades_day) : null,
      daily_loss_limit: form.daily_loss_limit !== "" ? parseFloat(form.daily_loss_limit) : null,
      required_capital: form.required_capital !== "" ? parseFloat(form.required_capital) : null,
      time_exit_minutes: form.time_exit_minutes !== "" ? parseInt(form.time_exit_minutes) : null,
      indicator_exit_enabled: !!form.indicator_exit_enabled,
      exit_mode: form.exit_mode,
      strategy_category: form.strategy_category || null,
      broker: form.broker,
      mode: form.mode,
      product: form.product,
      structure: form.structure,
      spread_width: (form.spread_width !== "" && form.spread_width != null) ? parseInt(form.spread_width) : null,
      short_delta: (form.short_delta !== "" && form.short_delta != null) ? parseFloat(form.short_delta) : null,
    };
    onSubmit(payload);
  };

  return (
    <form onSubmit={handleSubmit} className="mt-3 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-md p-3.5 space-y-3.5">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Target profit (TP %)</label>
          <input 
            type="number" 
            step="0.01" 
            value={form.target_pct} 
            onChange={(e) => setForm({ ...form, target_pct: e.target.value })}
            placeholder="e.g. 2.5" 
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] focus:border-indigo-500 rounded px-2.5 py-1.5 text-xs text-white"
          />
        </div>
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Stop loss (SL %)</label>
          <input 
            type="number" 
            step="0.01" 
            value={form.stoploss_pct} 
            onChange={(e) => setForm({ ...form, stoploss_pct: e.target.value })}
            placeholder="e.g. 1.2" 
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] focus:border-indigo-500 rounded px-2.5 py-1.5 text-xs text-white"
          />
        </div>
      </div>

      <div className="rounded-md border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2.5 space-y-2">
        <div className="flex items-center justify-between">
          <span className="font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-2)]">Trailing Stop Loss</span>
          <input 
            type="checkbox" 
            checked={form.trailing_sl_enabled} 
            onChange={(e) => setForm({ ...form, trailing_sl_enabled: e.target.checked })}
            className="w-3.5 h-3.5 accent-indigo-500 bg-[var(--qd-surface-2)] border-[var(--qd-border)] cursor-pointer"
          />
        </div>
        
        {form.trailing_sl_enabled && (
          <div className="grid grid-cols-2 gap-2 pt-1">
            <div>
              <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Trail Trigger %</label>
              <input 
                type="number" 
                step="0.01" 
                value={form.trail_trigger_pct} 
                onChange={(e) => setForm({ ...form, trail_trigger_pct: e.target.value })}
                placeholder="e.g. 1.0" 
                className="w-full bg-[var(--qd-surface-3)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
              />
            </div>
            <div>
              <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Trail Step %</label>
              <input 
                type="number" 
                step="0.01" 
                value={form.trail_step_pct} 
                onChange={(e) => setForm({ ...form, trail_step_pct: e.target.value })}
                placeholder="e.g. 0.2" 
                className="w-full bg-[var(--qd-surface-3)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
              />
            </div>
          </div>
        )}
      </div>

      <div className="border-t border-[var(--qd-border)] pt-2.5">
        <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Strategy Category</label>
        <select
          value={form.strategy_category}
          onChange={(e) => {
            const cat = e.target.value;
            if (cat === "scalper") {
              setForm({ ...form, strategy_category: cat, cooldown_minutes: "1", max_trades_day: "20", daily_loss_limit: "3000" });
            } else {
              setForm({ ...form, strategy_category: cat });
            }
          }}
          className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
        >
          <option value="scalper">SCALPER — high frequency (cooldown ≤ 3m, ≥ 5 trades/day)</option>
          <option value="intraday">INTRADAY — moderate frequency</option>
          <option value="swing">SWING — low frequency</option>
        </select>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Cooldown (Min)</label>
          <input
            type="number"
            value={form.cooldown_minutes}
            onChange={(e) => setForm({ ...form, cooldown_minutes: e.target.value })}
            placeholder="30"
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
          />
        </div>
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Max Trades/Day</label>
          <input 
            type="number" 
            value={form.max_trades_day} 
            onChange={(e) => setForm({ ...form, max_trades_day: e.target.value })}
            placeholder="5" 
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
          />
        </div>
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Loss Limit (INR)</label>
          <input 
            type="number" 
            value={form.daily_loss_limit} 
            onChange={(e) => setForm({ ...form, daily_loss_limit: e.target.value })}
            placeholder="5000" 
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 border-t border-[var(--qd-border)] pt-2.5">
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Time Exit (Min)</label>
          <input 
            type="number" 
            value={form.time_exit_minutes} 
            onChange={(e) => setForm({ ...form, time_exit_minutes: e.target.value })}
            placeholder="360" 
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white"
          />
        </div>
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Exit Mode</label>
          <select 
            value={form.exit_mode} 
            onChange={(e) => setForm({ ...form, exit_mode: e.target.value })}
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            <option value="SQUARE_OFF">SQUARE OFF</option>
            <option value="REVERSE">REVERSE</option>
            <option value="NONE">NONE</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 border-t border-[var(--qd-border)] pt-2.5">
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Execution Broker</label>
          <select 
            value={form.broker} 
            onChange={(e) => setForm({ ...form, broker: e.target.value })}
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            <option value="upstox">Upstox (HFT Enabled)</option>
          </select>
        </div>
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Deployment Mode</label>
          <select 
            value={form.mode} 
            onChange={(e) => setForm({ ...form, mode: e.target.value })}
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            <option value="paper">Paper Trading (Simulated)</option>
            <option value="live">Live Trading (Production)</option>
          </select>
        </div>
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Product Type</label>
          <select 
            value={form.product} 
            onChange={(e) => setForm({ ...form, product: e.target.value })}
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
          >
            <option value="MIS">MIS (Intraday)</option>
            <option value="CNC">CNC (Delivery)</option>
            <option value="NRML">NRML (Normal)</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 pt-1">
        <div>
          <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Allocated Capital (INR)</label>
          <input 
            type="number" 
            value={form.required_capital} 
            onChange={(e) => setForm({ ...form, required_capital: e.target.value })}
            placeholder="50000" 
            className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
          />
        </div>
        <div className="flex flex-col justify-end pb-1.5">
          <div className="flex items-center justify-between px-1">
            <span className="font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)]">Indicator Exit</span>
            <input 
              type="checkbox" 
              checked={form.indicator_exit_enabled} 
              onChange={(e) => setForm({ ...form, indicator_exit_enabled: e.target.checked })}
              className="w-3.5 h-3.5 accent-indigo-500 cursor-pointer"
            />
          </div>
        </div>
      </div>

      {s.visual_config?.options?.enabled && (
        <div className="grid grid-cols-3 gap-2 pt-1">
          <div>
            <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Structure</label>
            <select
              value={form.structure}
              onChange={(e) => setForm({ ...form, structure: e.target.value })}
              className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1.5 text-[11px] text-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
              data-testid="settings-structure"
            >
              <option value="single_leg">Single Leg</option>
              <option value="credit_spread">Credit Spread</option>
              <option value="debit_spread">Debit Spread</option>
            </select>
          </div>
          {(form.structure === "credit_spread" || form.structure === "debit_spread") && (
            <>
              <div>
                <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">
                  {form.structure === "debit_spread" ? "Long Δ" : "Short Δ"}
                </label>
                <input
                  type="number" step="0.05" min="0.05" max="0.95"
                  value={form.short_delta}
                  onChange={(e) => setForm({ ...form, short_delta: e.target.value })}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                  data-testid="settings-short-delta"
                />
              </div>
              <div>
                <label className="block font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-3)] mb-1">Width (strikes)</label>
                <input
                  type="number" min="1" max="20"
                  value={form.spread_width}
                  onChange={(e) => setForm({ ...form, spread_width: e.target.value })}
                  className="w-full bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded px-2 py-1 text-[11px] text-white"
                  data-testid="settings-spread-width"
                />
              </div>
            </>
          )}
        </div>
      )}

      <button
        type="submit"
        disabled={saving}
        className="w-full py-2 bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-[var(--qd-accent-contrast)] font-mono text-[11px] uppercase font-bold tracking-wider rounded border border-[var(--qd-border-strong)] shadow-md active:scale-95 transition-all flex items-center justify-center gap-1.5 disabled:opacity-50"
      >
        {saving ? (
          <RefreshCw size={12} className="animate-spin" />
        ) : (
          <CheckCircle2 size={12} />
        )}
        {saving ? "Syncing Bounds..." : "Sync Risk Bounds"}
      </button>
    </form>
  );
};

export default RuntimeSettingsForm;
