import React, { useEffect, useState } from "react";
import { api } from "../lib/api";
import { User, Shield, Settings2, Lock, Activity, AlertTriangle, CheckCircle2, XCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

export default function Profile() {
  const [p, setP] = useState(null);
  const [form, setForm] = useState({});
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  const [busy, setBusy] = useState(false);

  const load = () =>
    api.get("/profile").then((r) => {
      setP(r.data);
      setForm({
        name: r.data.name || "",
        default_qty: r.data.default_qty,
        default_product: r.data.default_product,
        max_daily_loss: r.data.max_daily_loss,
        max_position_size: r.data.max_position_size,
        paper_mode: r.data.paper_mode,
      });
    });
  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true);
    try {
      await api.put("/profile", form);
      toast.success("Profile updated");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Update failed");
    } finally { setBusy(false); }
  };

  const togglePaper = async () => {
    const next = !form.paper_mode;
    setForm({ ...form, paper_mode: next });
    try {
      await api.put("/profile", { paper_mode: next });
      toast.success(next ? "Switched to PAPER mode (safe)" : "⚠ Switched to LIVE mode — real orders");
      load();
    } catch (e) { toast.error(e.response?.data?.detail || "Failed"); }
  };

  const changePassword = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post("/profile/change-password", pw);
      toast.success("Password changed");
      setPw({ current_password: "", new_password: "" });
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed");
    } finally { setBusy(false); }
  };

  if (!p) return <div className="font-mono text-sm text-[var(--qd-text-2)]">Loading...</div>;

  const z = p.zerodha || {};
  return (
    <div className="space-y-4 max-w-5xl" data-testid="profile-page">
      <div>
        <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// ACCOUNT</div>
        <h1 className="font-head text-3xl font-bold text-white mt-1">Profile</h1>
      </div>

      {/* Master switch */}
      <div className="qd-card p-5 flex flex-col md:flex-row md:items-center justify-between gap-3" data-testid="paper-live-switch">
        <div className="flex items-start gap-3">
          <AlertTriangle size={22} className={form.paper_mode ? "text-[var(--qd-warn)]" : "text-[var(--qd-loss)]"} />
          <div>
            <div className="font-head text-lg text-white">Trading Mode</div>
            <div className="text-sm text-[var(--qd-text-2)]">
              {form.paper_mode
                ? "PAPER — orders are simulated locally. Safe to test strategies."
                : "LIVE — orders go to your Zerodha account. Real money at risk."}
            </div>
          </div>
        </div>
        <button
          onClick={togglePaper}
          className={`px-5 py-2.5 font-mono text-sm uppercase tracking-wider rounded-sm ${
            form.paper_mode ? "border border-[var(--qd-border)] text-white hover:border-white" : "qd-btn-sell"
          }`}
          data-testid="toggle-mode-btn"
        >
          {form.paper_mode ? "Go LIVE →" : "← Back to Paper"}
        </button>
      </div>

      {/* Zerodha connection */}
      <div className="qd-card p-5" data-testid="zerodha-status-card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-head text-lg text-white flex items-center gap-2"><Shield size={16} /> Zerodha Session</h2>
          <button onClick={load} className="text-xs font-mono text-[var(--qd-text-2)] hover:text-white flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <Stat label="Status" value={
            z.connected ? <span className="text-[var(--qd-profit)] flex items-center gap-1"><CheckCircle2 size={14} /> Connected</span>
            : z.reason === "expired" ? <span className="text-[var(--qd-warn)] flex items-center gap-1"><AlertTriangle size={14} /> Expired</span>
            : <span className="text-[var(--qd-text-3)] flex items-center gap-1"><XCircle size={14} /> Not connected</span>
          } />
          <Stat label="Kite User" value={z.kite_user_id || "—"} mono />
          <Stat label="Token Expires" value={z.expires_at ? new Date(z.expires_at).toLocaleString("en-IN") : "—"} mono />
          <Stat label="Reason" value={z.reason || "—"} mono />
        </div>
        {z.reason === "expired" && (
          <div className="mt-3 text-xs font-mono text-[var(--qd-warn)]">
            Kite tokens expire at 6 AM IST. Re-connect on the Broker Keys page.
          </div>
        )}
      </div>

      {/* Basic info */}
      <div className="qd-card p-5" data-testid="basic-info-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><User size={16} /> Basic Info</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Input label="Name" value={form.name} onChange={(v) => setForm({ ...form, name: v })} testid="input-name" />
          <Input label="Email" value={p.email} disabled />
        </div>
      </div>

      {/* Trading preferences */}
      <div className="qd-card p-5" data-testid="prefs-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><Settings2 size={16} /> Trading Preferences</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Input label="Default Qty" type="number" value={form.default_qty} onChange={(v) => setForm({ ...form, default_qty: +v })} testid="input-qty" />
          <SelectIn label="Default Product" value={form.default_product} onChange={(v) => setForm({ ...form, default_product: v })} options={["MIS", "CNC", "NRML"]} testid="input-product" />
          <Input label="Max Daily Loss (₹)" type="number" value={form.max_daily_loss} onChange={(v) => setForm({ ...form, max_daily_loss: +v })} testid="input-max-loss" />
          <Input label="Max Position Size (₹)" type="number" value={form.max_position_size} onChange={(v) => setForm({ ...form, max_position_size: +v })} testid="input-max-size" />
        </div>
        <button onClick={save} disabled={busy} className="mt-4 bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white px-4 py-2 font-mono text-xs uppercase tracking-wider rounded-sm" data-testid="save-profile-btn">
          Save Preferences
        </button>
      </div>

      {/* Change password */}
      <form onSubmit={changePassword} className="qd-card p-5 space-y-3" data-testid="password-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2"><Lock size={16} /> Change Password</h2>
        <Input label="Current Password" type="password" value={pw.current_password} onChange={(v) => setPw({ ...pw, current_password: v })} testid="input-current-pw" />
        <Input label="New Password" type="password" value={pw.new_password} onChange={(v) => setPw({ ...pw, new_password: v })} testid="input-new-pw" />
        <button type="submit" disabled={busy || !pw.current_password || !pw.new_password} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white px-4 py-2 font-mono text-xs uppercase tracking-wider rounded-sm" data-testid="change-pw-btn">
          Change Password
        </button>
      </form>

      {/* Session info */}
      <div className="qd-card p-5" data-testid="session-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><Activity size={16} /> Session</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
          <Stat label="User ID" value={p.id.slice(0, 8) + "…"} mono />
          <Stat label="Created" value={new Date(p.created_at).toLocaleDateString()} mono />
          <Stat label="Mode" value={form.paper_mode ? "PAPER" : "LIVE"} mono tone={form.paper_mode ? "warn" : "loss"} />
        </div>
      </div>
    </div>
  );
}

const Input = ({ label, value, onChange, type = "text", testid, disabled }) => (
  <div>
    <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</label>
    <input
      data-testid={testid}
      type={type}
      value={value ?? ""}
      onChange={(e) => onChange && onChange(e.target.value)}
      disabled={disabled}
      className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2 text-sm text-white font-mono rounded-sm disabled:opacity-50"
    />
  </div>
);

const SelectIn = ({ label, value, onChange, options, testid }) => (
  <div>
    <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</label>
    <select data-testid={testid} value={value} onChange={(e) => onChange(e.target.value)} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-white font-mono rounded-sm">
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  </div>
);

const Stat = ({ label, value, mono, tone }) => (
  <div>
    <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className={`mt-1 text-sm ${mono ? "font-mono" : ""} ${
      tone === "warn" ? "text-[var(--qd-warn)]" : tone === "loss" ? "text-[var(--qd-loss)]" : "text-white"
    }`}>{value}</div>
  </div>
);
