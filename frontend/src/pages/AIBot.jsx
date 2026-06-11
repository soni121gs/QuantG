import React, { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { AlertCircle, Bot, Send, Sparkles, User, ShieldAlert, Sliders, CheckCircle2, XCircle, ShieldCheck, HelpCircle, Activity } from "lucide-react";
import { Button } from "../components/ui/button";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";

const SESSION = "default";
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

export default function AIBot() {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("agent");
  const [aiStatus, setAiStatus] = useState(null);
  const [marketAnalysis, setMarketAnalysis] = useState(null);
  const [analysisBusy, setAnalysisBusy] = useState(false);
  
  // Real-time Dashboard state
  const [profile, setProfile] = useState(null);
  
  const endRef = useRef(null);

  const fetchProfile = () => {
    api.get("/profile").then((r) => setProfile(r.data)).catch(() => {});
  };

  useEffect(() => {
    api.get(`/ai/chat/${SESSION}`).then((r) => setMessages(r.data)).catch(() => {});
    api.get("/ai/status").then((r) => setAiStatus(r.data)).catch(() => {});
    fetchProfile();
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
      setMessages((m) => [...m, {
        id: `a-${Date.now()}`,
        role: "assistant",
        content: r.data.content,
        tools_used: r.data.tools_used,
        unavailable: r.data.unavailable,
        read_only: r.data.read_only,
        pending_action: r.data.pending_action
      }]);
    } catch (e) {
      setMessages((m) => [...m, { id: `err-${Date.now()}`, role: "assistant", content: `Error: ${e.response?.data?.detail || e.message}` }]);
    } finally { setBusy(false); }
  };

  const handleApproveAction = async (actionId) => {
    try {
      await api.post("/agent/action/approve", { action_id: actionId });
      setMessages((m) =>
        m.map((msg) =>
          msg.pending_action?.id === actionId
            ? { ...msg, pending_action: { ...msg.pending_action, status: "approved" } }
            : msg
        )
      );
      // Immediately refresh profile dashboard metrics in real time!
      fetchProfile();
    } catch (e) {
      alert(e.response?.data?.detail || "Action approval failed");
    }
  };

  const handleRejectAction = async (actionId) => {
    try {
      await api.post("/agent/action/reject", { action_id: actionId });
      setMessages((m) =>
        m.map((msg) =>
          msg.pending_action?.id === actionId
            ? { ...msg, pending_action: { ...msg.pending_action, status: "rejected" } }
            : msg
        )
      );
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
    <div className="flex h-[calc(100vh-156px)] flex-col gap-4" data-testid="ai-bot-page">
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
            <Button
              type="button"
              onClick={runMarketAnalysis}
              disabled={analysisBusy}
              variant="primary"
              size="sm"
            >
              {analysisBusy ? "Analyzing..." : "Generate Analysis"}
            </Button>
          </div>
          {marketAnalysis?.content && (
            <div className="rounded border border-[var(--qd-border)] bg-[var(--qd-bg)] p-4 text-sm leading-relaxed text-[var(--qd-text-2)] whitespace-pre-wrap">
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
                className="rounded border border-[var(--qd-border)] p-3.5 text-left font-mono text-xs text-[var(--qd-text-2)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] transition-all bg-[var(--qd-surface-2)]/20 hover:bg-[var(--qd-surface-2)]/50"
              >
                {prompt}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Main Grid Structure */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 flex-1 min-h-0">
        
        {/* Left Column: Chat Area */}
        <div className="lg:col-span-8 flex flex-col min-h-0 qd-card">
          <div className="flex-1 overflow-y-auto p-4 space-y-4" data-testid="messages">
            {messages.length === 0 && (
              <div className="text-center py-16">
                <Sparkles className="mx-auto text-[var(--qd-accent)] mb-4 animate-pulse" size={32} />
                <p className="font-mono text-sm text-[var(--qd-text-2)] mb-8">How can I protect or configure your trading setup today?</p>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-xl mx-auto">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} onClick={() => send(s)} className="text-left text-xs font-mono text-[var(--qd-text-2)] border border-[var(--qd-border)] hover:border-[var(--qd-accent)] hover:text-[var(--qd-text)] p-4 transition-all rounded bg-[var(--qd-surface-2)]/10 hover:bg-[var(--qd-surface-2)]/40 hover:-translate-y-0.5">
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m, i) => (
              <Message
                key={m.id || `m-${i}`}
                m={m}
                onApprove={handleApproveAction}
                onReject={handleRejectAction}
              />
            ))}
            {busy && <Message key="typing" m={{ role: "assistant", content: "..." }} />}
            <div ref={endRef} />
          </div>

          {/* Send Input */}
          <div className="border-t border-[var(--qd-border)] p-3 flex items-center gap-2 bg-[var(--qd-bg)]/20 rounded-b-xl">
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder="Configure drawdown controls, toggle emergency kill, or update settings..."
              className="flex-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] focus:ring-1 focus:ring-[var(--qd-accent)] outline-none px-4 py-3 text-sm text-[var(--qd-text)] font-mono rounded"
              data-testid="ai-input"
            />
            <Button onClick={() => send()} disabled={busy || !text.trim()} variant="primary" size="lg" data-testid="ai-send-btn" aria-label="Send message">
              <Send size={16} />
            </Button>
          </div>
        </div>

        {/* Right Column: Live Risk Management Dashboard */}
        <div className="lg:col-span-4 flex flex-col gap-4 overflow-y-auto">
          
          {/* Live Status and Mode Indicator */}
          <div className="qd-card p-5 flex flex-col gap-4">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)]/50 pb-3">
              <h2 className="font-head font-bold text-white text-sm uppercase tracking-wider flex items-center gap-2"><ShieldCheck size={16} className="text-emerald-400" /> System Governance</h2>
              <div className="flex items-center gap-2">
                <span className={`w-2.5 h-2.5 rounded-full ${profile?.paper_mode ? "bg-rose-500 glow-pulse-red" : "bg-emerald-500 glow-pulse-green"}`} />
                <span className="font-mono text-[10px] font-bold text-[var(--qd-text)] uppercase">{profile?.paper_mode ? "Sandbox Paper" : "Production Live"}</span>
              </div>
            </div>

            {profile ? (
              <div className="space-y-4">
                {/* Active Mode Banner */}
                <div className={`p-3.5 rounded border text-center font-mono text-xs font-bold ${profile.paper_mode ? "bg-rose-950/20 border-rose-500/30 text-rose-400" : "bg-emerald-950/20 border-emerald-500/30 text-emerald-400"}`}>
                  {profile.paper_mode ? "PAPER / EMERGENCY PAUSE ACTIVE" : "TERMINAL ROUTING LIVE ORDERS"}
                </div>

                {/* Grid metrics */}
                <div className="grid grid-cols-2 gap-3">
                  <div className="p-3 bg-[var(--qd-surface-2)]/30 rounded border border-[var(--qd-border)]/50">
                    <div className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">Drawdown Limit</div>
                    <div className="font-head font-bold text-sm text-white mt-1">{Number(profile.max_daily_loss).toLocaleString()} INR</div>
                  </div>
                  <div className="p-3 bg-[var(--qd-surface-2)]/30 rounded border border-[var(--qd-border)]/50">
                    <div className="font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">Trades Speed Limit</div>
                    <div className="font-head font-bold text-sm text-white mt-1">{profile.max_trades_per_day || "Unlimited"} / Day</div>
                  </div>
                </div>

                {/* Progress Drawdown Bar */}
                <div className="space-y-1.5 mt-2">
                  <div className="flex justify-between font-mono text-[9px] text-[var(--qd-text-3)] uppercase tracking-wider">
                    <span>Daily Drawdown Margin</span>
                    <span className="text-amber-400 font-bold">Risk Boundary</span>
                  </div>
                  <div className="w-full bg-[var(--qd-bg)] rounded-full h-2.5 overflow-hidden border border-[var(--qd-border)]/60">
                    <div className="bg-gradient-to-r from-emerald-500 via-amber-500 to-rose-500 h-full rounded-full" style={{ width: "65%" }}></div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-center font-mono text-xs text-[var(--qd-text-3)] py-6 animate-pulse">Loading profile risk metrics...</div>
            )}
          </div>

          {/* Capital Allocations & Size Limits */}
          <div className="qd-card p-5">
            <h2 className="font-head font-bold text-white text-sm uppercase tracking-wider border-b border-[var(--qd-border)]/50 pb-3 mb-4 flex items-center gap-2"><Sliders size={16} className="text-blue-400" /> Capital & Position Size Limits</h2>
            
            {profile ? (
              <div className="space-y-4">
                <div className="flex items-center justify-between p-2.5 bg-[var(--qd-surface-2)]/20 rounded border border-[var(--qd-border)]/40">
                  <div className="font-mono text-xs text-[var(--qd-text-2)]">Per-Strategy Cap</div>
                  <div className="font-mono text-xs font-extrabold text-blue-400">{Number(profile.per_strategy_capital || 0).toLocaleString()} INR</div>
                </div>
                <div className="flex items-center justify-between p-2.5 bg-[var(--qd-surface-2)]/20 rounded border border-[var(--qd-border)]/40">
                  <div className="font-mono text-xs text-[var(--qd-text-2)]">Max Position Size</div>
                  <div className="font-mono text-xs font-extrabold text-blue-400">{Number(profile.max_position_size || 0).toLocaleString()} INR</div>
                </div>
                <div className="flex items-center justify-between p-2.5 bg-[var(--qd-surface-2)]/20 rounded border border-[var(--qd-border)]/40">
                  <div className="font-mono text-xs text-[var(--qd-text-2)]">Default Lot Quantity</div>
                  <div className="font-mono text-xs font-extrabold text-blue-400">{profile.default_qty || "Not Set"}</div>
                </div>
              </div>
            ) : (
              <div className="text-center font-mono text-xs text-[var(--qd-text-3)] py-6 animate-pulse">Loading size boundaries...</div>
            )}
          </div>

          {/* Quick Help & Guidelines */}
          <div className="qd-card p-5 text-xs leading-relaxed text-[var(--qd-text-2)]">
            <h3 className="font-head font-bold text-white uppercase text-xs pb-2 flex items-center gap-2"><HelpCircle size={14} className="text-[var(--qd-accent)]" /> Copilot Guidelines</h3>
            <p className="mt-1 font-mono">You can speak freely with the co-pilot to adjust parameters. All critical changes require your explicit click approval here before committing to Upstox live routes.</p>
          </div>

        </div>

      </div>
    </div>
  );
}

const Message = ({ m, onApprove, onReject }) => {
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

  const getActionDescription = (actionName, params) => {
    const list = [];
    if (params?.paper_mode !== undefined) {
      list.push(`Set Trading Mode to: ${params.paper_mode ? "PAPER / EMERGENCY PAUSE" : "LIVE TRADING"}`);
    }
    if (params?.max_daily_loss !== undefined) {
      list.push(`Change Daily Loss Limit to: ${Number(params.max_daily_loss).toLocaleString()} INR`);
    }
    if (params?.max_position_size !== undefined) {
      list.push(`Change Maximum Position Size to: ${Number(params.max_position_size).toLocaleString()} INR`);
    }
    if (params?.per_strategy_capital !== undefined) {
      list.push(`Set Strategy Capital Allocation: ${Number(params.per_strategy_capital).toLocaleString()} INR`);
    }
    if (params?.default_qty !== undefined) {
      list.push(`Change Default Order Quantity: ${params.default_qty}`);
    }
    if (params?.max_trades_per_day !== undefined) {
      list.push(`Adjust Max Trades Per Day: ${params.max_trades_per_day}`);
    }
    return list.map((item, index) => <div key={index} className="font-mono text-xs text-[var(--qd-text)] py-1">{item}</div>);
  };

  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : ""}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded bg-[var(--qd-accent)] flex items-center justify-center flex-shrink-0 shadow-md">
          <Bot size={16} className="text-white" />
        </div>
      )}
      <div className={`max-w-[85%] px-4 py-3 rounded-lg shadow-sm border ${isUser ? "qd-force-white bg-[var(--qd-accent)] border-[var(--qd-accent)]" : "bg-[var(--qd-surface-2)]/65 border-[var(--qd-border)] text-[var(--qd-text)]"}`}>
        <pre className="whitespace-pre-wrap font-mono text-sm leading-relaxed font-sans">{m.content}</pre>

        {hasAction && (
          <div className="mt-4 p-4 rounded border border-[var(--qd-border)] bg-[var(--qd-bg)]/80 relative overflow-hidden">
            <div className="flex items-center justify-between border-b border-[var(--qd-border)] pb-2 mb-3">
              <div className="font-mono text-[9px] uppercase tracking-wider text-[var(--qd-text-3)] font-semibold">AI System Action Proposal</div>
              {getActionBadge(m.pending_action.action, m.pending_action.params)}
            </div>

            <div className="space-y-1 my-3 bg-[var(--qd-surface-2)]/50 p-3 rounded border border-[var(--qd-border)]/40">
              {getActionDescription(m.pending_action.action, m.pending_action.params)}
            </div>

            {m.pending_action.status === "pending" && (
              <div className="flex gap-2 mt-4">
                <Button
                  onClick={() => onApprove(m.pending_action.id)}
                  className="flex-1 font-mono text-[11px] uppercase tracking-wider"
                  variant="success"
                  size="sm"
                >
                  Approve Action
                </Button>
                <Button
                  onClick={() => onReject(m.pending_action.id)}
                  className="flex-1 font-mono text-[11px] uppercase tracking-wider"
                  variant="danger"
                  size="sm"
                >
                  Decline
                </Button>
              </div>
            )}

            {m.pending_action.status === "approved" && (
              <div className="mt-3 py-2 px-3 rounded bg-emerald-500/10 border border-emerald-500/30 flex items-center gap-2 text-emerald-400 text-xs font-mono">
                <CheckCircle2 size={14} className="flex-shrink-0" />
                <span>Action approved and successfully committed to terminal settings.</span>
              </div>
            )}

            {m.pending_action.status === "rejected" && (
              <div className="mt-3 py-2 px-3 rounded bg-rose-500/10 border border-rose-500/30 flex items-center gap-2 text-rose-400 text-xs font-mono">
                <XCircle size={14} className="flex-shrink-0" />
                <span>Action proposal rejected and discarded.</span>
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
            <div>
              Some read-only data was unavailable. The answer may be incomplete.
            </div>
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
