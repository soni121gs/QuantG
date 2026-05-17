import React, { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";
import { formatApiErrorDetail } from "../lib/api";
import { Activity, Mail, Lock, User, AlertCircle } from "lucide-react";

export default function Auth({ mode = "login" }) {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: "", password: "", name: "" });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

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
      if (mode === "login") {
        await login(form.email.toLowerCase().trim(), form.password);
      } else {
        if (!form.name || form.name.trim().length === 0) {
          setErr("Name is required");
          setBusy(false);
          return;
        }
        await register(form.email.toLowerCase().trim(), form.password, form.name.trim());
      }
      navigate("/dashboard");
    } catch (e) {
      const errMsg = e.response?.data?.detail || e.message || "Authentication failed";
      setErr(formatApiErrorDetail(errMsg));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen grid lg:grid-cols-2 bg-[var(--qd-bg)]">
      {/* Left: form */}
      <div className="flex flex-col justify-between p-8 lg:p-12 max-w-xl">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 bg-[var(--qd-accent)] flex items-center justify-center">
            <Activity size={16} className="text-white" strokeWidth={2.5} />
          </div>
          <span className="font-head font-bold tracking-tight text-white text-lg">
            QUANT<span className="text-[var(--qd-accent)]">G</span>
          </span>
        </div>

        <div className="my-auto">
          <div className="font-mono text-xs text-[var(--qd-text-3)] uppercase tracking-widest mb-3">
            {mode === "login" ? "// SECURE LOGIN" : "// CREATE ACCOUNT"}
          </div>
          <h1 className="font-head text-4xl lg:text-5xl font-bold tracking-tight text-white mb-3">
            {mode === "login" ? "Welcome back, trader." : "Build your edge."}
          </h1>
          <p className="text-sm text-[var(--qd-text-2)] mb-8 max-w-md">
            Run algo strategies, integrate your Zerodha account, and let our AI bot find opportunities while you sleep.
          </p>

          <form onSubmit={submit} className="space-y-3 max-w-md" data-testid="auth-form">
            {mode === "register" && (
              <Field
                icon={<User size={14} />}
                placeholder="Your name"
                value={form.name}
                onChange={(v) => setForm({ ...form, name: v })}
                testid="input-name"
              />
            )}
            <Field
              icon={<Mail size={14} />}
              placeholder="trader@market.com"
              value={form.email}
              onChange={(v) => setForm({ ...form, email: v })}
              testid="input-email"
              type="email"
            />
            <Field
              icon={<Lock size={14} />}
              placeholder="Password"
              value={form.password}
              onChange={(v) => setForm({ ...form, password: v })}
              testid="input-password"
              type="password"
            />
            {err && (
              <div className="text-xs font-mono text-[var(--qd-loss)] py-2 px-3 bg-[var(--qd-loss)]/10 border border-[var(--qd-loss)]/30 rounded-sm flex items-start gap-2" data-testid="auth-error">
                <AlertCircle size={14} className="mt-0.5 flex-shrink-0" />
                <span>{err}</span>
              </div>
            )}
            <button
              type="submit"
              disabled={busy}
              className="w-full bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white py-2.5 px-4 font-mono text-sm uppercase tracking-wider transition-colors rounded-sm"
              data-testid="auth-submit"
            >
              {busy ? "..." : mode === "login" ? "Enter Terminal ->" : "Create Account ->"}
            </button>
          </form>

          <div className="mt-6 text-xs text-[var(--qd-text-2)] font-mono">
            {mode === "login" ? (
              <>
                No account?{" "}
                <Link to="/signup" className="text-[var(--qd-accent)] hover:underline" data-testid="link-signup">
                  Sign up
                </Link>
              </>
            ) : (
              <>
                Already in?{" "}
                <Link to="/login" className="text-[var(--qd-accent)] hover:underline" data-testid="link-login">
                  Login
                </Link>
              </>
            )}
          </div>
        </div>

        <div className="text-[10px] font-mono text-[var(--qd-text-3)] uppercase tracking-widest">
          Quantdesk - Paper Trading Mode
        </div>
      </div>

      {/* Right: hero */}
      <div
        className="hidden lg:block relative bg-cover bg-center"
        style={{
          backgroundImage:
            "url(https://static.prod-images.emergentagent.com/jobs/6456a41d-90f7-40ab-b7a0-c10007c6cd13/images/66c3accb1236f6dde0453585ba36e9c0d7ab37b509457d605103056a618cabd8.png)",
        }}
      >
        <div className="absolute inset-0 bg-gradient-to-r from-[var(--qd-bg)] via-transparent to-transparent" />
        <div className="absolute bottom-8 right-8 left-8 grid grid-cols-2 gap-3 max-w-md ml-auto">
          <Stat label="UPTIME" value="99.99%" />
          <Stat label="STRATEGIES" value="2,341" />
          <Stat label="AVG LATENCY" value="42ms" />
          <Stat label="WIN RATE" value="68.4%" tone="profit" />
        </div>
      </div>
    </div>
  );
}

const Field = ({ icon, placeholder, value, onChange, type = "text", testid }) => (
  <div className="flex items-center bg-[var(--qd-surface)] border border-[var(--qd-border)] focus-within:border-[var(--qd-accent)] rounded-sm">
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

const Stat = ({ label, value, tone }) => (
  <div className="qd-card p-3 backdrop-blur bg-[#050505]/80">
    <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className={`font-mono text-xl font-bold mt-1 ${tone === "profit" ? "text-[var(--qd-profit)]" : "text-white"}`}>
      {value}
    </div>
  </div>
);
