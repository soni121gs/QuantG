import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { AlertCircle, ArrowRight, Bot, Send, Sparkles, User, ShieldAlert, Sliders, CheckCircle2, XCircle, ShieldCheck, HelpCircle, Activity, MessageSquare, Plus } from "lucide-react";
import { Button } from "../components/ui/button";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";
import { useExecutionState } from "../hooks/useExecutionState";

// Minimal, dependency-free markdown renderer for agent replies (bold, inline
// code, bullet/numbered lists, headings). Builds real React nodes — no
// dangerouslySetInnerHTML — so Gemini's markdown stops showing as raw ** **.
const renderInline = (text) => {
  const parts = [];
  const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let lastIndex = 0;
  let match;
  let key = 0;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(<strong key={key++} className="font-semibold text-[var(--qd-text)]">{token.slice(2, -2)}</strong>);
    } else {
      parts.push(<code key={key++} className="rounded bg-[var(--qd-bg)] px-1 py-0.5 font-mono text-[0.85em] text-[var(--qd-accent)]">{token.slice(1, -1)}</code>);
    }
    lastIndex = regex.lastIndex;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
};

const renderMarkdown = (text) => {
  const lines = String(text || "").split("\n");
  const blocks = [];
  let list = null;
  let para = [];
  const flushPara = () => { if (para.length) { blocks.push({ type: "p", content: para.join(" ") }); para = []; } };
  const flushList = () => { if (list) { blocks.push(list); list = null; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { flushPara(); flushList(); continue; }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    const ul = /^[-*]\s+(.*)$/.exec(line);
    const ol = /^\d+\.\s+(.*)$/.exec(line);
    if (h) { flushPara(); flushList(); blocks.push({ type: "h", level: h[1].length, content: h[2] }); }
    else if (ul) { flushPara(); if (!list || list.type !== "ul") { flushList(); list = { type: "ul", items: [] }; } list.items.push(ul[1]); }
    else if (ol) { flushPara(); if (!list || list.type !== "ol") { flushList(); list = { type: "ol", items: [] }; } list.items.push(ol[1]); }
    else { flushList(); para.push(line); }
  }
  flushPara();
  flushList();
  return blocks.map((b, i) => {
    if (b.type === "h") {
      return <div key={i} className={`font-head font-bold text-[var(--qd-text)] mt-2 first:mt-0 ${b.level === 1 ? "text-base" : "text-sm"}`}>{renderInline(b.content)}</div>;
    }
    if (b.type === "ul") return <ul key={i} className="my-1.5 list-disc space-y-1 pl-5">{b.items.map((it, j) => <li key={j}>{renderInline(it)}</li>)}</ul>;
    if (b.type === "ol") return <ol key={i} className="my-1.5 list-decimal space-y-1 pl-5">{b.items.map((it, j) => <li key={j}>{renderInline(it)}</li>)}</ol>;
    return <p key={i} className="my-1.5 leading-relaxed first:mt-0">{renderInline(b.content)}</p>;
  });
};

const SUGGESTIONS = [
  "Verify my active strategies and drawdown limits",
  "Check Upstox feed and market status",
  "Lower my daily loss limit to 6000 INR for protection",
  "Switch terminal to emergency paper mode",
];
const MODES = [
  { id: "agent", label: "Ask Agent" },
  { id: "brief", label: "Market Brief" },
];
const BRIEF_PROMPTS = [
  "Create a short market brief for NIFTY, BANKNIFTY, and SENSEX.",
  "Summarize risks before placing live orders today.",
  "Build a checklist for index-option trades using Upstox instrument keys.",
];

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
  const [mode, setMode] = useState("agent");
  const [aiStatus, setAiStatus] = useState(null);
  const [marketAnalysis, setMarketAnalysis] = useState(null);
  const [analysisBusy, setAnalysisBusy] = useState(false);
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
    <div className="space-y-5 pb-4" data-testid="ai-bot-page">
      <PageHeader
        eyebrow="Active Risk & Co-Pilot"
        title="Ask QuantG Agent"
        subtitle="Trading diagnostics, market briefings, and governed action proposals in one command surface."
        badge={<StatusBadge tone={profile?.paper_mode ? "paper" : "live"}>{profile?.paper_mode ? "Paper" : "Live"}</StatusBadge>}
        actions={
          <div className="flex rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-bg)] p-1">
            {MODES.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setMode(item.id)}
                className={`rounded-[var(--qd-radius-sm)] px-4 py-1.5 font-mono text-[10px] uppercase tracking-wider ${mode === item.id ? "qd-force-white bg-[var(--qd-accent)]" : "text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"}`}
              >
                {item.label}
              </button>
            ))}
          </div>
        }
      />

      {mode === "brief" && (
        <div className="qd-card p-5">
          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between border-b border-[var(--qd-border)]/50 pb-4 mb-4">
            <div>
              <h2 className="font-head text-lg font-semibold text-white flex items-center gap-2"><Activity size={18} className="text-[var(--qd-accent)]" /> Market Analysis</h2>
              <p className="mt-1 text-xs text-[var(--qd-text-2)]">Gemini evaluates live strategy scores, index trend structure, and Upstox feed state.</p>
            </div>
            <Button type="button" onClick={runMarketAnalysis} disabled={analysisBusy} variant="primary" size="sm">
              {analysisBusy ? "Analyzing..." : "Generate Analysis"}
            </Button>
          </div>
          {marketAnalysis?.content && (
            <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-4 text-sm leading-relaxed text-[var(--qd-text-2)]">
              {renderMarkdown(marketAnalysis.content)}
            </div>
          )}
          <div className="mt-4 grid grid-cols-1 gap-2 md:grid-cols-3">
            {BRIEF_PROMPTS.map((prompt) => (
              <button
                key={prompt}
                type="button"
                onClick={() => { setMode("agent"); send(prompt); }}
                className="rounded border border-[var(--qd-border)] p-3.5 text-left font-mono text-xs text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] transition-all bg-[var(--qd-surface-2)]/20 hover:bg-[var(--qd-surface-2)]/50"
              >
                {prompt}
              </button>
            ))}
          </div>
        </div>
      )}

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
              messages.map((m, i) => (
                <Message key={m.id || `m-${i}`} m={m} profile={profile} onApprove={handleApproveAction} onReject={handleRejectAction} />
              ))
            )}
            {busy && <TypingIndicator />}
            <div ref={endRef} />
          </div>

          {/* Composer: persistent quick actions + multi-line input */}
          <div className="border-t border-[var(--qd-border)] bg-[var(--qd-bg)]/20">
            <div className="flex flex-wrap gap-1.5 px-3 pt-3">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => send(s)}
                  disabled={busy}
                  className="rounded-full border border-[var(--qd-border)] px-3 py-1 text-[11px] text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] disabled:opacity-50"
                >
                  {s}
                </button>
              ))}
            </div>
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

        {/* Right: compact, always-visible context */}
        <div className="lg:col-span-4 space-y-4">

          {/* System governance */}
          <div className="qd-card p-4">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)]/50 pb-2.5">
              <h2 className="font-head font-bold text-white text-xs uppercase tracking-wider flex items-center gap-2"><ShieldCheck size={15} className="text-emerald-400" /> System Governance</h2>
              <div className="flex items-center gap-1.5">
                <span className={`w-2 h-2 rounded-full ${profile?.paper_mode ? "bg-rose-500" : "bg-emerald-500"}`} />
                <span className="font-mono text-[9px] font-bold text-[var(--qd-text)] uppercase">{profile?.paper_mode ? "Sandbox Paper" : "Production Live"}</span>
              </div>
            </div>

            {profile ? (
              <div className="mt-3 space-y-3">
                <div className={`px-3 py-2 rounded border text-center font-mono text-[11px] font-bold ${profile.paper_mode ? "bg-rose-950/20 border-rose-500/30 text-rose-400" : "bg-emerald-950/20 border-emerald-500/30 text-emerald-400"}`}>
                  {profile.paper_mode ? "PAPER / EMERGENCY PAUSE ACTIVE" : "TERMINAL ROUTING LIVE ORDERS"}
                </div>

                <div className="grid grid-cols-2 gap-2">
                  <div className="p-2.5 bg-[var(--qd-surface-2)]/30 rounded border border-[var(--qd-border)]/50">
                    <div className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">Loss Limit</div>
                    <div className="font-head font-bold text-sm text-white mt-0.5">{Number(profile.max_daily_loss || 0).toLocaleString()} INR</div>
                  </div>
                  <div className="p-2.5 bg-[var(--qd-surface-2)]/30 rounded border border-[var(--qd-border)]/50">
                    <div className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">Trades / Day</div>
                    <div className="font-head font-bold text-sm text-white mt-0.5">{profile.max_trades_per_day || "Unlimited"}</div>
                  </div>
                </div>

                <div className="space-y-1.5">
                  <div className="flex justify-between font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">
                    <span>Daily Drawdown Used</span>
                    <span className={`font-bold ${drawdownTone === "rose" ? "text-rose-400" : drawdownTone === "amber" ? "text-amber-400" : "text-emerald-400"}`}>{drawdownUsedPct.toFixed(0)}%</span>
                  </div>
                  <div className="w-full bg-[var(--qd-bg)] rounded-full h-2 overflow-hidden border border-[var(--qd-border)]/60">
                    <div className={`h-full rounded-full transition-all ${drawdownTone === "rose" ? "bg-rose-500" : drawdownTone === "amber" ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${drawdownUsedPct}%` }} />
                  </div>
                  <div className="font-mono text-[9px] text-[var(--qd-text-3)]">
                    Net P&L {netPnl >= 0 ? "+" : ""}{netPnl.toLocaleString()} INR {lossLimit > 0 ? `of ${lossLimit.toLocaleString()} INR limit` : "· no loss limit set"}
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-center font-mono text-xs text-[var(--qd-text-3)] py-5 animate-pulse">Loading risk metrics…</div>
            )}
          </div>

          {/* Capital limits */}
          <div className="qd-card p-4">
            <h2 className="font-head font-bold text-white text-xs uppercase tracking-wider border-b border-[var(--qd-border)]/50 pb-2.5 flex items-center gap-2"><Sliders size={15} className="text-blue-400" /> Capital &amp; Size Limits</h2>
            {profile ? (
              <div className="mt-3 space-y-2">
                <CompactRow label="Per-Strategy Cap" value={`${Number(profile.per_strategy_capital || 0).toLocaleString()} INR`} />
                <CompactRow label="Max Position Size" value={`${Number(profile.max_position_size || 0).toLocaleString()} INR`} />
                <CompactRow label="Default Lot Qty" value={profile.default_qty || "Not set"} />
              </div>
            ) : (
              <div className="text-center font-mono text-xs text-[var(--qd-text-3)] py-5 animate-pulse">Loading size boundaries…</div>
            )}
          </div>

          {/* What the agent can do */}
          <div className="qd-card p-4 text-xs leading-relaxed">
            <h3 className="font-head font-bold text-white uppercase text-xs pb-2.5 mb-2.5 flex items-center gap-2 border-b border-[var(--qd-border)]/50"><HelpCircle size={14} className="text-[var(--qd-accent)]" /> What I Can Do</h3>

            <div className="font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)] flex items-center gap-1.5"><ShieldCheck size={12} className="text-emerald-400" /> Reads automatically</div>
            <ul className="mt-1.5 space-y-0.5 text-[var(--qd-text-2)]">
              <li>· Orders, positions &amp; execution state</li>
              <li>· Strategies, errors &amp; rejected orders</li>
              <li>· Upstox feed, market status &amp; ticks</li>
              <li>· Daily P&amp;L, loss limit &amp; capital usage</li>
            </ul>

            <div className="mt-2.5 pt-2.5 border-t border-[var(--qd-border)]/50 font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)] flex items-center gap-1.5"><Sliders size={12} className="text-blue-400" /> Can change — you approve</div>
            <ul className="mt-1.5 space-y-0.5 text-[var(--qd-text-2)]">
              <li>· Daily loss limit &amp; max trades / day</li>
              <li>· Position size &amp; per-strategy capital</li>
              <li>· Default qty · Paper ↔ Live (kill)</li>
            </ul>

            <div className="mt-2.5 pt-2.5 border-t border-[var(--qd-border)]/50 flex items-start gap-1.5 text-[var(--qd-text-3)]">
              <ShieldAlert size={12} className="mt-0.5 text-[var(--qd-loss)] flex-shrink-0" />
              <span>Never places, cancels, or exits trades. Every change needs your click.</span>
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}

