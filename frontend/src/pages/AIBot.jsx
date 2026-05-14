import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { Bot, Send, Sparkles, User } from "lucide-react";

const SESSION = "default";
const SUGGESTIONS = [
  "Suggest a momentum strategy for NIFTY",
  "Explain RSI vs MACD in 3 bullets",
  "Write Python for Bollinger band squeeze",
  "How to manage risk with a 1L portfolio?",
];

export default function AIBot() {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    api.get(`/ai/chat/${SESSION}`).then((r) => setMessages(r.data)).catch(() => {});
  }, []);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const send = async (msg) => {
    const content = (msg ?? text).trim();
    if (!content || busy) return;
    setBusy(true);
    setText("");
    setMessages((m) => [...m, { role: "user", content }]);
    try {
      const r = await api.post("/ai/chat", { session_id: SESSION, message: content });
      setMessages((m) => [...m, { role: "assistant", content: r.data.content }]);
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", content: `× Error: ${e.response?.data?.detail || e.message}` }]);
    } finally { setBusy(false); }
  };

  return (
    <div className="flex flex-col h-[calc(100vh-130px)]" data-testid="ai-bot-page">
      <div className="mb-3">
        <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// CLAUDE SONNET 4.5</div>
        <h1 className="font-head text-3xl font-bold text-white mt-1 flex items-center gap-3"><Bot size={26} className="text-[var(--qd-accent)]" /> QuantBot</h1>
        <p className="text-xs text-[var(--qd-text-2)] mt-1">Your AI trading co-pilot. Ask for strategies, code, or analysis.</p>
      </div>

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
            <Message key={i} m={m} />
          ))}
          {busy && <Message m={{ role: "assistant", content: "▊" }} />}
          <div ref={endRef} />
        </div>

        <div className="border-t border-[var(--qd-border)] p-3 flex items-center gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder="Ask QuantBot about strategies, markets, code..."
            className="flex-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2.5 text-sm text-white font-mono rounded-sm"
            data-testid="chat-input"
          />
          <button onClick={() => send()} disabled={busy || !text.trim()} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white px-4 py-2.5 rounded-sm" data-testid="send-btn">
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
