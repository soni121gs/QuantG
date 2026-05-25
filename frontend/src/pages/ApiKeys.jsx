import React, { useEffect, useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { api, formatApiErrorDetail } from "../lib/api";
import { KeyRound, Trash2, Save, ShieldCheck, AlertTriangle, ExternalLink, CheckCircle2, XCircle, Copy } from "lucide-react";
import { toast } from "sonner";

export default function ApiKeys() {
  const [keys, setKeys] = useState([]);
  const [form, setForm] = useState({
    broker: "zerodha",
    api_key: "",
    api_secret: "",
    user_id_at_broker: "",
    mobile_number: "",
    mpin: "",
    totp_secret_key: "",
    redirect_uri: "",
    is_sandbox: false,
  });
  const [upstoxRedirectUri, setUpstoxRedirectUri] = useState("");
  const [saving, setSaving] = useState(false);
  const [zStatus, setZStatus] = useState({ connected: false });
  const [kotakStatus, setKotakStatus] = useState({ connected: false });
  const [kotakOtp, setKotakOtp] = useState("");
  const [upstoxStatus, setUpstoxStatus] = useState({ connected: false });
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();

  const defaultUpstoxRedirect = () => `${window.location.origin}/api/broker/upstox/callback`;

  const load = () =>
    Promise.all([
      api.get("/broker/keys").then((r) => setKeys(r.data)),
      api.get("/zerodha/status").then((r) => setZStatus(r.data)).catch(() => {}),
      api.get("/kotak/status").then((r) => setKotakStatus(r.data)).catch(() => {}),
      api.get("/upstox/status").then((r) => setUpstoxStatus(r.data)).catch(() => {}),
      api.get("/broker/upstox/config").then((r) => {
        const uri = r.data.redirect_uri || defaultUpstoxRedirect();
        setUpstoxRedirectUri(uri);
        setForm((prev) => ({ ...prev, redirect_uri: uri }));
      }).catch(() => {
        const uri = defaultUpstoxRedirect();
        setUpstoxRedirectUri(uri);
        setForm((prev) => ({ ...prev, redirect_uri: uri }));
      }),
    ]);
  useEffect(() => { load(); }, []);

  // Handle Zerodha OAuth callback (?request_token=...&status=success)
  useEffect(() => {
    const rt = params.get("request_token");
    const st = params.get("status");
    if (rt && st === "success") {
      api.post("/zerodha/exchange", { request_token: rt })
        .then((r) => {
          toast.success(`Connected to Zerodha (${r.data.kite_user_id})`);
          // Clear query string
          navigate("/broker-keys", { replace: true });
          load();
        })
        .catch((e) => {
          toast.error(e.response?.data?.detail || "Token exchange failed");
          navigate("/broker-keys", { replace: true });
        });
    }
  }, [params, navigate]);

  useEffect(() => {
    if (params.get("upstox") === "connected") {
      toast.success("Upstox connected successfully");
      navigate("/broker-keys", { replace: true });
      load();
    }
  }, [params, navigate]);

  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const payload = { ...form };
      if (form.broker === "upstox") {
        payload.redirect_uri = form.redirect_uri || upstoxRedirectUri || defaultUpstoxRedirect();
      }
      await api.post("/broker/keys", payload);
      toast.success("Keys saved securely");
      setForm({ ...form, api_key: "", api_secret: "", mobile_number: "", mpin: "", totp_secret_key: "", is_sandbox: false });
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    } finally { setSaving(false); }
  };

  const del = async (id) => {
    await api.delete(`/broker/keys/${id}`);
    load();
  };

  const connect = async () => {
    try {
      const r = await api.get("/zerodha/login-url");
      window.location.href = r.data.url;
    } catch (e) {
      toast.error(e.response?.data?.detail || "Login URL unavailable");
    }
  };

  const disconnect = async () => {
    if (!window.confirm("Disconnect Zerodha session?")) return;
    await api.post("/zerodha/disconnect");
    toast.success("Disconnected");
    load();
  };

  const kotakLogin = async () => {
    try {
      await api.post("/kotak/login", { current_otp: kotakOtp.trim() || undefined });
      toast.success("Kotak Neo session connected");
      setKotakOtp("");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Kotak login failed");
    }
  };

  const kotakRepair = async () => {
    try {
      await api.post("/kotak/repair");
      toast.success("Kotak gateway reset. Enter fresh OTP and connect.");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Kotak repair failed");
    }
  };

  const kotakLogout = async () => {
    try {
      await api.post("/kotak/logout");
      toast.success("Kotak Neo session closed");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Kotak logout failed");
    }
  };

  const kotakOrderFeed = async () => {
    try {
      await api.post("/kotak/order-feed/start");
      toast.success("Kotak order feed started");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Kotak order feed failed");
    }
  };

  const connectUpstox = async () => {
    try {
      const r = await api.get("/broker/upstox/login");
      if (!r.data?.url) {
        toast.error("Upstox did not return a login URL. Check API key, secret, and redirect URI.");
        return;
      }
      window.location.href = r.data.url;
    } catch (e) {
      const detail = formatApiErrorDetail(e.response?.data?.detail) || "Upstox login URL unavailable";
      toast.error(detail);
    }
  };

  const redirectUrl = `${window.location.origin}/broker-keys?status=success`;
  const upstoxRedirectUrl = upstoxRedirectUri || defaultUpstoxRedirect();

  return (
    <div className="space-y-4 max-w-4xl" data-testid="api-keys-page">
      <div>
        <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// CREDENTIALS</div>
        <h1 className="font-head text-3xl font-bold text-white mt-1">Broker Keys</h1>
        <p className="text-sm text-[var(--qd-text-2)] mt-1">Connect broker APIs for real market data, order sync, and controlled live execution.</p>
      </div>

      {/* Step 1: save keys */}
      <form onSubmit={save} className="qd-card p-5 space-y-3" data-testid="keys-form">
        <h2 className="font-head text-lg text-white flex items-center gap-2"><KeyRound size={16} /> Step 1 · Save broker API credentials</h2>
        <div>
          <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">Broker</label>
          <select
            value={form.broker}
            onChange={(e) => setForm({ ...form, broker: e.target.value })}
            className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2 text-sm text-white font-mono rounded-sm"
            data-testid="broker-select"
          >
            <option value="zerodha">Zerodha Kite</option>
            <option value="kotak_neo">Kotak Neo</option>
            <option value="upstox">Upstox</option>
          </select>
        </div>
        <Input label={form.broker === "kotak_neo" ? "Consumer Key" : form.broker === "upstox" ? "API Key / Client ID" : "API Key"} value={form.api_key} onChange={(v) => setForm({ ...form, api_key: v })} testid="api-key-input" />
        <Input label={form.broker === "kotak_neo" ? "Consumer Secret" : form.broker === "upstox" ? "API Secret" : "API Secret"} value={form.api_secret} onChange={(v) => setForm({ ...form, api_secret: v })} type="password" testid="api-secret-input" />
        <Input label={form.broker === "kotak_neo" ? "UCC / Client ID" : "Client ID (optional)"} value={form.user_id_at_broker} onChange={(v) => setForm({ ...form, user_id_at_broker: v })} testid="input-client-id" />
        {form.broker === "kotak_neo" && (
          <div className="grid md:grid-cols-3 gap-3">
            <Input label="Mobile Number" value={form.mobile_number} onChange={(v) => setForm({ ...form, mobile_number: v })} testid="input-kotak-mobile" />
            <Input label="MPIN / Password" value={form.mpin} onChange={(v) => setForm({ ...form, mpin: v })} type="password" testid="input-kotak-mpin" />
            <Input label="TOTP Setup Secret" value={form.totp_secret_key} onChange={(v) => setForm({ ...form, totp_secret_key: v })} type="password" testid="input-kotak-totp" />
          </div>
        )}
        {form.broker === "upstox" && (
          <div className="flex items-center gap-2 py-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] p-3 rounded-sm">
            <input
              type="checkbox"
              id="is_sandbox"
              checked={form.is_sandbox || false}
              onChange={(e) => setForm({ ...form, is_sandbox: e.target.checked })}
              className="accent-[var(--qd-cyan)] w-4 h-4 cursor-pointer"
            />
            <label htmlFor="is_sandbox" className="text-sm font-mono text-white cursor-pointer select-none">
              Use Upstox Sandbox Mode (Mock Trading)
            </label>
          </div>
        )}
        <button disabled={saving} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white px-4 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2" data-testid="save-broker-keys-btn">
          <Save size={14} /> Save Keys
        </button>
      </form>

      {/* Step 2: Configure redirect URL */}
      <div className="qd-card p-5" data-testid="redirect-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3">
          <AlertTriangle size={16} className="text-[var(--qd-warn)]" /> Step 2 · Set this Redirect URL in your Kite app
        </h2>
        <p className="text-sm text-[var(--qd-text-2)] mb-2">
          Go to <a className="text-[var(--qd-accent)] hover:underline" target="_blank" rel="noreferrer" href="https://developers.kite.trade/apps">developers.kite.trade/apps</a>,
          open your app, and paste this in the <span className="font-mono text-white">Redirect URL</span> field:
        </p>
        <div className="flex items-center gap-2 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 rounded-sm">
          <code className="font-mono text-xs text-[var(--qd-profit)] flex-1 break-all">{redirectUrl}</code>
          <button onClick={() => { navigator.clipboard.writeText(redirectUrl); toast.success("Copied"); }} className="text-[var(--qd-text-2)] hover:text-white" data-testid="copy-redirect"><Copy size={14} /></button>
        </div>
      </div>

      {/* Step 3: OAuth Connect */}
      <div className="qd-card p-5" data-testid="connect-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><ShieldCheck size={16} /> Step 3 · Connect Zerodha (daily)</h2>
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm">
            {zStatus.connected ? (
              <div className="text-[var(--qd-profit)] flex items-center gap-2"><CheckCircle2 size={14} /> Connected as <span className="font-mono">{zStatus.kite_user_id}</span></div>
            ) : zStatus.reason === "expired" ? (
              <div className="text-[var(--qd-warn)] flex items-center gap-2"><AlertTriangle size={14} /> Session expired — re-connect to continue trading.</div>
            ) : (
              <div className="text-[var(--qd-text-2)] flex items-center gap-2"><XCircle size={14} /> Not connected. Click Connect to start the daily Kite login.</div>
            )}
            {zStatus.expires_at && (
              <div className="text-xs font-mono text-[var(--qd-text-3)] mt-1">Token expires: {new Date(zStatus.expires_at).toLocaleString("en-IN")}</div>
            )}
          </div>
          <div className="flex gap-2">
            {zStatus.connected && (
              <button onClick={disconnect} className="border border-[var(--qd-border)] hover:border-[var(--qd-loss)] text-[var(--qd-loss)] px-3 py-2 text-xs font-mono uppercase rounded-sm" data-testid="zerodha-disconnect">Disconnect</button>
            )}
            <button onClick={connect} className="bg-[var(--qd-profit)] text-black hover:opacity-85 px-5 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2" data-testid="zerodha-connect">
              <ExternalLink size={14} /> {zStatus.connected ? "Re-connect" : "Connect to Zerodha"}
            </button>
          </div>
        </div>
        <div className="mt-3 text-xs font-mono text-[var(--qd-text-3)]">
          Note: Zerodha tokens expire daily at 6 AM IST. You'll need to re-connect each trading morning.
        </div>
      </div>

      <div className="qd-card p-5" data-testid="kotak-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><ShieldCheck size={16} /> Kotak Neo</h2>
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm">
            {kotakStatus.connected ? (
              <div className="text-[var(--qd-profit)] flex items-center gap-2"><CheckCircle2 size={14} /> Kotak Neo session connected</div>
            ) : kotakStatus.keys_saved ? (
              <div className="text-[var(--qd-profit)] flex items-center gap-2"><CheckCircle2 size={14} /> Credentials saved for <span className="font-mono">{kotakStatus.client_id || "Kotak Neo"}</span></div>
            ) : (
              <div className="text-[var(--qd-text-2)] flex items-center gap-2"><XCircle size={14} /> Not configured. Save Kotak Neo credentials above to prepare the broker connection.</div>
            )}
            <div className="text-xs font-mono text-[var(--qd-text-3)] mt-1">
              SDK: {kotakStatus.sdk_available ? "available" : "not installed"} · Credentials: {kotakStatus.env_ready ? "ready" : `missing ${(kotakStatus.missing_env || []).join(", ") || "TOTP/MPIN/mobile"}`} · {kotakStatus.reason || "ready"}
            </div>
          </div>
          <div className="flex gap-2">
            {kotakStatus.connected && (
              <>
                <button onClick={kotakOrderFeed} className="border border-[var(--qd-border)] hover:border-[var(--qd-profit)] text-white px-3 py-2 text-xs font-mono uppercase rounded-sm">Order Feed</button>
                <button onClick={kotakLogout} className="border border-[var(--qd-border)] hover:border-[var(--qd-loss)] text-[var(--qd-loss)] px-3 py-2 text-xs font-mono uppercase rounded-sm">Disconnect</button>
              </>
            )}
            <input value={kotakOtp} onChange={(e) => setKotakOtp(e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="Current OTP" className="w-28 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-xs text-white font-mono rounded-sm" data-testid="kotak-current-otp" />
            <button onClick={kotakRepair} className="border border-[var(--qd-border)] hover:border-[var(--qd-accent)] text-white px-3 py-2 text-xs font-mono uppercase rounded-sm">
              Repair
            </button>
            <button onClick={kotakLogin} disabled={!kotakStatus.keys_saved || !kotakStatus.sdk_available || !kotakStatus.env_ready} className="bg-[var(--qd-profit)] disabled:opacity-40 text-black hover:opacity-85 px-5 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2">
              <ExternalLink size={14} /> Connect Kotak
            </button>
          </div>
        </div>
      </div>

      <div className="qd-card p-5 border-l-4 border-l-[var(--qd-cyan)] cyber-glow-cyan" data-testid="upstox-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3">
          <ShieldCheck size={16} className="text-[var(--qd-cyan)]" /> 
          Upstox 
          <span className="text-[9px] font-mono uppercase bg-gradient-to-r from-[var(--qd-cyan)] to-[var(--qd-accent)] text-[#030407] px-2 py-0.5 rounded font-extrabold tracking-wider">Primary</span>
          {upstoxStatus.is_sandbox && (
            <span className="text-[9px] font-mono uppercase bg-amber-500 text-black px-2 py-0.5 rounded font-extrabold tracking-wider ml-1">Sandbox (Mock)</span>
          )}
        </h2>
        <div className="text-sm">
          {upstoxStatus.keys_saved ? (
            <div className="text-[var(--qd-profit)] flex items-center gap-2"><CheckCircle2 size={14} /> Credentials saved for <span className="font-mono">{upstoxStatus.client_id || "Upstox"}</span></div>
          ) : (
            <div className="text-[var(--qd-text-2)] flex items-center gap-2"><XCircle size={14} /> Not configured. Save Upstox credentials above to prepare the broker connection.</div>
          )}
          <div className="text-xs font-mono text-[var(--qd-text-3)] mt-1">
            SDK: {upstoxStatus.sdk_available ? "available" : "not installed"} · Environment: <span className={upstoxStatus.is_sandbox ? "text-amber-400 font-bold" : "text-[var(--qd-cyan)] font-bold"}>{upstoxStatus.is_sandbox ? "SANDBOX (Mock Trading)" : "PRODUCTION (Live Market)"}</span> · OAuth, market data, and live routing remain disabled until verified.
          </div>
        </div>
      </div>

      <div className="qd-card p-5 border-l-4 border-l-[var(--qd-cyan)] cyber-glow-cyan" data-testid="upstox-connect-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><ShieldCheck size={16} className="text-[var(--qd-cyan)]" /> Connect Upstox OAuth</h2>
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm">
            <div className={upstoxStatus.connected ? "text-[var(--qd-profit)]" : "text-[var(--qd-text-2)]"}>
              {upstoxStatus.connected ? "Upstox connected — live data and orders enabled" : upstoxStatus.keys_saved ? "Upstox keys saved. Click Connect Upstox." : "Save Upstox API Key and Secret first."}
            </div>
            <div className="text-xs text-[var(--qd-text-3)] mt-2">
              Paste this Redirect URL in Upstox Developer Portal (exact match):
            </div>
            <div className="text-xs font-mono text-[var(--qd-profit)] break-all mt-1">{upstoxRedirectUrl}</div>
          </div>
          <div className="flex gap-2">
            <button onClick={() => { navigator.clipboard.writeText(upstoxRedirectUrl); toast.success("Copied"); }} className="border border-[var(--qd-border)] hover:border-[var(--qd-accent)] text-white px-3 py-2 text-xs font-mono uppercase rounded-sm">
              Copy URL
            </button>
            <button onClick={connectUpstox} disabled={!upstoxStatus.keys_saved} className="bg-[var(--qd-profit)] disabled:opacity-40 text-black hover:opacity-85 px-5 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2">
              <ExternalLink size={14} /> {upstoxStatus.connected ? "Re-connect Upstox" : "Connect Upstox"}
            </button>
          </div>
        </div>
      </div>

      {/* Saved keys list */}
      <div className="qd-card" data-testid="keys-list">
        <div className="border-b border-[var(--qd-border)] px-4 py-3"><h2 className="font-head text-base text-white">Linked Brokers</h2></div>
        {keys.length === 0 ? (
          <div className="p-8 text-center font-mono text-sm text-[var(--qd-text-2)]">No keys linked.</div>
        ) : (
          <div className="qd-table-wrap"><table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">Broker</th><th className="px-4 py-2">API Key</th><th className="px-4 py-2">Client</th><th className="px-4 py-2">Linked</th><th className="px-4 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {keys.map((k) => (
                <tr key={k.id} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                  <td className="px-4 py-2.5 text-white flex items-center gap-2"><ShieldCheck size={14} className="text-[var(--qd-profit)]" />{k.broker.toUpperCase()}</td>
                  <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{k.api_key_masked}</td>
                  <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{k.user_id_at_broker || "—"}</td>
                  <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{new Date(k.created_at).toLocaleDateString()}</td>
                  <td className="px-4 py-2.5 text-right">
                    <button onClick={() => del(k.id)} className="text-[var(--qd-loss)] hover:opacity-80" data-testid={`delete-key-${k.id}`}><Trash2 size={14} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
    </div>
  );
}

const Input = ({ label, value, onChange, type = "text", testid }) => (
  <div>
    <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</label>
    <input
      data-testid={testid}
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2 text-sm text-white font-mono rounded-sm"
    />
  </div>
);
