import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { Bot, Send, Plus, PanelRight, PanelRightClose, PanelLeft, PanelLeftClose, X, ShieldCheck, Microscope, RefreshCw, AlertTriangle, CheckCircle2, FlaskConical } from "lucide-react";
import { Button } from "../components/ui/button";
import { StatusBadge } from "../components/ui/app-shell";
import { useExecutionState } from "../hooks/useExecutionState";
import ChatFeed from "../components/aibot/ChatFeed";
import AgentContextPanel, { ConversationHistoryCard, RiskPostureCard, CapabilitiesCard } from "../components/aibot/AgentContextPanel";
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
  const [activeTab, setActiveTab] = useState("chat"); // "chat", "approvals", or "research"
  const [pendingActions, setPendingActions] = useState([]);
  const [researchRows, setResearchRows] = useState([]);
  const [researchLoading, setResearchLoading] = useState(false);

  // Collapsible sidebar states for desktop
  const [showHistory, setShowHistory] = useState(true);
  const [showInspector, setShowInspector] = useState(true);
  const [showSuggestions, setShowSuggestions] = useState(true);

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

  const fetchResearchLedger = async () => {
    setResearchLoading(true);
    try {
      const r = await api.get("/ops/research/hypotheses?limit=50");
      setResearchRows(r.data || []);
    } catch {
      setResearchRows([]);
    } finally {
      setResearchLoading(false);
    }
  };

  useEffect(() => {
    api.get("/ai/status").then((r) => setAiStatus(r.data)).catch(() => {});
    fetchProfile();
    fetchPendingActions();
    fetchResearchLedger();
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
    setShowSuggestions(true);
  };

  const selectSession = (id) => {
    setSessionId(id);
    setRailOpen(false);
    setActiveTab("chat");
    setShowSuggestions(true);
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
      const { data } = await api.post("/agent/action/approve", { action_id: actionId });
      setMessages((m) => m.map((msg) => msg.pending_action?.id === actionId ? { ...msg, pending_action: { ...msg.pending_action, status: "approved" } } : msg));
      setPendingActions((prev) => prev.filter((a) => a.action_id !== actionId));
      window.dispatchEvent(new Event("quantg-pending-actions-updated"));
      fetchProfile();
      const bits = [];
      if (data?.field) bits.push(`\`${data.field}\` → \`${data.new_value}\``);
      if (data?.outcome === "founder_directed_applied") bits.push("(founder-directed)");
      if (data?.outcome === "edit_in_template_task") bits.push("queued as an edit-in-template task");
      if (data?.resync_warning) bits.push(`⚠️ ${data.resync_warning}`);
      setMessages((m) => [...m, { id: `ok-${Date.now()}`, role: "assistant", content: `✅ Approved${bits.length ? ": " + bits.join(" ") : "."}` }]);
    } catch (e) {
      const detail = e.response?.data?.detail || "Action approval failed";
      const stale = e.response?.status === 404;
      // A stale/invalid proposal can never be approved — drop it so it stops nagging.
      if (stale) {
        setPendingActions((prev) => prev.filter((a) => a.action_id !== actionId));
        window.dispatchEvent(new Event("quantg-pending-actions-updated"));
      }
      setMessages((m) => [...m, { id: `err-${Date.now()}`, role: "assistant", content: `⚠️ Could not apply this change: ${detail}${stale ? " — the proposal was stale and has been cleared." : ""}` }]);
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

  // Dynamic column layout classes
  const historySpan = showHistory ? 3 : 0;
  const inspectorSpan = showInspector ? 3 : 0;
  const chatSpan = 12 - historySpan - inspectorSpan;
  const chatSpanClass = {
    6: "lg:col-span-6",
    9: "lg:col-span-9",
    12: "lg:col-span-12"
  }[chatSpan] || "lg:col-span-8";

  return (
    <div className="flex flex-col gap-2" data-testid="ai-bot-page">
      {/* Ultra-compact single-line header to maximize chat workspace */}
      <div className="flex items-center justify-between gap-2 pb-1">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-6 h-6 rounded-md bg-[var(--qd-accent)] flex items-center justify-center shrink-0">
            <Bot size={13} className="text-[var(--qd-accent-contrast)]" />
          </div>
          <h1 className="font-head text-sm font-semibold text-[var(--qd-text)] truncate">Hermes Analyst Co-Pilot</h1>
          <StatusBadge tone={profile?.paper_mode ? "paper" : "live"} className="h-4 px-1 text-[9px] font-mono font-bold shrink-0">
            {profile?.paper_mode ? "Paper" : "Live"}
          </StatusBadge>
        </div>
        <div className="hidden sm:flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)] font-semibold shrink-0">
          <span className={`h-1.5 w-1.5 rounded-full ${profile?.paper_mode ? "bg-amber-400" : "bg-emerald-400"}`} />
          {profile?.paper_mode ? "Paper Mode" : "Live Broker"}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:h-[calc(100vh-92px)] lg:grid-cols-12">
        {/* Left Sidebar: Conversations list */}
        {showHistory && (
          <div className="hidden lg:block lg:col-span-3 lg:h-full lg:min-h-0 animate-fade-in">
            <ConversationHistoryCard
              sessions={sessions}
              sessionId={sessionId}
              onSelect={selectSession}
              onNew={newChat}
              onDelete={deleteSession}
            />
          </div>
        )}

        {/* Center Panel: Chat workspace */}
        <div className={`flex min-h-[80vh] flex-col overflow-hidden qd-card ${chatSpanClass} lg:min-h-0 lg:h-full transition-all duration-300`}>

          {/* Header: tabs & sidebar toggles */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)]/40 px-3 py-2">
            <div className="flex min-w-0 items-center gap-3">
              <button
                type="button"
                onClick={() => setShowHistory(!showHistory)}
                className="hidden lg:inline-flex items-center justify-center p-1.5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] hover:bg-[var(--qd-surface-2)] cursor-pointer transition-colors"
                title={showHistory ? "Collapse history" : "Expand history"}
              >
                {showHistory ? <PanelLeftClose size={15} /> : <PanelLeft size={15} />}
              </button>

              <div className="flex min-w-0 items-center gap-2">
                <button
                  onClick={() => setActiveTab("chat")}
                  className={`rounded-[var(--qd-radius-sm)] border px-2.5 py-1.5 font-head text-xs font-semibold transition-colors cursor-pointer ${
                    activeTab === "chat"
                      ? "border-[var(--qd-border-strong)] bg-[var(--qd-surface)] text-[var(--qd-text)]"
                      : "border-transparent text-[var(--qd-text-3)] hover:border-[var(--qd-border)] hover:text-[var(--qd-text-2)]"
                  }`}
                >
                  Chat
                </button>
                <button
                  onClick={() => { setActiveTab("approvals"); fetchPendingActions(); }}
                  className={`flex items-center gap-1.5 rounded-[var(--qd-radius-sm)] border px-2.5 py-1.5 font-head text-xs font-semibold transition-colors cursor-pointer ${
                    activeTab === "approvals"
                      ? "border-[var(--qd-border-strong)] bg-[var(--qd-surface)] text-[var(--qd-text)]"
                      : "border-transparent text-[var(--qd-text-3)] hover:border-[var(--qd-border)] hover:text-[var(--qd-text-2)]"
                  }`}
                >
                  Approvals
                  {pendingActions.length > 0 && (
                    <span className="inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-full bg-[var(--qd-warn)] text-[9px] font-bold text-white px-1 shadow-sm">
                      {pendingActions.length}
                    </span>
                  )}
                </button>
                <button
                  onClick={() => { setActiveTab("research"); fetchResearchLedger(); }}
                  className={`flex items-center gap-1.5 rounded-[var(--qd-radius-sm)] border px-2.5 py-1.5 font-head text-xs font-semibold transition-colors cursor-pointer ${
                    activeTab === "research"
                      ? "border-[var(--qd-border-strong)] bg-[var(--qd-surface)] text-[var(--qd-text)]"
                      : "border-transparent text-[var(--qd-text-3)] hover:border-[var(--qd-border)] hover:text-[var(--qd-text-2)]"
                  }`}
                >
                  <Microscope size={13} />
                  Research
                  {researchRows.length > 0 && (
                    <span className="inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-full bg-[var(--qd-accent)] text-[9px] font-bold text-white px-1 shadow-sm">
                      {researchRows.length}
                    </span>
                  )}
                </button>
              </div>
            </div>
            
            <div className="flex items-center gap-2">
              <span className="hidden items-center gap-1.5 rounded-full border border-[var(--qd-border)] px-2.5 py-0.5 font-mono text-[var(--qd-text-3)] sm:inline-flex t-meta uppercase tracking-wider" title="Reasoning provider">
                <span className={`h-1.5 w-1.5 rounded-full ${aiStatus?.gemini_configured ? "bg-emerald-500" : "bg-amber-500"}`} /> {providerLabel}
              </span>
              
              <button
                type="button"
                onClick={newChat}
                className="inline-flex items-center gap-1 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-2.5 py-1.5 font-mono text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] t-meta uppercase tracking-wider cursor-pointer hover:bg-[var(--qd-surface-2)]"
                data-testid="ai-new-chat-header"
              >
                <Plus size={13} /> New
              </button>

              <button
                type="button"
                onClick={() => setShowInspector(!showInspector)}
                className="hidden lg:inline-flex items-center justify-center p-1.5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] hover:bg-[var(--qd-surface-2)] cursor-pointer transition-colors"
                title={showInspector ? "Collapse inspector" : "Expand inspector"}
              >
                {showInspector ? <PanelRightClose size={15} /> : <PanelRight size={15} />}
              </button>
              
              {/* Mobile: open conversations / context drawer */}
              <button
                type="button"
                onClick={() => setRailOpen(true)}
                className="inline-flex items-center rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] p-1.5 text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] lg:hidden cursor-pointer"
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
              <div className="flex-1 space-y-3 overflow-y-auto p-3 md:p-4" data-testid="messages">
                {messages.length === 0 ? (
                  <EmptyState onPick={send} />
                ) : (
                  <ChatFeed messages={messages} profile={profile} onApprove={handleApproveAction} onReject={handleRejectAction} />
                )}
                {busy && <TypingIndicator />}
                <div ref={endRef} />
              </div>

              {/* Composer Input Area */}
              <div className="border-t border-[var(--qd-border)] bg-[var(--qd-surface-2)]/10 backdrop-blur-xs flex flex-col">
                {messages.length > 0 && showSuggestions && !text.trim() && (
                  <PromptSuggestionsPanel onPick={send} busy={busy} onClose={() => setShowSuggestions(false)} />
                )}
                <div className="p-2.5">
                  <div className="group relative rounded-lg border border-[var(--qd-border)] bg-[var(--qd-surface)] shadow-xs focus-within:border-[var(--qd-border-strong)] focus-within:ring-2 focus-within:ring-[var(--qd-focus)] transition-all flex flex-col">
                    <textarea
                      value={text}
                      onChange={(e) => setText(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          send();
                        }
                      }}
                      rows={2}
                      placeholder="Ask about positions, risk or feed health — or say 'lower my daily loss limit to 6000'."
                      className="w-full border-0 bg-transparent px-3 py-2 text-[13px] leading-relaxed text-[var(--qd-text)] outline-none shadow-none resize-none font-sans placeholder-[var(--qd-text-3)]"
                      data-testid="ai-input"
                      disabled={busy}
                    />
                    
                    <div className="flex items-center justify-between px-3 py-2 border-t border-[var(--qd-border)]/40 bg-[var(--qd-surface-2)]/20 rounded-b-xl">
                      <div className="flex items-center gap-2">
                        <span className="t-meta text-[var(--qd-text-3)] font-mono uppercase tracking-widest flex items-center gap-1 font-semibold">
                          <Bot size={12} className="text-[var(--qd-accent)]" /> Active Analyst
                        </span>
                      </div>
                      
                      <button 
                        onClick={() => send()} 
                        disabled={busy || !text.trim()} 
                        className="rounded-md bg-[var(--qd-accent)] p-2 flex items-center justify-center transition-all hover:bg-[var(--qd-accent-hover)] active:scale-95 disabled:opacity-40 disabled:pointer-events-none cursor-pointer shadow-sm"
                        data-testid="ai-send-btn" 
                        aria-label="Send message"
                        type="button"
                      >
                        <Send size={14} className="text-[var(--qd-accent-contrast)]" />
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </>
          ) : activeTab === "approvals" ? (
            /* Approvals Queue */
            <div className="flex-1 space-y-4 overflow-y-auto p-3 md:p-4" data-testid="approvals-queue">
              {pendingActions.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-20 text-center">
                  <ShieldCheck size={40} className="text-emerald-500/70" />
                  <p className="mt-4 font-head text-base font-bold text-[var(--qd-text)]">No pending approvals</p>
                  <p className="mt-1 text-sm text-[var(--qd-text-2)]">Hermes hasn't proposed any non-trading actions requiring review.</p>
                </div>
              ) : (
                <div className="grid gap-3">
                  {pendingActions.map((a) => {
                    let badgeColor = "bg-[var(--qd-surface-3)] text-[var(--qd-accent)] border border-[var(--qd-border)]";
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
                      <div key={a.action_id} className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 shadow-sm">
                        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
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
                          <div className="flex items-center gap-2 sm:shrink-0">
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
          ) : (
            <ResearchLabPanel
              rows={researchRows}
              loading={researchLoading}
              onRefresh={fetchResearchLedger}
              onAsk={send}
            />
          )}
        </div>

        {/* Right Sidebar: Risk Posture & Capabilities (Desktop only) */}
        {showInspector && (
          <div className="hidden lg:flex lg:col-span-3 lg:flex-col lg:h-full lg:overflow-y-auto space-y-4 lg:min-h-0 pr-1 animate-fade-in">
            <RiskPostureCard profile={profile} executionSummary={executionSummary} />
            <CapabilitiesCard profile={profile} />
          </div>
        )}
      </div>

      {/* Mobile rail drawer */}
      {railOpen && (
        <div className="fixed inset-0 z-[60] bg-black/60 lg:hidden animate-fade-in" onClick={() => setRailOpen(false)} data-testid="ai-rail-overlay">
          <aside
            className="absolute right-0 top-0 bottom-0 w-[88%] max-w-sm overflow-y-auto border-l border-[var(--qd-border)] bg-[var(--qd-bg-2)] p-4 flex flex-col gap-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <span className="qd-section-title font-mono animate-fade-in">Conversations &amp; Context</span>
              <button type="button" onClick={() => setRailOpen(false)} className="rounded p-1 text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)] cursor-pointer" aria-label="Close">
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

const verdictTone = (status) => {
  const s = String(status || "UNTESTED").toUpperCase();
  if (s === "CANDIDATE_EDGE") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-500";
  if (s === "FRAGILE" || s === "INSUFFICIENT_DATA") return "border-amber-500/30 bg-amber-500/10 text-amber-500";
  if (s === "NO_EDGE_NEGATIVE" || s === "REJECTED") return "border-rose-500/30 bg-rose-500/10 text-rose-500";
  return "border-[var(--qd-border)] bg-[var(--qd-surface-3)] text-[var(--qd-text-2)]";
};

const statusIcon = (status) => {
  const s = String(status || "").toUpperCase();
  if (s === "CANDIDATE_EDGE") return <CheckCircle2 size={13} />;
  if (s === "FRAGILE" || s === "INSUFFICIENT_DATA") return <AlertTriangle size={13} />;
  return <FlaskConical size={13} />;
};

const ResearchLabPanel = ({ rows, loading, onRefresh, onAsk }) => {
  const total = rows.length;
  const validated = rows.filter((r) => r.verdict?.status === "CANDIDATE_EDGE").length;
  const watch = rows.filter((r) => ["FRAGILE", "INSUFFICIENT_DATA"].includes(r.verdict?.status)).length;
  const rejected = rows.filter((r) => ["NO_EDGE_NEGATIVE", "REJECTED"].includes(r.verdict?.status)).length;

  return (
    <div className="flex-1 overflow-y-auto p-3 md:p-4" data-testid="research-lab">
      <div className="mb-3 flex flex-col gap-3 border-b border-[var(--qd-border)] pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Microscope size={16} className="text-[var(--qd-accent)]" />
            <h2 className="font-head text-sm font-semibold text-[var(--qd-text)]">Hermes Research Lab</h2>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">
            <span>{total} hypotheses</span>
            <span>{validated} validated</span>
            <span>{watch} watch</span>
            <span>{rejected} rejected</span>
          </div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="inline-flex items-center justify-center gap-1.5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] disabled:opacity-50"
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </div>

      {loading && rows.length === 0 ? (
        <div className="grid gap-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-28 animate-pulse rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)]" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="flex min-h-[48vh] flex-col items-center justify-center text-center">
          <Microscope size={40} className="text-[var(--qd-text-3)]" />
          <p className="mt-4 font-head text-base font-bold text-[var(--qd-text)]">No research hypotheses yet</p>
          <p className="mt-1 max-w-xl text-sm text-[var(--qd-text-2)]">
            Hermes will show structured hypotheses here once the research ledger has entries from Edge Lab, OOS studies, or manually saved research ideas.
          </p>
        </div>
      ) : (
        <div className="grid gap-3">
          {rows.map((row) => {
            const verdict = row.verdict || {};
            const evidence = row.evidence_links || [];
            const limitations = verdict.limitations || [];
            return (
              <div key={row.hypothesis_id} className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface)] p-3 shadow-sm">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider ${verdictTone(verdict.status)}`}>
                        {statusIcon(verdict.status)}
                        {verdict.status || "UNTESTED"}
                      </span>
                      <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">
                        {row.status || "draft"} · n={verdict.sample_n || 0} · conf={Number(verdict.confidence || 0).toFixed(2)}
                      </span>
                    </div>
                    <h3 className="mt-2 font-head text-sm font-semibold leading-snug text-[var(--qd-text)]">{row.hypothesis}</h3>
                    <p className="mt-1 text-xs leading-relaxed text-[var(--qd-text-2)]">{row.market_premise || row.entry_exit_idea || "No premise recorded."}</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => onAsk(`Critique research hypothesis ${row.hypothesis_id}. Tell me what is proven, what is missing, what would falsify it, and what the research desk says.`)}
                    className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)]"
                  >
                    <Bot size={12} />
                    Ask Hermes
                  </button>
                </div>

                <div className="mt-3 grid gap-2 md:grid-cols-3">
                  <MetricTile label="Instrument" value={row.instrument || "-"} />
                  <MetricTile label="Timeframe" value={row.timeframe || "-"} />
                  <MetricTile label="Evidence" value={`${evidence.length} links`} />
                </div>

                {verdict.summary && (
                  <div className="mt-3 rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-2 text-xs leading-relaxed text-[var(--qd-text-2)]">
                    {verdict.summary}
                  </div>
                )}

                {evidence.length > 0 && (
                  <div className="mt-3 overflow-hidden rounded border border-[var(--qd-border)]">
                    <div className="grid grid-cols-[1fr_auto_auto] gap-2 border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-1.5 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">
                      <span>Source</span>
                      <span>Sample</span>
                      <span>Confidence</span>
                    </div>
                    {evidence.slice(0, 4).map((ev) => (
                      <div key={ev.evidence_id || ev.source} className="grid grid-cols-[1fr_auto_auto] gap-2 border-b border-[var(--qd-border)]/70 px-2 py-1.5 text-xs last:border-b-0">
                        <span className="min-w-0 truncate text-[var(--qd-text-2)]">{ev.source || "unknown"}</span>
                        <span className="font-mono text-[var(--qd-text-3)]">{ev.sample_n || 0}</span>
                        <span className="font-mono text-[var(--qd-text-3)]">{Number(ev.confidence || 0).toFixed(2)}</span>
                      </div>
                    ))}
                  </div>
                )}

                {limitations.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {limitations.slice(0, 4).map((item) => (
                      <span key={item} className="rounded-sm border border-amber-500/25 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] text-amber-500">
                        {item}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

const MetricTile = ({ label, value }) => (
  <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2 py-2">
    <div className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">{label}</div>
    <div className="mt-1 truncate font-mono text-xs font-semibold text-[var(--qd-text)]">{value}</div>
  </div>
);

const TypingIndicator = () => (
  <div className="flex gap-3.5 items-center animate-pulse">
    <div className="relative flex-shrink-0">
      <div className="w-8.5 h-8.5 rounded-lg bg-[var(--qd-accent)] flex items-center justify-center shadow-md">
        <Bot size={17} className="text-[var(--qd-accent-contrast)]" />
      </div>
      <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 bg-amber-500 border-2 border-[var(--qd-bg)] rounded-full animate-ping" title="Agent thinking" />
    </div>
    <div className="flex items-center gap-2 rounded-xl border border-[var(--qd-border)] bg-[var(--qd-surface)] px-4 py-3 shadow-xs">
      <span className="text-[12px] font-mono text-[var(--qd-text-3)] font-semibold uppercase tracking-wider">Hermes is thinking</span>
      <div className="flex items-center gap-1">
        <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "0ms" }} />
        <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "150ms" }} />
        <span className="h-1.5 w-1.5 rounded-full bg-[var(--qd-accent)] animate-bounce" style={{ animationDelay: "300ms" }} />
      </div>
    </div>
  </div>
);
