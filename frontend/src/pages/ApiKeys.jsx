import React, { useCallback, useEffect, useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { api, formatApiErrorDetail } from "../lib/api";
import { Activity, Database, KeyRound, Trash2, Save, ShieldCheck, ExternalLink, CheckCircle2, XCircle, Copy, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";

export default function ApiKeys() {
  const [keys, setKeys] = useState([]);
  const [form, setForm] = useState({
    broker: "upstox",
    api_key: "",
    api_secret: "",
    user_id_at_broker: "",
    redirect_uri: "",
    is_sandbox: false,
  });
  const [upstoxRedirectUri, setUpstoxRedirectUri] = useState("");
  const [saving, setSaving] = useState(false);
  const [upstoxStatus, setUpstoxStatus] = useState({ connected: false });
  const [dataHealth, setDataHealth] = useState(null);
  const [params] = useSearchParams();
  const navigate = useNavigate();

  const defaultUpstoxRedirect = () => `${window.location.origin}/api/broker/upstox/callback`;

  const load = useCallback(() =>
    Promise.all([
      api.get("/broker/keys").then((r) => setKeys(r.data)),
      api.get("/upstox/status").then((r) => setUpstoxStatus(r.data)).catch(() => {}),
      api.get("/upstox/data-health").then((r) => setDataHealth(r.data)).catch(() => {}),
      api.get("/broker/upstox/config").then((r) => {
        const uri = r.data.redirect_uri || defaultUpstoxRedirect();
        setUpstoxRedirectUri(uri);
        setForm((prev) => ({ ...prev, redirect_uri: uri }));
      }).catch(() => {
        const uri = defaultUpstoxRedirect();
        setUpstoxRedirectUri(uri);
        setForm((prev) => ({ ...prev, redirect_uri: uri }));
      }),
    ]), []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (params.get("upstox") === "connected") {
      toast.success("Upstox connected successfully");
      navigate("/broker-keys", { replace: true });
      load();
    }
  }, [params, navigate, load]);

  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const payload = {
        ...form,
        broker: "upstox",
        redirect_uri: form.redirect_uri || upstoxRedirectUri || defaultUpstoxRedirect(),
      };
      await api.post("/broker/keys", payload);
      toast.success("Upstox keys saved securely");
      setForm({ ...form, api_key: "", api_secret: "", is_sandbox: false });
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const del = async (id) => {
    await api.delete(`/broker/keys/${id}`);
    load();
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

  const upstoxRedirectUrl = upstoxRedirectUri || defaultUpstoxRedirect();

  return (
    <div className="space-y-4 max-w-4xl" data-testid="api-keys-page">
      <PageHeader
        eyebrow="Upstox Credentials"
        title="Broker Keys"
        subtitle="Connect Upstox for V3 market data, order sync, and backend-gated execution."
        badge={<StatusBadge tone={upstoxStatus.connected ? "healthy" : "warning"}>{upstoxStatus.connected ? "Connected" : "Setup Required"}</StatusBadge>}
      />

      <form onSubmit={save} className="qd-card p-5 space-y-3" data-testid="keys-form">
        <h2 className="font-head text-lg text-white flex items-center gap-2"><KeyRound size={16} /> Save Upstox API credentials</h2>
        <Input label="API Key / Client ID" value={form.api_key} onChange={(v) => setForm({ ...form, api_key: v })} testid="api-key-input" />
        <Input label="API Secret" value={form.api_secret} onChange={(v) => setForm({ ...form, api_secret: v })} type="password" testid="api-secret-input" />
        <Input label="Client ID (optional)" value={form.user_id_at_broker} onChange={(v) => setForm({ ...form, user_id_at_broker: v })} testid="input-client-id" />
        <Input label="Redirect URI" value={form.redirect_uri} onChange={(v) => setForm({ ...form, redirect_uri: v })} testid="input-redirect-uri" />
        <label className="flex items-center gap-2 py-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] p-3 rounded-sm cursor-pointer">
          <input
            type="checkbox"
            checked={form.is_sandbox || false}
            onChange={(e) => setForm({ ...form, is_sandbox: e.target.checked })}
            className="accent-[var(--qd-cyan)] w-4 h-4 cursor-pointer"
          />
          <span className="text-sm font-mono text-[var(--qd-text)] select-none">Use Upstox Sandbox Mode (Mock Trading)</span>
        </label>
        <button disabled={saving} className="qd-force-white bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 px-4 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2" data-testid="save-broker-keys-btn">
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
          {saving ? "Saving..." : "Save Keys"}
        </button>
      </form>

      <div className="qd-card p-5 border-l-4 border-l-[var(--qd-cyan)] cyber-glow-cyan" data-testid="upstox-connect-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3">
          <ShieldCheck size={16} className="text-[var(--qd-cyan)]" /> Connect Upstox OAuth
        </h2>
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="text-sm min-w-0">
            <div className={upstoxStatus.connected ? "text-[var(--qd-profit)]" : "text-[var(--qd-text-2)]"}>
              {upstoxStatus.connected ? "Upstox connected - live data and orders enabled" : upstoxStatus.keys_saved ? "Reconnect Upstox required" : "Save Upstox API Key and Secret first."}
            </div>
            <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs font-mono text-[var(--qd-text-3)]">
              <div>Login: <span className={upstoxStatus.logged_in ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"}>{upstoxStatus.logged_in ? "logged in" : "not logged in"}</span></div>
              <div>Token: <span className={upstoxStatus.token_valid ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"}>{upstoxStatus.token_state || (upstoxStatus.token_present ? "present" : "missing")}</span></div>
              <div>Last auth: <span className="text-[var(--qd-text)]">{upstoxStatus.last_auth_time ? new Date(upstoxStatus.last_auth_time).toLocaleString() : "-"}</span></div>
              <div>Feed: <span className={upstoxStatus.feed_running ? "text-[var(--qd-profit)]" : "text-[var(--qd-warn)]"}>{upstoxStatus.feed_running ? "running" : "stopped"}</span></div>
            </div>
            {upstoxStatus.reconnect_required && (
              <div className="mt-2 text-xs font-mono text-[var(--qd-warn)]">Live trading disabled until Upstox OAuth is reconnected.</div>
            )}
            <div className="text-xs text-[var(--qd-text-3)] mt-2">Paste this Redirect URL in Upstox Developer Portal (exact match):</div>
            <div className="text-xs font-mono text-[var(--qd-profit)] break-all mt-1">{upstoxRedirectUrl}</div>
            <div className="text-xs font-mono text-[var(--qd-text-3)] mt-2">
              SDK: {upstoxStatus.sdk_available ? "available" : "not installed"} / Environment:{" "}
              <span className={upstoxStatus.is_sandbox ? "text-amber-400 font-bold" : "text-[var(--qd-cyan)] font-bold"}>
                {upstoxStatus.is_sandbox ? "SANDBOX" : "PRODUCTION"}
              </span>
            </div>
          </div>
          <div className="flex gap-2">
            <button onClick={() => { navigator.clipboard.writeText(upstoxRedirectUrl); toast.success("Copied"); }} className="border border-[var(--qd-border)] hover:border-[var(--qd-accent)] text-[var(--qd-text)] px-3 py-2 text-xs font-mono uppercase rounded-sm">
              <Copy size={14} />
            </button>
            <button onClick={connectUpstox} disabled={!upstoxStatus.keys_saved} className="bg-[var(--qd-profit)] disabled:opacity-40 text-black hover:opacity-85 px-5 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2">
              <ExternalLink size={14} /> {upstoxStatus.connected ? "Re-connect Upstox" : "Connect Upstox"}
            </button>
          </div>
        </div>
      </div>

      <div className="qd-card p-5" data-testid="upstox-plus-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3">
          <Database size={16} className="text-[var(--qd-cyan)]" /> Upstox Plus Data
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-2">
          <Coverage label="F&O EOD" data={dataHealth?.data_coverage?.bhavcopy_fo} />
          <Coverage label="Options 1m" data={dataHealth?.data_coverage?.options_1m} />
          <Coverage label="Earnings" data={dataHealth?.data_coverage?.earnings_dates} />
          <Coverage label="Participant OI" data={dataHealth?.data_coverage?.participant_oi} />
        </div>
        <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-2 text-xs font-mono text-[var(--qd-text-3)]">
          {(dataHealth?.upstox_plus?.features || []).map((feature) => (
            <div key={feature} className="flex items-center gap-2">
              <Activity size={12} className="text-[var(--qd-profit)]" /> {feature}
            </div>
          ))}
        </div>
      </div>

      <div className="qd-card" data-testid="keys-list">
        <div className="border-b border-[var(--qd-border)] px-4 py-3"><h2 className="font-head text-base text-white">Linked Upstox Keys</h2></div>
        {keys.length === 0 ? (
          <div className="p-8 text-center font-mono text-sm text-[var(--qd-text-2)]">No Upstox keys linked.</div>
        ) : (
          <div className="qd-table-wrap"><table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-widest text-[var(--qd-text-3)] font-mono">
                <th className="px-4 py-2">Broker</th><th className="px-4 py-2">API Key</th><th className="px-4 py-2">Client</th><th className="px-4 py-2">Linked</th><th className="px-4 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {keys.map((k) => (
                <tr key={k.id} className="border-t border-[var(--qd-border)] hover:bg-[var(--qd-surface-2)]">
                  <td className="px-4 py-2.5 text-[var(--qd-text)] flex items-center gap-2"><ShieldCheck size={14} className="text-[var(--qd-profit)]" />{k.broker.toUpperCase()}</td>
                  <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{k.api_key_masked}</td>
                  <td className="px-4 py-2.5 text-[var(--qd-text-2)]">{k.user_id_at_broker || "-"}</td>
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
    <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</label>
    <input
      data-testid={testid}
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2 text-sm text-[var(--qd-text)] font-mono rounded-sm"
    />
  </div>
);

const Coverage = ({ label, data }) => {
  const ok = data?.available;
  return (
    <div className="border border-[var(--qd-border)] bg-[var(--qd-bg)] p-3 rounded-sm min-h-[90px]">
      <div className="text-[10px] uppercase tracking-widest font-mono text-[var(--qd-text-3)]">{label}</div>
      <div className={ok ? "mt-1 text-lg font-head text-[var(--qd-profit)]" : "mt-1 text-lg font-head text-[var(--qd-warn)]"}>
        {data?.days ?? 0} days
      </div>
      <div className="mt-1 text-[11px] font-mono text-[var(--qd-text-3)] break-words">
        {data?.first_day && data?.last_day ? `${data.first_day} to ${data.last_day}` : data?.reason || "No stored coverage"}
      </div>
    </div>
  );
};
