import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";
import { formatApiErrorDetail } from "../lib/api";
import { Activity, Mail, Lock, User, AlertCircle, Cpu, ShieldCheck, Smartphone, Zap, Sparkles, TrendingUp, BarChart2 } from "lucide-react";

export default function Auth({ mode: initialMode = "login" }) {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const [isLogin, setIsLogin] = useState(initialMode === "login");
  const [form, setForm] = useState({ email: "", password: "", name: "" });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingApproval, setPendingApproval] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      if (!form.email || !form.password) {
        setErr("Email and password are required");
        setBusy(false);
        return;
      }
      if (form.email.indexOf("@") === -1) {
        setErr("Please enter a valid email");
        setBusy(false);
        return;
      }
      if (form.password.length < 6) {
        setErr("Password must be at least 6 characters");
        setBusy(false);
        return;
      }

      if (isLogin) {
        await login(form.email.toLowerCase().trim(), form.password);
        navigate("/dashboard");
      } else {
        if (!form.name || form.name.trim().length === 0) {
          setErr("Name is required");
          setBusy(false);
          return;
        }
        const user = await register(form.email.toLowerCase().trim(), form.password, form.name.trim());
        
        // Check if registration went to pending approval
        if (user && !user.approved && user.role !== "owner") {
          localStorage.removeItem("qd_token"); // Remove token so it doesn't try to log them in automatically
          setPendingApproval(true);
        } else {
          navigate("/dashboard");
        }
      }
    } catch (e) {
      const errMsg = e.response?.data?.detail || e.message || "Authentication failed";
      setErr(formatApiErrorDetail(errMsg));
    } finally {
      setBusy(false);
    }
  };

  if (pendingApproval) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[var(--qd-bg)] px-4 py-12 qd-grid-bg relative overflow-hidden">
        {/* Glow Effects */}
        <div className="absolute top-1/4 left-1/4 w-96 h-96 bg-[var(--qd-accent)]/10 rounded-full blur-[120px] pointer-events-none" />
        <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-[var(--qd-cyan)]/10 rounded-full blur-[120px] pointer-events-none" />
        
        <div className="qd-card p-8 md:p-10 max-w-lg w-full text-center space-y-6 relative z-10 border border-[var(--qd-cyan)]/25 bg-[var(--qd-surface-2)]/90 backdrop-blur-md">
          <div className="w-20 h-20 bg-[var(--qd-cyan)]/10 border border-[var(--qd-cyan)]/30 rounded-full flex items-center justify-center mx-auto text-[var(--qd-cyan)] animate-pulse">
            <ShieldCheck size={40} />
          </div>
          
          <div className="space-y-2">
            <h2 className="text-3xl font-head font-extrabold tracking-tight text-white">Registration Complete</h2>
            <div className="text-xs font-mono text-[var(--qd-cyan)] tracking-wider uppercase font-semibold">
              Pending Owner Approval
            </div>
          </div>

          <p className="text-sm text-[var(--qd-text-2)] leading-relaxed">
            Welcome to the <span className="text-white font-semibold">QuantG Institutional Terminal</span>. As an institutional-grade platform, new trader registries must be verified and approved by the system owner.
          </p>

          <div className="p-4 bg-[var(--qd-bg)]/80 border border-[var(--qd-border)] rounded-md text-left space-y-2">
            <div className="flex justify-between text-xs font-mono text-[var(--qd-text-3)]">
              <span>REGISTRY ID:</span>
              <span className="text-[var(--qd-text-2)] font-semibold uppercase">{form.email.split("@")[0]}-PENDING</span>
            </div>
            <div className="flex justify-between text-xs font-mono text-[var(--qd-text-3)]">
              <span>ACCOUNT STATUS:</span>
              <span className="text-[var(--qd-warn)] font-semibold">AWAITING_APPROVAL</span>
            </div>
          </div>

          <p className="text-xs text-[var(--qd-text-3)] font-mono leading-normal">
            The owner has been notified. Once approved, your default HFT strategies will be seeded and your login unlocked.
          </p>

          <button
            onClick={() => {
              setPendingApproval(false);
              setIsLogin(true);
            }}
            className="w-full bg-gradient-to-r from-[var(--qd-accent)] to-[var(--qd-cyan)] hover:opacity-90 active:scale-95 text-white py-3 px-4 font-mono text-sm uppercase tracking-wider transition-all duration-200 rounded-md font-bold shadow-lg"
          >
            Back to Login Gateway
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen grid lg:grid-cols-2 bg-[var(--qd-bg)] qd-grid-bg relative overflow-hidden">
      {/* Background gradients */}
      <div className="absolute top-0 left-0 w-[50vw] h-[50vh] bg-[var(--qd-accent)]/5 rounded-full blur-[160px] pointer-events-none" />
      <div className="absolute bottom-0 right-0 w-[50vw] h-[50vh] bg-[var(--qd-cyan)]/5 rounded-full blur-[160px] pointer-events-none" />

      {/* Left Column: Brand Showcase and Landing Info */}
      <div className="flex flex-col justify-between p-8 lg:p-16 relative z-10">
        {/* Header Branding */}
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 bg-gradient-to-tr from-[var(--qd-accent)] to-[var(--qd-cyan)] rounded flex items-center justify-center shadow-lg shadow-[var(--qd-accent)]/20 animate-pulse">
            <Activity size={20} className="text-white" strokeWidth={2.5} />
          </div>
          <span className="font-head font-extrabold tracking-tight text-white text-xl">
            QUANT<span className="cyber-accent-text">G</span>
            <span className="text-[11px] font-mono text-[var(--qd-cyan)] ml-1.5 px-1.5 py-0.5 border border-[var(--qd-cyan)]/30 rounded bg-[var(--qd-cyan)]/10 font-bold uppercase tracking-widest">
              Inst
            </span>
          </span>
        </div>

        {/* Dynamic Corporate Pitch */}
        <div className="my-12 lg:my-auto max-w-lg space-y-8">
          <div className="space-y-4">
            <div className="inline-flex items-center gap-2 px-3 py-1 bg-[var(--qd-surface-2)] border border-[var(--qd-border)] rounded-full text-xs font-mono text-[var(--qd-cyan)] uppercase tracking-wider">
              <Sparkles size={12} className="text-[var(--qd-cyan)] animate-spin" />
              Next-Gen Algorithmic Suite
            </div>
            <h1 className="font-head text-4xl lg:text-5xl font-black tracking-tight text-white leading-tight">
              Institutional-grade <br />
              <span className="cyber-gradient-text">HFT Trading Terminal.</span>
            </h1>
            <p className="text-base text-[var(--qd-text-2)] leading-relaxed">
              A high-precision, low-latency execution desk tailored for index options and commodities, routed through Upstox API v2.
            </p>
          </div>

          {/* Micro-Features grid with glowing neon elements */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="qd-card-tight p-4 bg-[var(--qd-surface)]/40 hover:bg-[var(--qd-surface-2)]/60">
              <div className="text-[var(--qd-cyan)] mb-2"><Zap size={20} /></div>
              <h3 className="text-white font-bold text-sm">Upstox HFT Pipeline</h3>
              <p className="text-xs text-[var(--qd-text-3)] mt-1">Direct API integration utilizing low-latency order endpoints to maximize execution speed.</p>
            </div>
            <div className="qd-card-tight p-4 bg-[var(--qd-surface)]/40 hover:bg-[var(--qd-surface-2)]/60">
              <div className="text-[var(--qd-accent)] mb-2"><TrendingUp size={20} /></div>
              <h3 className="text-white font-bold text-sm">Low Capital Strategies</h3>
              <p className="text-xs text-[var(--qd-text-3)] mt-1">Premium options strategies for NIFTY, SENSEX, Crude & Gas, optimized for INR 10,000–20,000 capital.</p>
            </div>
            <div className="qd-card-tight p-4 bg-[var(--qd-surface)]/40 hover:bg-[var(--qd-surface-2)]/60">
              <div className="text-emerald-400 mb-2"><BarChart2 size={20} /></div>
              <h3 className="text-white font-bold text-sm">Paper & Live Brokers</h3>
              <p className="text-xs text-[var(--qd-text-3)] mt-1">Deploy strategies dynamically. Choose between paper simulations or live accounts per strategy card.</p>
            </div>
            <div className="qd-card-tight p-4 bg-[var(--qd-surface)]/40 hover:bg-[var(--qd-surface-2)]/60">
              <div className="text-[var(--qd-cyan)] mb-2"><Smartphone size={20} /></div>
              <h3 className="text-white font-bold text-sm">Mobile Adaptive Engine</h3>
              <p className="text-xs text-[var(--qd-text-3)] mt-1">Stunning fully-responsive layouts designed to monitor, toggle, and manage live portfolios on phone browsers.</p>
            </div>
          </div>
        </div>

        {/* Footer info */}
        <div className="flex flex-wrap items-center gap-4 text-xs font-mono text-[var(--qd-text-3)] uppercase tracking-wider">
          <span>&copy; 2026 QUANTG GROUP</span>
          <span className="hidden sm:inline border-l border-[var(--qd-border)] h-3" />
          <span>SECURED BY AES-256 GCM</span>
        </div>
      </div>

      {/* Right Column: Sliding Auth Glass Panel */}
      <div className="flex items-center justify-center p-6 md:p-8 relative z-10 lg:border-l lg:border-[var(--qd-border)] bg-[var(--qd-bg-2)]/60 backdrop-blur-xl">
        <div className="w-full max-w-md space-y-6">
          
          {/* Card Wrapper */}
          <div className="qd-card p-6 md:p-8 border border-[var(--qd-border)] shadow-2xl relative bg-[var(--qd-surface)]/90 backdrop-blur-xl">
            {/* Sliding Form Header Tabs */}
            <div className="flex border-b border-[var(--qd-border)] mb-6 text-sm font-mono tracking-wider">
              <button
                type="button"
                onClick={() => { setIsLogin(true); setErr(""); }}
                className={`flex-1 pb-3 text-center transition-all ${isLogin ? "qd-tab-active font-bold text-white" : "text-[var(--qd-text-3)] hover:text-[var(--qd-text)]"}`}
              >
                // GATEWAY_LOGIN
              </button>
              <button
                type="button"
                onClick={() => { setIsLogin(false); setErr(""); }}
                className={`flex-1 pb-3 text-center transition-all ${!isLogin ? "qd-tab-active font-bold text-white" : "text-[var(--qd-text-3)] hover:text-[var(--qd-text)]"}`}
              >
                // REGISTER_TRADER
              </button>
            </div>

            <div className="space-y-4">
              <div className="space-y-1">
                <h2 className="text-xl font-head font-bold text-white">
                  {isLogin ? "Access Terminal" : "Request Registry"}
                </h2>
                <p className="text-xs text-[var(--qd-text-3)]">
                  {isLogin ? "Provide secure keys to enter the terminal." : "Trader registry entries undergo Owner review prior to seeding."}
                </p>
              </div>

              {/* Form Block */}
              <form onSubmit={submit} className="space-y-4" data-testid="auth-form">
                {!isLogin && (
                  <Field
                    icon={<User size={16} />}
                    placeholder="Trader's Full Name"
                    value={form.name}
                    onChange={(v) => setForm({ ...form, name: v })}
                    testid="input-name"
                  />
                )}
                <Field
                  icon={<Mail size={16} />}
                  placeholder="Registry Email (trader@market.com)"
                  value={form.email}
                  onChange={(v) => setForm({ ...form, email: v })}
                  testid="input-email"
                  type="email"
                />
                <Field
                  icon={<Lock size={16} />}
                  placeholder="Security Password"
                  value={form.password}
                  onChange={(v) => setForm({ ...form, password: v })}
                  testid="input-password"
                  type="password"
                />

                {err && (
                  <div className="text-xs font-mono text-[var(--qd-loss)] py-2.5 px-3 bg-[var(--qd-loss)]/10 border border-[var(--qd-loss)]/30 rounded flex items-start gap-2" data-testid="auth-error">
                    <AlertCircle size={14} className="mt-0.5 flex-shrink-0" />
                    <span className="leading-normal">{err}</span>
                  </div>
                )}

                <button
                  type="submit"
                  disabled={busy}
                  className="w-full bg-gradient-to-r from-[var(--qd-accent)] to-[var(--qd-cyan)] hover:opacity-90 disabled:opacity-50 text-white py-3 px-4 font-mono text-sm uppercase tracking-wider font-bold transition-all duration-200 rounded-md active:scale-95 shadow-md"
                  data-testid="auth-submit"
                >
                  {busy ? "Authorizing..." : isLogin ? "Launch Trading Desk ->" : "Request Registry Gate ->"}
                </button>
              </form>
            </div>
          </div>

          {/* Quick Info Box below auth container */}
          <div className="qd-card-tight p-4 bg-[var(--qd-surface)] border border-[var(--qd-border)] flex gap-3 text-xs text-[var(--qd-text-3)] leading-relaxed">
            <Cpu size={24} className="text-[var(--qd-cyan)] flex-shrink-0 mt-0.5 animate-pulse" />
            <div className="font-mono">
              <span className="text-white font-semibold block">Security Notice:</span>
              QuantG utilizes bank-level transport security and cryptographically masks API keys in MongoDB.
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}

const Field = ({ icon, placeholder, value, onChange, type = "text", testid }) => (
  <div className="flex items-center bg-[var(--qd-surface-2)] border border-[var(--qd-border)] focus-within:border-[var(--qd-cyan)] focus-within:ring-1 focus-within:ring-[var(--qd-cyan)]/25 rounded-md min-h-[44px] transition-all">
    <span className="px-3 text-[var(--qd-text-3)]">{icon}</span>
    <input
      data-testid={testid}
      type={type}
      placeholder={placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="flex-1 bg-transparent py-2.5 pr-3 text-sm text-white placeholder:text-[var(--qd-text-3)] outline-none font-mono"
      required
    />
  </div>
);
