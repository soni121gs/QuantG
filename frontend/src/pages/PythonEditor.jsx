import React, { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { Play, Save, Code2 } from "lucide-react";
import { LineChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const STARTER = `# QuantDesk Python Strategy
# Define a run(data) function. data = list of {date, close}.
# Return a list of {date, action: 'BUY' | 'SELL'}.

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

export default function PythonEditor() {
  const [params] = useSearchParams();
  const id = params.get("id");
  const [name, setName] = useState("SMA Crossover");
  const [description, setDescription] = useState("Simple 5/20 moving average crossover.");
  const [code, setCode] = useState(STARTER);
  const [symbol, setSymbol] = useState("RELIANCE");
  const [days, setDays] = useState(60);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [savedId, setSavedId] = useState(id || null);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (!id) return;
    api.get(`/strategies/${id}`).then((r) => {
      setName(r.data.name); setDescription(r.data.description || "");
      setCode(r.data.python_code || STARTER); setSavedId(r.data.id);
    });
  }, [id]);

  const lines = useMemo(() => code.split("\n").length, [code]);

  const run = async () => {
    setBusy(true); setMsg("");
    try {
      const r = await api.post("/strategies/backtest", { python_code: code, symbol, days: +days, strategy_id: savedId });
      setResult(r.data);
    } catch (e) {
      setMsg(e.response?.data?.detail || "Backtest failed");
    } finally { setBusy(false); }
  };

  const save = async () => {
    setMsg("");
    try {
      if (savedId) {
        await api.put(`/strategies/${savedId}`, { name, description, kind: "python", python_code: code, status: "draft" });
      } else {
        const r = await api.post("/strategies", { name, description, kind: "python", python_code: code });
        setSavedId(r.data.id);
      }
      setMsg("✓ Saved");
    } catch (e) { setMsg("× Save failed"); }
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
          <div className="qd-card p-4">
            <h3 className="font-head text-base text-white mb-3">Backtest Config</h3>
            <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Symbol</label>
            <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} className="w-full mt-1 mb-3 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="symbol-input" />
            <label className="block font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Days</label>
            <input type="number" value={days} onChange={(e) => setDays(e.target.value)} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm" data-testid="days-input" />
            {msg && <div className="mt-3 font-mono text-xs text-[var(--qd-text-2)]" data-testid="bt-msg">{msg}</div>}
          </div>

          {result && (
            <div className="qd-card p-4" data-testid="backtest-summary">
              <h3 className="font-head text-base text-white mb-3">Results</h3>
              <SummaryRow label="Total PnL" value={`₹${result.summary.total_pnl.toLocaleString("en-IN")}`} tone={result.summary.total_pnl >= 0 ? "p" : "l"} />
              <SummaryRow label="Return" value={`${result.summary.return_pct}%`} tone={result.summary.return_pct >= 0 ? "p" : "l"} />
              <SummaryRow label="Trades" value={result.summary.trades} />
              <SummaryRow label="Win Rate" value={`${result.summary.win_rate}%`} />
              <SummaryRow label="Wins / Losses" value={`${result.summary.wins} / ${result.summary.losses}`} />
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
