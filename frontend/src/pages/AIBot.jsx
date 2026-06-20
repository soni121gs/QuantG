import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { Bot, Send, Plus, PanelRight, X } from "lucide-react";
import { Button } from "../components/ui/button";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";
import { useExecutionState } from "../hooks/useExecutionState";
import ChatFeed from "../components/aibot/ChatFeed";
import AgentContextPanel from "../components/aibot/AgentContextPanel";
import { PromptSuggestionsPanel, EmptyState } from "../components/aibot/PromptSuggestionsPanel";

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
  const [railOpen, setRailOpen] = useState(false); // mobile context drawer

  const endRef = useRef(null);

  const providerLabel = aiStatus?.gemini_configured ? `Gemini (${aiStatus.model})` : "Local rules";
  const currentTitle = sessions.find((s) => s.id === sessionId)?.title || "New chat";

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
    setRailOpen(false);
  };

  const selectSession = (id) => {
    setSessionId(id);
    setRailOpen(false);
  };

  const deleteSession = (id) => {
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      const safe = next.length ? next : [{ id: newSessionId(), title: "New chat", updatedAt: Date.now() }];
      saveSessions(safe);
      if (id === sessionId) setSessionId(safe[0].id);
      return safe;
    });
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

  const contextPanel = (
    <AgentContextPanel
      sessions={sessions}
      sessionId={sessionId}
      onSelect={selectSession}
      onNew={newChat}
      onDelete={deleteSession}
      profile={profile}
      executionSummary={executionSummary}
    />
  );

  return (
    <div className="flex flex-col gap-4" data-testid="ai-bot-page">
      <PageHeader
        eyebrow="Active Risk & Co-Pilot"
        title="Hermes Analyst Co-Pilot"
        subtitle="Governance-gated operations and research co-pilot, grounded in live terminal context."
        badge={<StatusBadge tone={profile?.paper_mode ? "paper" : "live"}>{profile?.paper_mode ? "Paper" : "Live"}</StatusBadge>}
      />

      <div className="grid grid-cols-1 gap-4 lg:h-[calc(100vh-186px)] lg:grid-cols-12">

        {/* Chat workspace — fills the column height, internal scroll, docked composer */}
        <div className="flex min-h-[62vh] flex-col overflow-hidden qd-card lg:col-span-8 lg:min-h-0 lg:h-full">

          {/* Header: current chat + actions */}
          <div className="flex items-center justify-between gap-2 border-b border-[var(--qd-border)] px-4 py-3">
            <div className="flex min-w-0 items-center gap-2">
              <span className="grid h-7 w-7 shrink-0 place-items-center rounded bg-[var(--qd-accent)] shadow-sm">
                <Bot size={15} className="text-white" />
              </span>
              <span className="t-label truncate font-semibold text-[var(--qd-text)]" data-testid="ai-current-title">
                {currentTitle}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <span className="hidden items-center gap-1 rounded-full border border-[var(--qd-border)] px-2 py-0.5 font-mono text-[var(--qd-text-3)] sm:inline-flex t-meta uppercase tracking-wider" title="Reasoning provider">
                <span className={`h-1.5 w-1.5 rounded-full ${aiStatus?.gemini_configured ? "bg-emerald-500" : "bg-amber-500"}`} /> {providerLabel}
              </span>
              <button
                type="button"
                onClick={newChat}
                className="inline-flex items-center gap-1 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-2.5 py-1.5 font-mono text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] t-meta uppercase tracking-wider"
                data-testid="ai-new-chat-header"
              >
                <Plus size={13} /> New
              </button>
              {/* Mobile: open conversations / context drawer */}
              <button
                type="button"
                onClick={() => setRailOpen(true)}
                className="inline-flex items-center rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] p-1.5 text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] lg:hidden"
                data-testid="ai-open-rail"
                aria-label="Open conversations and context"
              >
                <PanelRight size={15} />
              </button>
            </div>
          </div>

          {/* Messages */}
          <div className="flex-1 space-y-4 overflow-y-auto p-4" data-testid="messages">
            {messages.length === 0 ? (
              <EmptyState onPick={send} />
            ) : (
              <ChatFeed messages={messages} profile={profile} onApprove={handleApproveAction} onReject={handleRejectAction} />
            )}
            {busy && <TypingIndicator />}
            <div ref={endRef} />
          </div>

          {/* Composer — suggestion chips only while a conversation is ongoing (empty state already shows them) */}
          <div className="border-t border-[var(--qd-border)] bg-[var(--qd-bg)]/20">
            {messages.length > 0 && <PromptSuggestionsPanel onPick={send} busy={busy} />}
            <div className="flex items-end gap-2 p-3">
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
                rows={1}
                placeholder="Ask about positions, risk or feed health — or say 'lower my daily loss limit to 6000'."
                className="t-body max-h-32 flex-1 resize-none rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] px-4 py-3 text-[var(--qd-text)] outline-none focus:border-[var(--qd-accent)] focus:ring-1 focus:ring-[var(--qd-accent)]"
                data-testid="ai-input"
              />
              <Button onClick={() => send()} disabled={busy || !text.trim()} variant="primary" size="lg" data-testid="ai-send-btn" aria-label="Send message">
                <Send size={16} />
              </Button>
            </div>
          </div>
        </div>

        {/* Desktop rail */}
        <div className="hidden lg:col-span-4 lg:block lg:h-full lg:min-h-0">
          {contextPanel}
        </div>
      </div>

      {/* Mobile rail drawer */}
      {railOpen && (
        <div className="fixed inset-0 z-[60] bg-black/60 lg:hidden" onClick={() => setRailOpen(false)} data-testid="ai-rail-overlay">
          <aside
            className="absolute right-0 top-0 bottom-0 w-[88%] max-w-sm overflow-y-auto border-l border-[var(--qd-border)] bg-[var(--qd-bg-2)] p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <span className="qd-section-title">Conversations &amp; Context</span>
              <button type="button" onClick={() => setRailOpen(false)} className="rounded p-1 text-[var(--qd-text-2)]" aria-label="Close">
                <X size={18} />
              </button>
            </div>
            {contextPanel}
          </aside>
        </div>
      )}
    </div>
  );
}

const TypingIndicator = () => (
  <div className="flex gap-3">
    <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded bg-[var(--qd-accent)] shadow-md">
      <Bot size={16} className="text-white" />
    </div>
    <div className="flex items-center gap-1.5 rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface-2)]/65 px-4 py-3">
      <span className="t-meta mr-1 font-mono text-[var(--qd-text-3)]">Reading your account</span>
      <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "0ms" }} />
      <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "150ms" }} />
      <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "300ms" }} />
    </div>
  </div>
);
