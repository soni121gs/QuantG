import React, { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Plus, X, Save, Blocks, ArrowRight, TrendingUp } from "lucide-react";
import { toast } from "sonner";

const INDICATORS = ["RSI", "SMA(5)", "SMA(20)", "EMA(50)", "MACD", "Price"];
const OPS = [">", "<", "==", "Crosses Above", "Crosses Below"];
const ACTIONS = ["BUY", "SELL", "EXIT"];

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"];
const STRIKE_MODES = [
  { id: "ATM_BUY", label: "Buy ATM", desc: "BUY signal → buy ATM Call. SELL → buy ATM Put. Directional bet, simplest." },
  { id: "OTM_BUY", label: "Buy OTM", desc: "BUY → buy OTM Call by N points. Cheaper premium, lower probability." },
  { id: "ATM_SELL", label: "Sell ATM (Write)", desc: "BUY → write ATM Put (bullish). SELL → write ATM Call. Needs margin." },
];

const DEFAULT_OPTIONS = {
  enabled: false,
  underlying: "NIFTY",
  strike_mode: "ATM_BUY",
  otm_points: 100,
  lots: 1,
  expiry_offset: 0,
};

export default function VisualBuilder() {
  const [name, setName] = useState("RSI Reversal");
  const [symbol, setSymbol] = useState("RELIANCE");
  const [conditions, setConditions] = useState([
    { _id: "c0", indicator: "RSI", op: "<", value: "30" },
  ]);
  const [action, setAction] = useState({ side: "BUY", qty: 10 });
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [preview, setPreview] = useState(null);

  const add = () => setConditions([...conditions, { _id: `c${Date.now()}-${Math.floor(Math.random() * 1000)}`, indicator: "Price", op: ">", value: "100" }]);
  const remove = (i) => setConditions(conditions.filter((_, idx) => idx !== i));
  const update = (i, key, v) => setConditions(conditions.map((c, idx) => (idx === i ? { ...c, [key]: v } : c)));

  // Live preview of which option contract would be picked
  useEffect(() => {
    if (!options.enabled) {
      setPreview(null);
      return;
    }
    const params = new URLSearchParams({
      underlying: options.underlying,
      strike_mode: options.strike_mode,
      otm_points: options.otm_points,
      action: "BUY",
      expiry_offset: options.expiry_offset,
    });
    api.get(`/options/preview?${params}`).then((r) => setPreview(r.data)).catch(() => setPreview(null));
  }, [options]);

  const save = async () => {
    try {
      const py = generatePython(conditions, action);
      await api.post("/strategies", {
        name,
        description: options.enabled
          ? `Options: ${options.underlying} ${STRIKE_MODES.find((m) => m.id === options.strike_mode)?.label} · ${options.lots} lot${options.lots > 1 ? "s" : ""}`
          : `Visual: ${conditions.length} conditions on ${symbol}`,
        kind: "visual",
        visual_config: { symbol, conditions, action, options },
        python_code: py,
      });
      toast.success("Strategy saved");
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Save failed");
    }
  };

  return (
    <div className="space-y-4 max-w-5xl" data-testid="visual-page">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// NO-CODE</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1 flex items-center gap-3"><Blocks size={24} className="text-[var(--qd-accent)]" /> Visual Builder</h1>
        </div>
        <button onClick={save} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white text-xs font-mono uppercase px-4 py-2 rounded-sm flex items-center gap-2" data-testid="save-visual-btn"><Save size={14} /> Save Strategy</button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Field label="Strategy Name" value={name} onChange={setName} testid="visual-name" />
        <Field label="Symbol (for equity mode)" value={symbol} onChange={(v) => setSymbol(v.toUpperCase())} testid="visual-symbol" />
      </div>

      {/* OPTIONS MODE */}
      <div className="qd-card p-5" data-testid="options-card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-head text-lg text-white flex items-center gap-2">
            <TrendingUp size={18} className="text-[var(--qd-accent)]" />
            Options Mode
            <span className="font-mono text-[10px] text-[var(--qd-text-3)] uppercase tracking-widest ml-2">// NIFTY / BANKNIFTY / SENSEX</span>
          </h2>
          <label className="flex items-center gap-2 cursor-pointer" data-testid="options-toggle-label">
            <input
              type="checkbox"
              checked={options.enabled}
              onChange={(e) => setOptions({ ...options, enabled: e.target.checked })}
              className="w-4 h-4 accent-[var(--qd-accent)]"
              data-testid="options-toggle"
            />
            <span className="font-mono text-xs uppercase tracking-wider text-[var(--qd-text-2)]">
              {options.enabled ? "Trading OPTIONS" : "Trading EQUITY"}
            </span>
          </label>
        </div>

        {options.enabled && (
          <div className="space-y-4">
            {/* Underlying */}
            <div>
              <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] block mb-1">Underlying</label>
              <div className="flex gap-2 flex-wrap">
                {UNDERLYINGS.map((u) => (
                  <button
                    key={u}
                    onClick={() => setOptions({ ...options, underlying: u })}
                    className={`px-4 py-2 font-mono text-xs uppercase rounded-sm border ${
                      options.underlying === u
                        ? "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-white"
                        : "border-[var(--qd-border)] text-[var(--qd-text-2)] hover:border-white hover:text-white"
                    }`}
                    data-testid={`underlying-${u}`}
                  >
                    {u}
                  </button>
                ))}
              </div>
            </div>

            {/* Strike mode */}
            <div>
              <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] block mb-1">Strike Selection</label>
              <div className="space-y-2">
                {STRIKE_MODES.map((m) => (
                  <label
                    key={m.id}
                    className={`block p-3 border rounded-sm cursor-pointer ${
                      options.strike_mode === m.id
                        ? "border-[var(--qd-accent)] bg-[rgba(0,122,255,0.06)]"
                        : "border-[var(--qd-border)] hover:border-white"
                    }`}
                    data-testid={`strike-mode-${m.id}`}
                  >
                    <input
                      type="radio"
                      name="strike-mode"
                      checked={options.strike_mode === m.id}
                      onChange={() => setOptions({ ...options, strike_mode: m.id })}
                      className="mr-2 accent-[var(--qd-accent)]"
                    />
                    <span className="font-mono text-sm text-white">{m.label}</span>
                    <span className="block ml-6 text-xs text-[var(--qd-text-2)] mt-0.5">{m.desc}</span>
                  </label>
                ))}
              </div>
            </div>

            {/* OTM points (only for OTM mode) */}
            {options.strike_mode === "OTM_BUY" && (
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] block mb-1">OTM Distance (points from ATM)</label>
                <input
                  type="number"
                  value={options.otm_points}
                  onChange={(e) => setOptions({ ...options, otm_points: +e.target.value })}
                  className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono w-32 rounded-sm"
                  data-testid="otm-points-input"
                />
              </div>
            )}

            {/* Lots & expiry */}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] block mb-1">Lots per Trade</label>
                <input
                  type="number"
                  min="1"
                  value={options.lots}
                  onChange={(e) => setOptions({ ...options, lots: Math.max(1, +e.target.value) })}
                  className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono w-full rounded-sm"
                  data-testid="options-lots"
                />
              </div>
              <div>
                <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] block mb-1">Expiry</label>
                <select
                  value={options.expiry_offset}
                  onChange={(e) => setOptions({ ...options, expiry_offset: +e.target.value })}
                  className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono w-full rounded-sm"
                  data-testid="options-expiry"
                >
                  <option value="0">Nearest weekly</option>
                  <option value="1">Next weekly</option>
                  <option value="2">2 weeks out</option>
                </select>
              </div>
            </div>

            {/* Preview box */}
            <div className="bg-[var(--qd-bg)] border border-[var(--qd-border)] rounded-sm p-3" data-testid="options-preview">
              <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">// PREVIEW — what will trade right now</div>
              {!preview ? (
                <div className="text-xs text-[var(--qd-text-2)] font-mono">Loading preview…</div>
              ) : preview.available ? (
                <div className="text-xs font-mono space-y-0.5">
                  <div className="text-white"><span className="text-[var(--qd-accent)]">{preview.tradingsymbol}</span> @ Strike <span className="text-white">{preview.strike}</span> · {preview.option_type}</div>
                  <div className="text-[var(--qd-text-2)]">Lot size {preview.lot_size} × {options.lots} lot{options.lots > 1 ? "s" : ""} = {preview.lot_size * options.lots} qty · Exchange {preview.exchange}</div>
                  <div className="text-[var(--qd-text-2)]">Spot ₹{preview.spot?.toFixed(2)} · ATM strike {preview.atm_strike} · Expires {preview.expiry}</div>
                  {preview.note && <div className="text-[var(--qd-warn)]">⚠ {preview.note}</div>}
                </div>
              ) : (
                <div className="text-xs font-mono space-y-0.5">
                  <div className="text-[var(--qd-warn)]">⚠ {preview.reason}</div>
                  {preview.lot_size && <div className="text-[var(--qd-text-2)]">Lot size: <span className="text-white">{preview.lot_size}</span> · Strike interval: {preview.strike_interval} · Exchange: {preview.exchange}</div>}
                </div>
              )}
            </div>

            <div className="bg-[rgba(255,159,10,0.08)] border border-[var(--qd-warn)] rounded-sm p-3 text-xs font-mono text-[var(--qd-text-2)]">
              <span className="text-[var(--qd-warn)]">⚠ Options are leveraged.</span> Test in PAPER mode first. Live option orders use exchange <span className="text-white">NFO</span> (NIFTY/BANKNIFTY) or <span className="text-white">BFO</span> (SENSEX) with product <span className="text-white">NRML</span>.
            </div>
          </div>
        )}
      </div>

      {/* IF block */}
      <div className="qd-card p-5" data-testid="if-block">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-head text-lg text-white flex items-center gap-2">
            <span className="font-mono bg-[var(--qd-accent)] text-white text-xs px-2 py-0.5">IF</span>
            All conditions match
          </h2>
          <button onClick={add} className="text-xs font-mono uppercase text-[var(--qd-accent)] hover:text-[var(--qd-accent-hover)] flex items-center gap-1" data-testid="add-condition"><Plus size={14} /> Add</button>
        </div>
        <div className="space-y-2">
          {conditions.map((c, i) => (
            <div key={c._id} className="flex items-center gap-2 bg-[var(--qd-bg)] border border-[var(--qd-border)] p-2 rounded-sm" data-testid={`cond-${i}`}>
              <Select value={c.indicator} options={INDICATORS} onChange={(v) => update(i, "indicator", v)} />
              <Select value={c.op} options={OPS} onChange={(v) => update(i, "op", v)} />
              <input value={c.value} onChange={(e) => update(i, "value", e.target.value)} className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-1.5 text-sm text-white font-mono w-24 rounded-sm" />
              {conditions.length > 1 && (
                <button onClick={() => remove(i)} className="text-[var(--qd-loss)] hover:opacity-70 ml-auto"><X size={14} /></button>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="flex justify-center"><ArrowRight className="text-[var(--qd-text-3)]" /></div>

      {/* THEN block */}
      <div className="qd-card p-5" data-testid="then-block">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3">
          <span className={`font-mono text-xs px-2 py-0.5 text-white ${action.side === "BUY" ? "bg-[var(--qd-profit)] text-black" : "bg-[var(--qd-loss)]"}`}>THEN</span>
          Execute action
        </h2>
        <div className="flex items-center gap-2 bg-[var(--qd-bg)] border border-[var(--qd-border)] p-2 rounded-sm">
          <Select value={action.side} options={ACTIONS} onChange={(v) => setAction({ ...action, side: v })} />
          {!options.enabled && (
            <>
              <input type="number" value={action.qty} onChange={(e) => setAction({ ...action, qty: +e.target.value })} className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-1.5 text-sm text-white font-mono w-24 rounded-sm" />
              <span className="font-mono text-xs text-[var(--qd-text-2)]">qty of {symbol}</span>
            </>
          )}
          {options.enabled && (
            <span className="font-mono text-xs text-[var(--qd-text-2)]">{options.lots} lot{options.lots > 1 ? "s" : ""} of {options.underlying} option (see preview above)</span>
          )}
        </div>
      </div>

      <div className="qd-card p-5">
        <h3 className="font-head text-base text-white mb-2">Generated Python</h3>
        <pre className="bg-black text-[#9bbd9b] font-mono text-xs p-3 overflow-x-auto rounded-sm">{generatePython(conditions, action)}</pre>
      </div>
    </div>
  );
}

const Field = ({ label, value, onChange, testid }) => (
  <div className="qd-card p-3">
    <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</label>
    <input data-testid={testid} value={value} onChange={(e) => onChange(e.target.value)} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" />
  </div>
);

const Select = ({ value, options, onChange }) => (
  <select value={value} onChange={(e) => onChange(e.target.value)} className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-1.5 text-sm text-white font-mono rounded-sm">
    {options.map((o) => <option key={o} value={o}>{o}</option>)}
  </select>
);

function generatePython(conds, action) {
  const checks = conds.map((c) => `  # ${c.indicator} ${c.op} ${c.value}`).join("\n");
  return `# Auto-generated from Visual Builder
def run(data):
    closes = [d['close'] for d in data]
    signals = []
    for i in range(20, len(closes)):
${checks}
        if closes[i] > closes[i-1]:  # demo trigger
            signals.append({'date': data[i]['date'], 'action': '${action.side}'})
    return signals
`;
}
