import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { Bot, Send, Sparkles, User, Wand2 } from "lucide-react";

const SESSION = "default";
const SUGGESTIONS = [
  "Suggest a momentum strategy for NIFTY",
  "Give me a pre-market risk checklist",
  "Explain today's MCX commodity setup",
  "How to manage risk with a 1L portfolio?",
];
const MODES = [
  { id: "chat", label: "Chat" },
  { id: "agent", label: "Strategy Agent" },
  { id: "brief", label: "Market Brief" },
];
const BRIEF_PROMPTS = [
  "Create a short market brief for NIFTY, BANKNIFTY, SENSEX, and MCX commodities.",
  "Summarize risks before placing live orders today.",
  "Build a checklist for commodity trades using exact Kotak MCX symbols.",
];

export default function AIBot() {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [strategies, setStrategies] = useState([]);
  const [selectedStrategy, setSelectedStrategy] = useState("");
  const [agentText, setAgentText] = useState("");
  const [agentBusy, setAgentBusy] = useState(false);
  const [proposal, setProposal] = useState(null);
  const [mode, setMode] = useState("chat");
  const endRef = useRef(null);

  useEffect(() => {
    api.get(`/ai/chat/${SESSION}`).then((r) => setMessages(r.data)).catch(() => {});
    api.get("/strategies").then((r) => {
      setStrategies(r.data || []);
      if (r.data?.[0]) setSelectedStrategy(r.data[0].id);
    }).catch(() => {});
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
      const r = await api.post("/ai/chat", { session_id: SESSION, message: content });
      setMessages((m) => [...m, { id: `a-${Date.now()}`, role: "assistant", content: r.data.content }]);
    } catch (e) {
      setMessages((m) => [...m, { id: `err-${Date.now()}`, role: "assistant", content: `Error: ${e.response?.data?.detail || e.message}` }]);
    } finally { setBusy(false); }
  };

  const runAgent = async (apply = false) => {
    if (!selectedStrategy || !agentText.trim() || agentBusy) return;
    setAgentBusy(true);
    try {
      const r = await api.post(`/strategies/${selectedStrategy}/ai-modify`, {
        instruction: agentText.trim(),
        apply,
      });
      setProposal(r.data);
      if (apply) {
        const latest = await api.get("/strategies");
        setStrategies(latest.data || []);
      }
    } catch (e) {
      setProposal({ ok: false, error: e.response?.data?.detail || e.message });
    } finally {
      setAgentBusy(false);
    }
  };

  return (
    <div className="flex h-[calc(100vh-130px)] flex-col gap-4" data-testid="ai-bot-page">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// GOOGLE AI STUDIO</div>
          <h1 className="font-head text-3xl font-bold text-white mt-1 flex items-center gap-3"><Bot size={26} className="text-[var(--qd-accent)]" /> QuantBot</h1>
          <p className="text-xs text-[var(--qd-text-2)] mt-1">Chat, strategy editing, and market briefs are separated so the workspace stays calm.</p>
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

      {mode === "agent" && (
      <div className="qd-card p-4" data-testid="ai-agent-controls">
        <div className="mb-3">
          <h2 className="font-head text-lg font-semibold text-white">Strategy Agent</h2>
          <p className="mt-1 text-xs text-[var(--qd-text-2)]">Preview Gemini changes, inspect validation, then apply only when the proposal looks right.</p>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-[220px_1fr_auto] gap-2 items-end">
          <div>
            <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Strategy</label>
            <select value={selectedStrategy} onChange={(e) => setSelectedStrategy(e.target.value)} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-xs text-white font-mono rounded-sm">
              {strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          <div>
            <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Agent Instruction</label>
            <input
              value={agentText}
              onChange={(e) => setAgentText(e.target.value)}
              placeholder="Tighten entries, add VWAP filter, reduce false signals..."
              className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2 text-xs text-white font-mono rounded-sm"
              data-testid="agent-instruction"
            />
          </div>
          <div className="flex gap-2">
            <button onClick={() => runAgent(false)} disabled={agentBusy || !selectedStrategy || !agentText.trim()} className="border border-[var(--qd-border)] hover:border-[var(--qd-accent)] disabled:opacity-40 text-white px-3 py-2 text-xs font-mono uppercase rounded-sm flex items-center gap-2">
              <Wand2 size={14} /> Preview
            </button>
            <button onClick={() => runAgent(true)} disabled={agentBusy || !selectedStrategy || !agentText.trim()} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-40 text-white px-3 py-2 text-xs font-mono uppercase rounded-sm">
              Apply
            </button>
          </div>
        </div>
        {proposal && (
          <div className="mt-3 border-t border-[var(--qd-border)] pt-3 text-xs font-mono">
            {proposal.ok === false ? (
              <div className="text-[var(--qd-loss)]">{proposal.error}</div>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-[1fr_220px] gap-3">
                <div className="text-[var(--qd-text-2)] whitespace-pre-wrap max-h-28 overflow-y-auto">
                  {proposal.proposal?.description || "Proposal ready."}
                  {(proposal.proposal?.notes || []).map((n) => `\n- ${n}`)}
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <Small label="Applied" value={proposal.applied ? "yes" : "no"} />
                  <Small label="Signals" value={proposal.validation?.signals ?? 0} />
                  <Small label="Symbol" value={proposal.validation?.symbol || "-"} />
                  <Small label="Source" value={proposal.validation?.data_source || "-"} />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
      )}

      {mode === "brief" && (
        <div className="qd-card p-4">
          <h2 className="font-head text-lg font-semibold text-white">Market Brief</h2>
          <p className="mt-1 text-xs text-[var(--qd-text-2)]">Use these as one-click Google AI prompts for market context, commodities, and risk.</p>
          <div className="mt-4 grid grid-cols-1 gap-2 md:grid-cols-3">
            {BRIEF_PROMPTS.map((prompt) => (
              <button
                key={prompt}
                type="button"
                onClick={() => {
                  setMode("chat");
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
            placeholder={mode === "agent" ? "Ask about the current proposal or strategy risk..." : "Ask QuantBot about strategies, commodities, markets, code..."}
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
      </div>
      {isUser && (
        <div className="w-7 h-7 rounded-sm bg-[var(--qd-surface-2)] border border-[var(--qd-border)] flex items-center justify-center flex-shrink-0">
          <User size={14} className="text-white" />
        </div>
      )}
    </div>
  );
};

const Small = ({ label, value }) => (
  <div className="border border-[var(--qd-border)] px-2 py-1 rounded-sm min-w-0">
    <div className="text-[9px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className="text-white truncate">{value}</div>
  </div>
);
