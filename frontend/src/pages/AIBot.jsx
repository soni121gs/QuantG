import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { AlertCircle, ArrowRight, Bot, Send, Sparkles, User, ShieldAlert, Sliders, CheckCircle2, XCircle, ShieldCheck, HelpCircle, MessageSquare, Plus } from "lucide-react";
import { Button } from "../components/ui/button";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";
import { useExecutionState } from "../hooks/useExecutionState";
import ChatFeed from "../components/aibot/ChatFeed";
import SessionHistorySidebar from "../components/aibot/SessionHistorySidebar";
import { PromptSuggestionsPanel, EmptyState } from "../components/aibot/PromptSuggestionsPanel";



// Human-readable labels + formatters for the 6 settings the agent can propose.
const FIELD_LABELS = {
  paper_mode: { label: "Trading Mode", fmt: (v) => (v ? "PAPER" : "LIVE") },
  max_daily_loss: { label: "Daily Loss Limit", fmt: (v) => `${Number(v).toLocaleString()} INR` },
  max_position_size: { label: "Max Position Size", fmt: (v) => `${Number(v).toLocaleString()} INR` },
  per_strategy_capital: { label: "Per-Strategy Capital", fmt: (v) => `${Number(v).toLocaleString()} INR` },
  max_trades_per_day: { label: "Max Trades / Day", fmt: (v) => `${v}` },
  default_qty: { label: "Default Quantity", fmt: (v) => `${v}` },
};

