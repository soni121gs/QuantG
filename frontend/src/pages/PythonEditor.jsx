import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { Play, Save, Code2, TrendingUp, SlidersHorizontal } from "lucide-react";
import { LineChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { toast } from "sonner";

const STARTER = `# QuantG Python Strategy
# Define a run(data) function. data = list of {date, close}.
# Return a list of {date, action: 'BUY' | 'SELL'}.
# When Options Mode is ON, data = underlying spot history (NIFTY/BANKNIFTY/SENSEX).

def run(data):
    short, long = 5, 20
    closes = [d['close'] for d in data]
    signals = []
    for i in range(long, len(closes)):
        s_avg = sum(closes[i-short:i]) / short
        l_avg = sum(closes[i-long:i]) / long
        prev_s = sum(closes[i-short-1:i-1]) / short
        prev_l = sum(closes[i-long-1:i-1]) / long
        if prev_s <= prev_l and s_avg > l_avg:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif prev_s >= prev_l and s_avg < l_avg:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
`;

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"];
const STRIKE_MODES = [
  { id: "ATM_BUY", label: "Buy ATM" },
  { id: "OTM_BUY", label: "Buy OTM" },
  { id: "ATM_SELL", label: "Sell ATM (Write)" },
];
const DEFAULT_OPTIONS = {
  enabled: false,
  underlying: "NIFTY",
  strike_mode: "ATM_BUY",
  otm_points: 100,
  lots: 1,
  expiry_offset: 0,
};

const SMA_ASSIGN_RE = /(short\s*,\s*long(?:\s*,\s*signals)?\s*=\s*)(\d+)\s*,\s*(\d+)(\s*,\s*\[\])?/;

function analyzeStrategyCode(code, options, symbol) {
  const sma = code.match(SMA_ASSIGN_RE);
  const hasCrossAbove = /prev_s\s*<=\s*prev_l[\s\S]{0,160}s_avg\s*>\s*l_avg/.test(code);
  const hasCrossBelow = /prev_s\s*>=\s*prev_l[\s\S]{0,160}s_avg\s*<\s*l_avg/.test(code);
  const hasRsi = /\brsi\b/i.test(code);

  const execution = options.enabled
    ? optionExecutionText(options)
    : `Trade ${symbol || "selected equity"} directly with the strategy signal.`;

  if (sma && hasCrossAbove && hasCrossBelow) {
    const shortWindow = Number(sma[2]);
    const longWindow = Number(sma[3]);
    return {
      type: "sma",
      title: "Moving-average crossover",
      editable: true,
      shortWindow,
      longWindow,
      signal: `Track ${shortWindow}-period SMA versus ${longWindow}-period SMA on closing price.`,
      buyRule: `BUY when the ${shortWindow}-period SMA crosses above the ${longWindow}-period SMA.`,
      sellRule: `SELL when the ${shortWindow}-period SMA crosses below the ${longWindow}-period SMA.`,
      execution,
    };
  }

  if (hasRsi) {
    return {
      type: "rsi",
      title: "RSI-based custom strategy",
      editable: false,
      signal: "Uses RSI logic from the pasted Python code.",
      buyRule: "BUY/SELL thresholds were found in custom code. Edit the Python for exact values.",
      sellRule: "This translator can summarize RSI code now; editable RSI controls can be added next.",
      execution,
    };
  }

  return {
    type: "custom",
    title: "Custom Python strategy",
    editable: false,
    signal: "Runs your custom run(data) function on candle history.",
    buyRule: "The app acts on signals returned as {'action': 'BUY'} from Python.",
    sellRule: "The app acts on signals returned as {'action': 'SELL'} from Python.",
    execution,
  };
}

function optionExecutionText(options) {
  const lotText = `${options.lots || 1} lot${Number(options.lots) === 1 ? "" : "s"}`;
  if (options.strike_mode === "OTM_BUY") {
    return `For ${options.underlying}, BUY signal buys an OTM Call and SELL signal buys an OTM Put, ${options.otm_points || 0} points from ATM, using ${lotText}.`;
  }
  if (options.strike_mode === "ATM_SELL") {
    return `For ${options.underlying}, BUY signal writes an ATM Put and SELL signal writes an ATM Call, using ${lotText}.`;
  }
  return `For ${options.underlying}, BUY signal buys an ATM Call and SELL signal buys an ATM Put, using ${lotText}.`;
}

function updateSmaWindowInCode(code, key, value) {
  const safe = Math.max(1, Number(value) || 1);
  const match = code.match(SMA_ASSIGN_RE);
  if (!match) return code;
  const currentShort = Number(match[2]);
  const currentLong = Number(match[3]);
  const nextShort = key === "short" ? safe : currentShort;
  const nextLong = key === "long" ? safe : currentLong;
  return code.replace(SMA_ASSIGN_RE, (_full, prefix, _short, _long, suffix = "") => `${prefix}${nextShort}, ${nextLong}${suffix}`);
}

function formatDataSource(source) {
  if (!source) return "—";
  if (source.startsWith("zerodha-kite-5minute")) return source.includes("tick-live") ? "REAL ticker 5m" : "REAL Kite 5m";
  if (source.startsWith("zerodha-kite-day")) return "REAL Kite day";
  if (source.startsWith("mock-5minute")) return "MOCK 5m";
  if (source.startsWith("mock-day")) return "MOCK day";
  return source;
}

export default function PythonEditor() {
  const [params] = useSearchParams();
  const id = params.get("id");
  const [name, setName] = useState("SMA Crossover");
  const [description, setDescription] = useState("Simple 5/20 moving average crossover.");
  const [code, setCode] = useState(STARTER);
  const [symbol, setSymbol] = useState("RELIANCE");
  const [days, setDays] = useState(60);
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [savedId, setSavedId] = useState(id || null);
  const [engine, setEngine] = useState("local");

  useEffect(() => {
    if (!id) return;
    api.get(`/strategies/${id}`).then((r) => {
      setName(r.data.name);
      setDescription(r.data.description || "");
      setCode(r.data.python_code || STARTER);
      setSavedId(r.data.id);
      const vc = r.data.visual_config || {};
      if (vc.symbol) setSymbol(vc.symbol);
      if (vc.options) setOptions({ ...DEFAULT_OPTIONS, ...vc.options });
    });
    // STARTER is module-level constant — safe to omit
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const lines = useMemo(() => code.split("\n").length, [code]);
  const tradePlan = useMemo(() => analyzeStrategyCode(code, options, symbol), [code, options, symbol]);

  const run = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.post("/strategies/backtest", {
        python_code: code,
        symbol: options.enabled ? options.underlying : symbol,
        days: +days,
        strategy_id: savedId,
        options: options.enabled ? options : undefined,
        engine,
      });
      setResult(r.data);
    } catch (e) {
      toast.error(e.response?.data?.detail || "Backtest failed");
    } finally { setBusy(false); }
  }, [code, symbol, days, savedId, options, engine]);

  const save = async () => {
    try {
      const visual_config = { symbol, options };
      if (savedId) {
        await api.put(`/strategies/${savedId}`, { name, description, kind: "python", python_code: code, visual_config, status: "draft" });
      } else {
        const r = await api.post("/strategies", { name, description, kind: "python", python_code: code, visual_config });
        setSavedId(r.data.id);
      }
      toast.success("Saved");
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    }
  };

  return (
    <div className="space-y-4" data-testid="python-page">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// PY :: STRATEGY</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1 flex items-center gap-3"><Code2 size={24} className="text-[var(--qd-accent)]" /> Python Editor</h1>
        </div>
        <div className="flex gap-2">
          <button onClick={save} className="border border-[var(--qd-border)] hover:border-white text-white text-xs font-mono uppercase px-4 py-2 rounded-sm flex items-center gap-2" data-testid="save-strategy-btn"><Save size={14} /> Save</button>
          <button onClick={run} disabled={busy} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white text-xs font-mono uppercase px-4 py-2 rounded-sm flex items-center gap-2" data-testid="run-backtest-btn">
            <Play size={14} /> {busy ? "Running..." : "Run Backtest"}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <input
          value={name} onChange={(e) => setName(e.target.value)}
          className="qd-card px-3 py-2 bg-[var(--qd-bg)] text-sm text-white font-mono"
          placeholder="Strategy name"
          data-testid="strategy-name-input"
        />
        <input
          value={description} onChange={(e) => setDescription(e.target.value)}
          className="qd-card px-3 py-2 bg-[var(--qd-bg)] text-sm text-white font-mono lg:col-span-2"
          placeholder="Description"
          data-testid="strategy-desc-input"
        />
      </div>

      {/* OPTIONS MODE — same as Visual Builder */}
      <div className="qd-card p-4" data-testid="options-card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-head text-base text-white flex items-center gap-2">
            <TrendingUp size={16} className="text-[var(--qd-accent)]" /> Options Mode
            <span className="font-mono text-[10px] text-[var(--qd-text-3)] uppercase tracking-widest ml-2">// NIFTY / BANKNIFTY / SENSEX</span>
          </h2>
          <label className="flex items-center gap-2 cursor-pointer">
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
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
            <div>
              <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">Underlying</label>
              <select
                value={options.underlying}
                onChange={(e) => setOptions({ ...options, underlying: e.target.value })}
                className="w-full bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-2 text-sm text-white font-mono rounded-sm"
                data-testid="options-underlying"
              >
                {UNDERLYINGS.map((u) => <option key={u} value={u}>{u}</option>)}
              </select>
            </div>
            <div>
              <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">Strike Mode</label>
              <select
                value={options.strike_mode}
                onChange={(e) => setOptions({ ...options, strike_mode: e.target.value })}
                className="w-full bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-2 text-sm text-white font-mono rounded-sm"
                data-testid="options-strike-mode"
              >
                {STRIKE_MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </div>
            <div>
              <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">Lots</label>
              <input
                type="number"
                min="1"
                value={options.lots}
                onChange={(e) => setOptions({ ...options, lots: Math.max(1, +e.target.value) })}
                className="w-full bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-2 text-sm text-white font-mono rounded-sm"
                data-testid="options-lots"
              />
            </div>
            <div>
              <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">Expiry</label>
              <select
                value={options.expiry_offset}
                onChange={(e) => setOptions({ ...options, expiry_offset: +e.target.value })}
                className="w-full bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-2 text-sm text-white font-mono rounded-sm"
                data-testid="options-expiry"
              >
                <option value="0">Nearest weekly</option>
                <option value="1">Next weekly</option>
                <option value="2">2 weeks out</option>
              </select>
            </div>
            {options.strike_mode === "OTM_BUY" && (
              <div className="md:col-span-4">
                <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">OTM Distance (points)</label>
                <input
                  type="number"
                  value={options.otm_points}
                  onChange={(e) => setOptions({ ...options, otm_points: +e.target.value })}
                  className="bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-2 text-sm text-white font-mono w-32 rounded-sm"
                  data-testid="options-otm-points"
                />
              </div>
            )}
            <div className="md:col-span-4 bg-[rgba(255,159,10,0.06)] border border-[var(--qd-warn)] rounded-sm p-2 text-[11px] font-mono text-[var(--qd-text-2)]">
              <span className="text-[var(--qd-warn)]">⚠</span> Backtest runs on the <span className="text-white">{options.underlying} spot</span> history. PnL simulation uses an option-premium proxy (~2% of spot move). Live trading uses real option premiums from Kite.
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 qd-card flex flex-col" data-testid="code-editor">
          <div className="flex items-center justify-between border-b border-[var(--qd-border)] px-3 py-2">
            <span className="font-mono text-xs text-[var(--qd-text-2)]">strategy.py</span>
            <span className="font-mono text-[10px] text-[var(--qd-text-3)]">{lines} lines</span>
          </div>
          <div className="flex bg-black">
            <pre className="font-mono text-xs text-[var(--qd-text-3)] py-3 px-2 select-none border-r border-[var(--qd-border)] min-w-[3rem] text-right leading-[1.5]">
              {Array.from({ length: lines }, (_, i) => i + 1).join("\n")}
            </pre>
            <textarea
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="flex-1 bg-black text-[#e5e5e5] font-mono text-xs leading-[1.5] py-3 px-3 outline-none resize-none min-h-[420px]"
              spellCheck={false}
              data-testid="code-textarea"
            />
          </div>
        </div>

        <div className="space-y-4">
          <TradePlanCard
            plan={tradePlan}
            onSmaChange={(key, value) => setCode((current) => updateSmaWindowInCode(current, key, value))}
          />

          <div className="qd-card p-4">
            <h3 className="font-head text-base text-white mb-3">Backtest Config</h3>
            {!options.enabled ? (
              <>
                <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Symbol (Equity)</label>
                <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} className="w-full mt-1 mb-3 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="symbol-input" />
              </>
            ) : (
              <div className="mb-3 text-[11px] font-mono text-[var(--qd-text-2)]">
                Underlying: <span className="text-white">{options.underlying}</span> · {options.lots} lot{options.lots > 1 ? "s" : ""} · {options.strike_mode}
              </div>
            )}
            <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Days</label>
            <input type="number" value={days} onChange={(e) => setDays(e.target.value)} className="w-full mt-1 mb-3 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="days-input" />
            <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">Engine</label>
            <select value={engine} onChange={(e) => setEngine(e.target.value)} className="w-full bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="engine-select">
              <option value="local">Local Simulator (Fast)</option>
              <option value="backtrader">Backtrader (Advanced)</option>
            </select>
          </div>

          {result && (
            <div className="qd-card p-4" data-testid="backtest-summary">
              <h3 className="font-head text-base text-white mb-3">
                Results {result.engine && <span className="font-mono text-[10px] uppercase text-[var(--qd-accent)] ml-1">// {result.engine.toUpperCase()}</span>} {result.mode === "options" && <span className="font-mono text-[10px] uppercase text-[var(--qd-accent)] ml-1">// OPTIONS</span>}
              </h3>
              <SummaryRow label="Total PnL" value={`₹${result.summary.total_pnl.toLocaleString("en-IN")}`} tone={result.summary.total_pnl >= 0 ? "p" : "l"} />
              <SummaryRow label="Return" value={`${result.summary.return_pct}%`} tone={result.summary.return_pct >= 0 ? "p" : "l"} />
              <SummaryRow label="Trades" value={result.summary.trades} />
              <SummaryRow label="Win Rate" value={`${result.summary.win_rate}%`} />
              <SummaryRow label="Wins / Losses" value={`${result.summary.wins} / ${result.summary.losses}`} />
              <SummaryRow label="Data" value={formatDataSource(result.data_source)} tone={result.data_live ? "p" : "l"} />
            </div>
          )}
        </div>
      </div>

      {result && (
        <div className="qd-card p-4" data-testid="equity-chart">
          <h3 className="font-head text-base text-white mb-2">Equity Curve</h3>
          <div className="h-72">
            <ResponsiveContainer>
              <LineChart data={result.equity_curve}>
                <XAxis dataKey="date" stroke="#666" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} tickFormatter={(d) => d.slice(5)} />
                <YAxis stroke="#666" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} domain={["dataMin - 1000", "dataMax + 1000"]} />
                <Tooltip contentStyle={{ background: "#121212", border: "1px solid #2a2a2e", borderRadius: 2, fontFamily: "JetBrains Mono", fontSize: 11 }} />
                <Line type="monotone" dataKey="equity" stroke="#00E676" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}

const SummaryRow = ({ label, value, tone }) => (
  <div className="flex justify-between py-1.5 border-b border-[var(--qd-border)] last:border-b-0">
    <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</span>
    <span className={`font-mono text-sm ${tone === "p" ? "text-[var(--qd-profit)]" : tone === "l" ? "text-[var(--qd-loss)]" : "text-white"}`}>{value}</span>
  </div>
);

const TradePlanCard = ({ plan, onSmaChange }) => (
  <div className="qd-card p-4" data-testid="market-language-card">
    <div className="flex items-center justify-between gap-2 mb-3">
      <h3 className="font-head text-base text-white flex items-center gap-2">
        <SlidersHorizontal size={16} className="text-[var(--qd-accent)]" />
        Market Language
      </h3>
      <span className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">{plan.title}</span>
    </div>

    {plan.editable && (
      <div className="grid grid-cols-2 gap-2 mb-3">
        <PlainInput label="Fast SMA" value={plan.shortWindow} onChange={(v) => onSmaChange("short", v)} testid="plain-fast-sma" />
        <PlainInput label="Slow SMA" value={plan.longWindow} onChange={(v) => onSmaChange("long", v)} testid="plain-slow-sma" />
      </div>
    )}

    <div className="space-y-2">
      <PlanLine label="Signal" value={plan.signal} />
      <PlanLine label="Entry" value={plan.buyRule} tone="p" />
      <PlanLine label="Exit" value={plan.sellRule} tone="l" />
      <PlanLine label="Execution" value={plan.execution} />
    </div>

    {!plan.editable && (
      <div className="mt-3 border border-dashed border-[var(--qd-border)] p-2 text-[11px] font-mono text-[var(--qd-text-2)]">
        Editable plain-language controls are available for recognized templates. This code still runs normally.
      </div>
    )}
  </div>
);

const PlainInput = ({ label, value, onChange, testid }) => (
  <label>
    <span className="block font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)] mb-1">{label}</span>
    <input
      type="number"
      min="1"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full bg-[var(--qd-bg)] border border-[var(--qd-border)] px-2 py-2 text-sm text-white font-mono rounded-sm"
      data-testid={testid}
    />
  </label>
);

const PlanLine = ({ label, value, tone }) => (
  <div className="border-b border-[var(--qd-border)] last:border-b-0 pb-2 last:pb-0">
    <div className="font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className={`mt-0.5 text-xs leading-relaxed ${
      tone === "p" ? "text-[var(--qd-profit)]" : tone === "l" ? "text-[var(--qd-loss)]" : "text-[var(--qd-text-2)]"
    }`}>{value}</div>
  </div>
);
