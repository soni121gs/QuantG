import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { Bot, Send, Plus, PanelRight, X, ShieldCheck } from "lucide-react";
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
  const [activeTab, setActiveTab] = useState("chat"); // "chat" or "approvals"
  const [pendingActions, setPendingActions] = useState([]);

  const endRef = useRef(null);

  const providerLabel = aiStatus?.gemini_configured ? `Gemini (${aiStatus.model})` : "Local rules";
  const currentTitle = sessions.find((s) => s.id === sessionId)?.title || "New chat";

  const fetchProfile = () => {
    api.get("/profile").then((r) => setProfile(r.data)).catch(() => {});
  };

  const fetchPendingActions = async () => {
    try {
      const r = await api.get("/agent/actions/pending");
      setPendingActions(r.data || []);
    } catch {}
  };

  useEffect(() => {
    api.get("/ai/status").then((r) => setAiStatus(r.data)).catch(() => {});
    fetchProfile();
    fetchPendingActions();
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
    setActiveTab("chat");
  };

  const selectSession = (id) => {
    setSessionId(id);
    setRailOpen(false);
    setActiveTab("chat");
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
      if (r.data.pending_action) {
        fetchPendingActions();
        window.dispatchEvent(new Event("quantg-pending-actions-updated"));
      }
    } catch (e) {
      setMessages((m) => [...m, { id: `err-${Date.now()}`, role: "assistant", content: `Error: ${e.response?.data?.detail || e.message}` }]);
    } finally { setBusy(false); }
  };

  const handleApproveAction = async (actionId) => {
    try {
      await api.post("/agent/action/approve", { action_id: actionId });
      setMessages((m) => m.map((msg) => msg.pending_action?.id === actionId ? { ...msg, pending_action: { ...msg.pending_action, status: "approved" } } : msg));
      setPendingActions((prev) => prev.filter((a) => a.action_id !== actionId));
      window.dispatchEvent(new Event("quantg-pending-actions-updated"));
      fetchProfile();
    } catch (e) {
      alert(e.response?.data?.detail || "Action approval failed");
    }
  };

  const handleRejectAction = async (actionId) => {
    try {
      await api.post("/agent/action/reject", { action_id: actionId });
      setMessages((m) => m.map((msg) => msg.pending_action?.id === actionId ? { ...msg, pending_action: { ...msg.pending_action, status: "rejected" } } : msg));
      setPendingActions((prev) => prev.filter((a) => a.action_id !== actionId));
      window.dispatchEvent(new Event("quantg-pending-actions-updated"));
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

          {/* Header: tabs */}
          <div className="flex items-center justify-between gap-2 border-b border-[var(--qd-border)] px-4 py-3">
            <div className="flex min-w-0 items-center gap-4">
              <button
                onClick={() => setActiveTab("chat")}
                className={`pb-1 border-b-2 font-head text-sm font-bold uppercase tracking-wider transition-colors ${
                  activeTab === "chat"
                    ? "border-[var(--qd-accent)] text-[var(--qd-text)]"
                    : "border-transparent text-[var(--qd-text-3)] hover:text-[var(--qd-text-2)]"
                }`}
              >
                Co-Pilot Chat
              </button>
              <button
                onClick={() => { setActiveTab("approvals"); fetchPendingActions(); }}
                className={`pb-1 border-b-2 font-head text-sm font-bold uppercase tracking-wider transition-colors flex items-center gap-1.5 ${
                  activeTab === "approvals"
                    ? "border-[var(--qd-accent)] text-[var(--qd-text)]"
                    : "border-transparent text-[var(--qd-text-3)] hover:text-[var(--qd-text-2)]"
                }`}
              >
                Approvals Queue
                {pendingActions.length > 0 && (
                  <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--qd-warn)] text-[9px] font-bold text-white px-1 shadow-sm">
                    {pendingActions.length}
                  </span>
                )}
              </button>
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

          {/* Tab Contents */}
          {activeTab === "chat" ? (
            <>
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

              {/* Composer */}
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
            </>
          ) : (
            /* Approvals Queue */
            <div className="flex-1 space-y-4 overflow-y-auto p-4" data-testid="approvals-queue">
              {pendingActions.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-20 text-center">
                  <ShieldCheck size={40} className="text-emerald-500/70" />
                  <p className="mt-4 font-head text-base font-bold text-[var(--qd-text)]">No pending approvals</p>
                  <p className="mt-1 text-sm text-[var(--qd-text-2)]">Hermes hasn't proposed any non-trading actions requiring review.</p>
                </div>
              ) : (
                <div className="grid gap-4">
                  {pendingActions.map((a) => {
                    let badgeColor = "bg-blue-500/10 text-blue-400 border border-blue-500/20";
                    let actionTitle = a.action_type;
                    if (a.action_type === "draft_wiki_note") {
                      badgeColor = "bg-indigo-500/10 text-indigo-400 border border-indigo-500/20";
                      actionTitle = "Draft Wiki Note";
                    } else if (a.action_type === "draft_task_entry") {
                      badgeColor = "bg-amber-500/10 text-amber-400 border border-amber-500/20";
                      actionTitle = "Draft Task Entry";
                    } else if (a.action_type === "draft_incident_report") {
                      badgeColor = "bg-rose-500/10 text-rose-400 border border-rose-500/20";
                      actionTitle = "Draft Incident Report";
                    } else if (a.action_type === "draft_pr_summary") {
                      badgeColor = "bg-purple-500/10 text-purple-400 border border-purple-500/20";
                      actionTitle = "Draft PR Summary";
                    } else if (a.action_type === "update_profile") {
                      badgeColor = "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20";
                      actionTitle = "Update Settings";
                    }

                    const titleParam = a.params?.title || a.params?.task_id || "Untitled Proposal";
                    const bodyText = a.params?.body_markdown || a.params?.content || JSON.stringify(a.params, null, 2);

                    return (
                      <div key={a.action_id} className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-4 shadow-sm">
                        <div className="flex items-start justify-between gap-4">
                          <div className="min-w-0">
                            <span className={`inline-flex items-center rounded-sm px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider ${badgeColor}`}>
                              {actionTitle}
                            </span>
                            <h4 className="mt-2 font-head text-base font-bold text-[var(--qd-text)] truncate">{titleParam}</h4>
                            {a.params?.folder && (
                              <div className="mt-1 font-mono text-xs text-[var(--qd-text-3)]">
                                Target Folder: <span className="text-[var(--qd-text-2)]">{a.params.folder}</span>
                              </div>
                            )}
                          </div>
                          <div className="flex items-center gap-2">
                            <Button
                              variant="secondary"
                              size="sm"
                              onClick={() => handleRejectAction(a.action_id)}
                            >
                              Reject
                            </Button>
                            <Button
                              variant="primary"
                              size="sm"
                              onClick={() => handleApproveAction(a.action_id)}
                            >
                              Approve
                            </Button>
                          </div>
                        </div>
                        
                        <div className="mt-3 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-3">
                          <pre className="whitespace-pre-wrap font-mono text-xs text-[var(--qd-text-2)] max-h-48 overflow-y-auto leading-relaxed">
                            {bodyText}
                          </pre>
                        </div>
                        
                        <div className="mt-3 flex items-center justify-between text-[10px] font-mono text-[var(--qd-text-3)]">
                          <span>Action ID: {a.action_id}</span>
                          <span>Proposed: {new Date(a.created_at).toLocaleString("en-IN")}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
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
