import React, { useEffect, useState } from "react";
import { api } from "../lib/api";
import { KeyRound, Trash2, Save, ShieldCheck, AlertTriangle } from "lucide-react";

export default function ApiKeys() {
  const [keys, setKeys] = useState([]);
  const [form, setForm] = useState({ broker: "zerodha", api_key: "", api_secret: "", user_id_at_broker: "" });
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  const load = () => api.get("/broker/keys").then((r) => setKeys(r.data));
  useEffect(() => { load(); }, []);

  const save = async (e) => {
    e.preventDefault();
    setSaving(true); setMsg("");
    try {
      await api.post("/broker/keys", form);
      setMsg("✓ Keys saved securely");
      setForm({ ...form, api_key: "", api_secret: "" });
      load();
    } catch (e) {
      setMsg(`× ${e.response?.data?.detail || "Save failed"}`);
    } finally { setSaving(false); }
  };

  const del = async (id) => {
    await api.delete(`/broker/keys/${id}`);
    load();
  };

  return (
    <div className="space-y-4 max-w-4xl" data-testid="api-keys-page">
      <div>
        <div className="font-mono text-[10px] tracking-widest uppercase text-[var(--qd-text-3)]">// CREDENTIALS</div>
        <h1 className="font-head text-3xl font-bold text-white mt-1">Broker Keys</h1>
        <p className="text-sm text-[var(--qd-text-2)] mt-1">Connect Zerodha Kite Connect to enable real-money execution.</p>
      </div>

      <div className="qd-card p-4 flex items-start gap-3 border-l-2 border-l-[var(--qd-warn)]" data-testid="paper-banner">
        <AlertTriangle size={16} className="text-[var(--qd-warn)] mt-0.5" />
        <div className="text-xs text-[var(--qd-text-2)]">
          <span className="text-white font-semibold">Paper Trading Mode is active.</span> Keys are stored encrypted for future live execution; current orders simulate against live mock prices.
        </div>
      </div>

      <form onSubmit={save} className="qd-card p-5 space-y-3" data-testid="keys-form">
        <h2 className="font-head text-lg text-white flex items-center gap-2"><KeyRound size={16} /> Zerodha Kite Connect</h2>
        <Input label="API Key" value={form.api_key} onChange={(v) => setForm({ ...form, api_key: v })} testid="api-key-input" />
        <Input label="API Secret" value={form.api_secret} onChange={(v) => setForm({ ...form, api_secret: v })} type="password" testid="api-secret-input" />
        <Input label="Client ID (optional)" value={form.user_id_at_broker} onChange={(v) => setForm({ ...form, user_id_at_broker: v })} testid="input-client-id" />
        {msg && <div className="font-mono text-xs text-[var(--qd-text-2)]">{msg}</div>}
        <button disabled={saving} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white px-4 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2" data-testid="save-broker-keys-btn">
          <Save size={14} /> Save Keys
        </button>
      </form>

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
