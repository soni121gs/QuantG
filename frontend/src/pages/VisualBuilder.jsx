import React, { useState } from "react";
import { api } from "../lib/api";
import { Plus, X, Save, Blocks, ArrowRight } from "lucide-react";

const INDICATORS = ["RSI", "SMA(5)", "SMA(20)", "EMA(50)", "MACD", "Price"];
const OPS = [">", "<", "==", "Crosses Above", "Crosses Below"];
const ACTIONS = ["BUY", "SELL", "EXIT"];

export default function VisualBuilder() {
  const [name, setName] = useState("RSI Reversal");
  const [symbol, setSymbol] = useState("NIFTY");
  const [conditions, setConditions] = useState([
    { _id: "c0", indicator: "RSI", op: "<", value: "30" },
  ]);
  const [action, setAction] = useState({ side: "BUY", qty: 10 });
  const [msg, setMsg] = useState("");

  const add = () => setConditions([...conditions, { _id: `c${Date.now()}-${Math.floor(Math.random() * 1000)}`, indicator: "Price", op: ">", value: "100" }]);
  const remove = (i) => setConditions(conditions.filter((_, idx) => idx !== i));
  const update = (i, key, v) => setConditions(conditions.map((c, idx) => (idx === i ? { ...c, [key]: v } : c)));

  const save = async () => {
    setMsg("");
    try {
      const py = generatePython(conditions, action);
      await api.post("/strategies", {
        name, description: `Visual: ${conditions.length} conditions on ${symbol}`,
        kind: "visual",
        visual_config: { symbol, conditions, action },
        python_code: py,
      });
      setMsg("✓ Saved to strategies");
    } catch (e) {
      setMsg("× Save failed");
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
        <Field label="Symbol" value={symbol} onChange={(v) => setSymbol(v.toUpperCase())} testid="visual-symbol" />
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
          <input type="number" value={action.qty} onChange={(e) => setAction({ ...action, qty: +e.target.value })} className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-1.5 text-sm text-white font-mono w-24 rounded-sm" />
          <span className="font-mono text-xs text-[var(--qd-text-2)]">qty of {symbol}</span>
        </div>
      </div>

      {msg && <div className="font-mono text-xs text-[var(--qd-text-2)]" data-testid="visual-msg">{msg}</div>}

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