const CompactRow = ({ label, value }) => (
  <div className="flex items-center justify-between p-2.5 bg-[var(--qd-surface-2)]/20 rounded border border-[var(--qd-border)]/40">
    <div className="font-mono text-xs text-[var(--qd-text-2)]">{label}</div>
    <div className="font-mono text-xs font-extrabold text-blue-400">{value}</div>
  </div>
);

const EmptyState = ({ onPick }) => (
  <div className="flex h-full flex-col items-center justify-center text-center px-4">
    <div className="w-14 h-14 rounded-2xl bg-[var(--qd-accent)]/15 border border-[var(--qd-accent)]/30 flex items-center justify-center mb-4">
      <Sparkles className="text-[var(--qd-accent)]" size={26} />
    </div>
    <h3 className="font-head text-lg font-bold text-[var(--qd-text)]">How can I protect your trading setup?</h3>
    <p className="mt-1.5 max-w-md text-sm text-[var(--qd-text-2)]">
      I read your live orders, positions, risk and feed health, then answer grounded in that data. I can also propose risk-setting changes for you to approve.
    </p>
    <div className="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full max-w-xl">
      {SUGGESTIONS.map((s) => (
        <button
          key={s}
          onClick={() => onPick(s)}
          className="text-left text-xs text-[var(--qd-text-2)] border border-[var(--qd-border)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] p-3.5 rounded-[var(--qd-radius-sm)] bg-[var(--qd-surface-2)]/20 hover:bg-[var(--qd-surface-2)]/50 transition-all hover:-translate-y-0.5"
        >
          {s}
        </button>
      ))}
    </div>
  </div>
);

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