// Multiple conversations are tracked client-side; the backend already keys
// chat history by an arbitrary session_id, so no server change is needed.
const SESSIONS_KEY = "quantg-agent-sessions";
const newSessionId = () => `s-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
const loadSessions = () => {
  try { return JSON.parse(window.localStorage.getItem(SESSIONS_KEY) || "[]"); } catch { return []; }
};
const saveSessions = (list) => {
  try { window.localStorage.setItem(SESSIONS_KEY, JSON.stringify(list.slice(0, 30))); } catch { /* ignore */ }
};

export default function AIBot() {
  const { summary: executionSummary } = useExecutionState({ pollMs: 15000 });
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [aiStatus, setAiStatus] = useState(null);
  const [profile, setProfile] = useState(null);
  const [sessions, setSessions] = useState(loadSessions);
  const [sessionId, setSessionId] = useState(() => loadSessions()[0]?.id || newSessionId());

  const endRef = useRef(null);

  const lossLimit = Number(profile?.max_daily_loss) || 0;
  const netPnl = Number(executionSummary?.net_pnl) || 0;
  const drawdownUsedPct = lossLimit > 0 ? Math.min(100, Math.max(0, (Math.max(0, -netPnl) / lossLimit) * 100)) : 0;
  const drawdownTone = drawdownUsedPct >= 80 ? "rose" : drawdownUsedPct >= 50 ? "amber" : "emerald";
  const providerLabel = aiStatus?.gemini_configured ? `Gemini (${aiStatus.model})` : "Local rules";

  const fetchProfile = () => {
    api.get("/profile").then((r) => setProfile(r.data)).catch(() => {});
  };

  useEffect(() => {
    api.get("/ai/status").then((r) => setAiStatus(r.data)).catch(() => {});
    fetchProfile();
    if (loadSessions().length === 0) {
      const seed = [{ id: sessionId, title: "New chat", updatedAt: Date.now() }];
      setSessions(seed);
      saveSessions(seed);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    api.get(`/ai/chat/${sessionId}`).then((r) => setMessages(r.data || [])).catch(() => setMessages([]));
  }, [sessionId]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  const upsertSession = (id, content) => {
    setSessions((prev) => {
      const existing = prev.find((s) => s.id === id);
      const title = existing && existing.title && existing.title !== "New chat" ? existing.title : content.slice(0, 42);
      const next = [{ id, title, updatedAt: Date.now() }, ...prev.filter((s) => s.id !== id)];
      saveSessions(next);
      return next;
    });
  };

  const newChat = () => {
    const id = newSessionId();
    setSessions((prev) => {
      const next = [{ id, title: "New chat", updatedAt: Date.now() }, ...prev];
      saveSessions(next);
      return next;
    });
    setSessionId(id);
    setMessages([]);
    setText("");
  };

  const send = async (msg) => {
    const content = (msg ?? text).trim();
    if (!content || busy) return;
    setBusy(true);
    setText("");
    upsertSession(sessionId, content);
    setMessages((m) => [...m, { id: `c-${Date.now()}`, role: "user", content }]);
    try {
      const r = await api.post("/agent/chat", { session_id: sessionId, message: content });
      setMessages((m) => [...m, {
        id: `a-${Date.now()}`,
        role: "assistant",
        content: r.data.content,
        tools_used: r.data.tools_used,
        unavailable: r.data.unavailable,
        read_only: r.data.read_only,
        pending_action: r.data.pending_action,
      }]);
    } catch (e) {
      setMessages((m) => [...m, { id: `err-${Date.now()}`, role: "assistant", content: `Error: ${e.response?.data?.detail || e.message}` }]);
    } finally { setBusy(false); }
  };

  const handleApproveAction = async (actionId) => {
    try {
      await api.post("/agent/action/approve", { action_id: actionId });
      setMessages((m) => m.map((msg) => msg.pending_action?.id === actionId ? { ...msg, pending_action: { ...msg.pending_action, status: "approved" } } : msg));
      fetchProfile();
    } catch (e) {
      alert(e.response?.data?.detail || "Action approval failed");
    }
  };

  const handleRejectAction = async (actionId) => {
    try {
      await api.post("/agent/action/reject", { action_id: actionId });
      setMessages((m) => m.map((msg) => msg.pending_action?.id === actionId ? { ...msg, pending_action: { ...msg.pending_action, status: "rejected" } } : msg));
    } catch (e) {
      alert(e.response?.data?.detail || "Action rejection failed");
    }
  };

  return (
    <div className="space-y-5 pb-4" data-testid="ai-bot-page">
      <PageHeader
        eyebrow="Active Risk & Co-Pilot"
        title="Ask QuantG Agent"
        subtitle="Trading diagnostics and governed action proposals, grounded in your live account."
        badge={<StatusBadge tone={profile?.paper_mode ? "paper" : "live"}>{profile?.paper_mode ? "Paper" : "Live"}</StatusBadge>}
      />

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">

        {/* Left: chat workspace — generous height, internal scroll, docked composer */}
        <div className="lg:col-span-8 flex flex-col qd-card overflow-hidden h-[calc(100vh-210px)] min-h-[480px]">

          {/* Conversation switcher */}
          <div className="flex items-center justify-between gap-2 border-b border-[var(--qd-border)] px-3 py-2.5">
            <div className="flex items-center gap-2 min-w-0">
              <MessageSquare size={15} className="text-[var(--qd-text-3)] shrink-0" />
              <select
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
                className="max-w-[260px] truncate bg-transparent text-sm font-semibold text-[var(--qd-text)] outline-none cursor-pointer"
                data-testid="ai-session-select"
              >
                {sessions.map((s) => <option key={s.id} value={s.id}>{s.title || "New chat"}</option>)}
              </select>
            </div>
            <div className="flex items-center gap-2">
              <span className="hidden sm:inline-flex items-center gap-1 rounded-full border border-[var(--qd-border)] px-2 py-0.5 font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)]" title="Reasoning provider">
                <span className={`h-1.5 w-1.5 rounded-full ${aiStatus?.gemini_configured ? "bg-emerald-500" : "bg-amber-500"}`} /> {providerLabel}
              </span>
              <button
                type="button"
                onClick={newChat}
                className="flex items-center gap-1.5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-2.5 py-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)]"
                data-testid="ai-new-chat"
              >
                <Plus size={13} /> New
              </button>
            </div>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-4 space-y-4" data-testid="messages">
            {messages.length === 0 ? (
              <EmptyState onPick={send} />
            ) : (
              <ChatFeed messages={messages} profile={profile} onApprove={handleApproveAction} onReject={handleRejectAction} />
            )}
            {busy && <TypingIndicator />}
            <div ref={endRef} />
          </div>

          {/* Composer: persistent quick actions + multi-line input */}
          <div className="border-t border-[var(--qd-border)] bg-[var(--qd-bg)]/20">
            <PromptSuggestionsPanel onPick={send} busy={busy} />
            <div className="p-3 flex items-end gap-2">
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
                rows={1}
                placeholder="Ask about positions, risk or feed health — or say 'lower my daily loss limit to 6000'. Enter to send · Shift+Enter for a new line."
                className="flex-1 resize-none max-h-32 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] focus:ring-1 focus:ring-[var(--qd-accent)] outline-none px-4 py-3 text-sm text-[var(--qd-text)] rounded"
                data-testid="ai-input"
              />
              <Button onClick={() => send()} disabled={busy || !text.trim()} variant="primary" size="lg" data-testid="ai-send-btn" aria-label="Send message">
                <Send size={16} />
              </Button>
            </div>
          </div>
        </div>

        <SessionHistorySidebar
          profile={profile}
          executionSummary={executionSummary}
        />
      </div>
    </div>
  );
}
const TypingIndicator = () => (
  <div className="flex gap-3">
    <div className="w-8 h-8 rounded bg-[var(--qd-accent)] flex items-center justify-center flex-shrink-0 shadow-md">
      <Bot size={16} className="text-white" />
    </div>
    <div className="flex items-center gap-1.5 rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface-2)]/65 px-4 py-3">
      <span className="font-mono text-[11px] text-[var(--qd-text-3)] mr-1">Reading your account</span>
      <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "0ms" }} />
      <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "150ms" }} />
      <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "300ms" }} />
    </div>
  </div>
);
