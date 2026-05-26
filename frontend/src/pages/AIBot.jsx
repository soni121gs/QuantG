import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { AlertCircle, Bot, Send, Sparkles, User } from "lucide-react";

const SESSION = "default";
const SUGGESTIONS = [
  "Check my Upstox and market data status",
  "Explain my current open positions and risk",
  "Find stuck or rejected orders",
  "Why did my active strategies trade or not trade?",
];
const MODES = [
  { id: "agent", label: "Ask Agent" },
  { id: "brief", label: "Market Brief" },
];
const BRIEF_PROMPTS = [
  "Create a short market brief for NIFTY, BANKNIFTY, SENSEX, and MCX commodities.",
  "Summarize risks before placing live orders today.",
  "Build a checklist for commodity trades using Upstox instrument keys.",
];

export default function AIBot() {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("agent");
  const [aiStatus, setAiStatus] = useState(null);
  const [marketAnalysis, setMarketAnalysis] = useState(null);
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    api.get(`/ai/chat/${SESSION}`).then((r) => setMessages(r.data)).catch(() => {});
    api.get("/ai/status").then((r) => setAiStatus(r.data)).catch(() => {});
  }, []);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const send = async (msg) => {
    const content = (msg ?? text).trim();
    if (!content || busy) return;
    setBusy(true);
    setText("");
    const cid = `c-${Date.now()}`;
    setMessages((m) => [...m, { id: cid, role: "user", content }]);
    try {
      const r = await api.post("/agent/chat", { session_id: SESSION, message: content });
      setMessages((m) => [...m, { id: `a-${Date.now()}`, role: "assistant", content: r.data.content, tools_used: r.data.tools_used, unavailable: r.data.unavailable, read_only: true }]);
    } catch (e) {
      setMessages((m) => [...m, { id: `err-${Date.now()}`, role: "assistant", content: `Error: ${e.response?.data?.detail || e.message}` }]);
    } finally { setBusy(false); }
  };

  const runMarketAnalysis = async () => {
    setAnalysisBusy(true);
    try {
      const r = await api.get("/ai/market-analysis");
      setMarketAnalysis(r.data);
    } catch (e) {
      setMarketAnalysis({ content: `Error: ${e.response?.data?.detail || e.message}` });
    } finally {
      setAnalysisBusy(false);
    }
  };

  return (
    <div className="flex h-[calc(100vh-130px)] flex-col gap-4" data-testid="ai-bot-page">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// READ-ONLY GEMINI AGENT</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1 flex items-center gap-3"><Bot size={26} className="text-[var(--qd-accent)]" /> Ask QuantG Agent</h1>
          <p className="text-xs text-[var(--qd-text-2)] mt-1">Read-only answers from execution state, orders, positions, strategies, Upstox, market data, logs, and risk.</p>
          <div className="mt-2 inline-flex items-center gap-2 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-2)]">
            <span className={(aiStatus?.provider || "").includes("google") ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"}>
              {aiStatus?.provider || "checking"}
            </span>
            <span>{aiStatus?.model || "model loading"}</span>
            <span className="text-[var(--qd-text-3)]">read-only</span>
          </div>
        </div>
        <div className="flex rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-1" data-testid="ai-mode-tabs">
          {MODES.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setMode(item.id)}
              className={`px-3 py-2 font-mono text-[10px] uppercase tracking-wider rounded ${mode === item.id ? "bg-[var(--qd-accent)] text-white" : "text-[var(--qd-text-2)] hover:text-white"}`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {mode === "brief" && (
        <div className="qd-card p-4">
          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
            <div>
              <h2 className="font-head text-lg font-semibold text-white">Market Brief</h2>
              <p className="mt-1 text-xs text-[var(--qd-text-2)]">Gemini reads live strategy scores, index context, and MCX crude oil/natural gas feed snapshots.</p>
            </div>
            <button
              type="button"
              onClick={runMarketAnalysis}
              disabled={analysisBusy}
              className="rounded bg-[var(--qd-accent)] px-3 py-2 font-mono text-xs uppercase tracking-wider text-white disabled:opacity-50"
            >
              {analysisBusy ? "Analyzing" : "Run Gemini Analysis"}
            </button>
          </div>
          {marketAnalysis?.content && (
            <div className="mt-3 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-3 text-sm leading-relaxed text-[var(--qd-text-2)] whitespace-pre-wrap">
              {marketAnalysis.content}
            </div>
          )}
          <div className="mt-4 grid grid-cols-1 gap-2 md:grid-cols-3">
            {BRIEF_PROMPTS.map((prompt) => (
              <button
                key={prompt}
                type="button"
                onClick={() => {
                  setMode("agent");
                  send(prompt);
                }}
                className="rounded border border-[var(--qd-border)] p-3 text-left font-mono text-xs text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-white"
              >
                {prompt}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex-1 qd-card flex flex-col min-h-0">
        <div className="flex-1 overflow-y-auto p-4 space-y-4" data-testid="messages">
          {messages.length === 0 && (
            <div className="text-center py-10">
              <Sparkles className="mx-auto text-[var(--qd-accent)] mb-3" />
              <p className="font-mono text-sm text-[var(--qd-text-2)] mb-6">How can I help your trading today?</p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2 max-w-2xl mx-auto">
                {SUGGESTIONS.map((s) => (
                  <button key={s} onClick={() => send(s)} className="text-left text-xs font-mono text-[var(--qd-text-2)] border border-[var(--qd-border)] hover:border-[var(--qd-accent)] hover:text-white p-3 transition-colors rounded-sm" data-testid={`suggestion-${s.slice(0, 10)}`}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <Message key={m.id || `m-${i}`} m={m} />
          ))}
          {busy && <Message key="typing" m={{ role: "assistant", content: "..." }} />}
          <div ref={endRef} />
        </div>

        <div className="border-t border-[var(--qd-border)] p-3 flex items-center gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder="Ask QuantG Agent about app state, orders, positions, broker, feed, logs, or risk..."
            className="flex-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2.5 text-sm text-white font-mono rounded-sm"
            data-testid="ai-input"
          />
          <button onClick={() => send()} disabled={busy || !text.trim()} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white px-4 py-2.5 rounded-sm" data-testid="ai-send-btn">
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}

const Message = ({ m }) => {
  const isUser = m.role === "user";
  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : ""}`}>
      {!isUser && (
        <div className="w-7 h-7 rounded-sm bg-[var(--qd-accent)] flex items-center justify-center flex-shrink-0">
          <Bot size={14} className="text-white" />
        </div>
      )}
      <div className={`max-w-[75%] px-4 py-2.5 rounded-sm ${isUser ? "bg-[var(--qd-accent)] text-white" : "bg-[var(--qd-surface-2)] border border-[var(--qd-border)] text-white"}`}>
        <pre className="whitespace-pre-wrap font-mono text-sm leading-relaxed">{m.content}</pre>
        {!isUser && m.tools_used?.length > 0 && (
          <div className="mt-3 border-t border-[var(--qd-border)] pt-2">
            <div className="mb-1 font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">Read-only tools used</div>
            <div className="flex flex-wrap gap-1">
              {m.tools_used.map((tool) => (
                <span
                  key={tool.name}
                  className={`rounded border px-2 py-1 font-mono text-[10px] ${tool.status === "ok" ? "border-[var(--qd-border)] text-[var(--qd-text-2)]" : "border-[var(--qd-loss)] text-[var(--qd-loss)]"}`}
                  title={tool.error || tool.name}
                >
                  {tool.name}
                </span>
              ))}
            </div>
          </div>
        )}
        {!isUser && m.unavailable?.length > 0 && (
          <div className="mt-2 flex gap-2 rounded border border-[var(--qd-loss)]/60 bg-[var(--qd-bg)] p-2 text-xs text-[var(--qd-loss)]">
            <AlertCircle size={14} className="mt-0.5 flex-shrink-0" />
            <div>
              Some read-only data was unavailable. The answer may be incomplete.
            </div>
          </div>
        )}
      </div>
      {isUser && (
        <div className="w-7 h-7 rounded-sm bg-[var(--qd-surface-2)] border border-[var(--qd-border)] flex items-center justify-center flex-shrink-0">
          <User size={14} className="text-white" />
        </div>
      )}
    </div>
  );
};