const Message = ({ m, profile, onApprove, onReject }) => {
  const isUser = m.role === "user";
  const hasAction = !isUser && m.pending_action;

  const getActionBadge = (actionName, params) => {
    if (params?.paper_mode === true || actionName === "emergency_kill") {
      return <span className="action-badge badge-kill"><ShieldAlert size={11} /> Kill Switch</span>;
    }
    if (params?.max_daily_loss !== undefined) {
      return <span className="action-badge badge-drawdown"><Sliders size={11} /> Drawdown Control</span>;
    }
    if (params?.max_position_size !== undefined || params?.per_strategy_capital !== undefined) {
      return <span className="action-badge badge-size"><Sliders size={11} /> Position Sizing</span>;
    }
    return <span className="action-badge badge-normal"><Sliders size={11} /> Parameter Adaptation</span>;
  };

  const changeRows = (params) => Object.keys(params || {})
    .filter((k) => FIELD_LABELS[k])
    .map((k) => {
      const cfg = FIELD_LABELS[k];
      const current = profile ? profile[k] : undefined;
      const hasCurrent = current !== undefined && current !== null && current !== "";
      return (
        <div key={k} className="flex items-center justify-between gap-3 py-1.5 border-b border-[var(--qd-border)]/30 last:border-0">
          <span className="font-mono text-[10px] uppercase tracking-wide text-[var(--qd-text-3)]">{cfg.label}</span>
          <span className="flex items-center gap-2 font-mono text-xs">
            {hasCurrent && <span className="text-[var(--qd-text-3)] line-through">{cfg.fmt(current)}</span>}
            <ArrowRight size={11} className="text-[var(--qd-accent)]" />
            <span className="font-bold text-[var(--qd-text)]">{cfg.fmt(params[k])}</span>
          </span>
        </div>
      );
    });

  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : ""}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded bg-[var(--qd-accent)] flex items-center justify-center flex-shrink-0 shadow-md">
          <Bot size={16} className="text-white" />
        </div>
      )}
      <div className={`max-w-[85%] px-4 py-3 rounded-lg shadow-sm border ${isUser ? "qd-force-white bg-[var(--qd-accent)] border-[var(--qd-accent)]" : "bg-[var(--qd-surface-2)]/65 border-[var(--qd-border)] text-[var(--qd-text)]"}`}>
        {isUser ? (
          <div className="whitespace-pre-wrap text-sm leading-relaxed">{m.content}</div>
        ) : (
          <div className="text-sm leading-relaxed">{renderMarkdown(m.content)}</div>
        )}

        {hasAction && (
          <div className="mt-4 rounded-lg border border-[var(--qd-accent)]/40 bg-[var(--qd-bg)]/80 overflow-hidden">
            <div className="flex items-center justify-between gap-2 border-b border-[var(--qd-border)] bg-[var(--qd-surface-2)]/40 px-3 py-2">
              <div className="font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-2)] font-semibold flex items-center gap-1.5">
                <ShieldCheck size={12} className="text-[var(--qd-accent)]" /> Action proposal · needs approval
              </div>
              {getActionBadge(m.pending_action.action, m.pending_action.params)}
            </div>

            <div className="px-3 py-2">
              {changeRows(m.pending_action.params)}
            </div>

            {m.pending_action.status === "pending" && (
              <div className="flex gap-2 p-3 pt-0">
                <Button onClick={() => onApprove(m.pending_action.id)} className="flex-1 font-mono text-[11px] uppercase tracking-wider" variant="success" size="sm">
                  <CheckCircle2 size={14} /> Approve
                </Button>
                <Button onClick={() => onReject(m.pending_action.id)} className="flex-1 font-mono text-[11px] uppercase tracking-wider" variant="danger" size="sm">
                  <XCircle size={14} /> Decline
                </Button>
              </div>
            )}

            {m.pending_action.status === "approved" && (
              <div className="m-3 mt-0 py-2 px-3 rounded bg-emerald-500/10 border border-emerald-500/30 flex items-center gap-2 text-emerald-400 text-xs font-mono">
                <CheckCircle2 size={14} className="flex-shrink-0" />
                <span>Approved and committed to your terminal settings.</span>
              </div>
            )}

            {m.pending_action.status === "rejected" && (
              <div className="m-3 mt-0 py-2 px-3 rounded bg-rose-500/10 border border-rose-500/30 flex items-center gap-2 text-rose-400 text-xs font-mono">
                <XCircle size={14} className="flex-shrink-0" />
                <span>Proposal rejected and discarded.</span>
              </div>
            )}
          </div>
        )}

        {!isUser && m.tools_used?.length > 0 && (
          <div className="mt-4 border-t border-[var(--qd-border)]/70 pt-2.5">
            <div className="mb-1.5 font-mono text-[9px] uppercase tracking-widest text-[var(--qd-text-3)] font-semibold">Active context feed</div>
            <div className="flex flex-wrap gap-1">
              {m.tools_used.map((tool) => (
                <span
                  key={tool.name}
                  className={`rounded border px-2 py-0.5 font-mono text-[9px] ${tool.status === "ok" ? "border-[var(--qd-border)] text-[var(--qd-text-3)]" : "border-[var(--qd-loss)] text-[var(--qd-loss)]"}`}
                  title={tool.error || tool.name}
                >
                  {tool.name}
                </span>
              ))}
            </div>
          </div>
        )}
        {!isUser && m.unavailable?.length > 0 && (
          <div className="mt-2.5 flex gap-2 rounded border border-[var(--qd-loss)]/60 bg-[var(--qd-bg)] p-2 text-[11px] text-[var(--qd-loss)]">
            <AlertCircle size={14} className="mt-0.5 flex-shrink-0" />
            <div>Some read-only data was unavailable. The answer may be incomplete.</div>
          </div>
        )}
      </div>
      {isUser && (
        <div className="w-8 h-8 rounded border border-[var(--qd-border)] bg-[var(--qd-surface-2)] flex items-center justify-center flex-shrink-0 shadow-md">
          <User size={16} className="text-white" />
        </div>
      )}
    </div>
  );
};
