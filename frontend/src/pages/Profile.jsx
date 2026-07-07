import React, { useEffect, useState } from "react";
import { api } from "../lib/api";
import { User, Settings2, Lock, Activity, AlertTriangle, CheckCircle2, XCircle, Zap, Bell, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PageHeader, StatusBadge } from "../components/ui/app-shell";

export default function Profile() {
  const [p, setP] = useState(null);
  const [form, setForm] = useState({});
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  const [busy, setBusy] = useState(false);
  const [showReadiness, setShowReadiness] = useState(false);
  const [notificationPrefs, setNotificationPrefs] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem("quantg-notification-prefs") || "{}");
    } catch {
      return {};
    }
  });

  const updateNotificationPref = (key, value) => {
    const next = { ...notificationPrefs, [key]: value };
    setNotificationPrefs(next);
    localStorage.setItem("quantg-notification-prefs", JSON.stringify(next));
    if (key === "browser_alerts") {
      localStorage.setItem("quantg-browser-alerts", value ? "true" : "false");
    }
    toast.success("Notification preference saved");
  };

  const load = () =>
    api.get("/profile").then((r) => {
      setP(r.data);
      setForm({
        name: r.data.name || "",
        default_qty: r.data.default_qty,
        default_product: r.data.default_product,
        max_daily_loss: r.data.max_daily_loss,
        max_position_size: r.data.max_position_size,
        per_strategy_capital: r.data.per_strategy_capital,
        max_trades_per_day: r.data.max_trades_per_day,
        data_broker: "upstox",
        execution_broker: "upstox",
        fallback_broker: "none",
        paper_mode: r.data.paper_mode,
      });
    });
  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true);
    try {
      await api.put("/profile", {
        ...form,
        data_broker: "upstox",
        execution_broker: "upstox",
        fallback_broker: "none",
      });
      toast.success("Profile updated");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Update failed");
    } finally {
      setBusy(false);
    }
  };

  const activateLive = async (strategyId) => {
    setBusy(true);
    try {
      const res = await api.post("/core/live/activate", {
        strategy_id: strategyId,
        acknowledge_real_money: true,
      });
      toast.success(`LIVE armed for ${res.data?.strategy?.name || "selected strategy"}`);
      load();
    } catch (e) {
      const detail = e.response?.data?.detail;
      const blockers = detail?.blockers;
      toast.error(blockers?.length ? blockers[0] : detail?.message || detail || "Live activation failed");
      throw e;
    } finally {
      setBusy(false);
    }
  };

  const switchToPaper = async () => {
    setBusy(true);
    try {
      await api.post("/core/live/disarm");
      toast.success("Live disarmed. Switched to PAPER mode.");
      load();
    } catch (e) {
      toast.error(e.response?.data?.detail || "Failed to switch to paper");
      throw e;
    } finally {
      setBusy(false);
    }
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
    } finally {
      setBusy(false);
    }
  };

  if (!p) return <div className="font-mono text-sm text-[var(--qd-text-2)]">Loading...</div>;

  return (
    <div className="space-y-4 max-w-5xl" data-testid="profile-page">
      <PageHeader
        eyebrow="Account"
        title="Profile"
        subtitle="Personal account, trading safety limits, and notification preferences."
        badge={<StatusBadge tone={form.paper_mode ? "paper" : "live"}>{form.paper_mode ? "Paper Mode" : "Live Mode"}</StatusBadge>}
      />

      <div className="qd-card p-5 flex flex-col md:flex-row md:items-center justify-between gap-3" data-testid="paper-live-switch">
        <div className="flex items-start gap-3">
          <AlertTriangle size={22} className={form.paper_mode ? "text-[var(--qd-warn)]" : "text-[var(--qd-loss)]"} />
          <div>
            <div className="font-head text-lg text-[var(--qd-text)]">Trading Mode</div>
            <div className="text-sm text-[var(--qd-text-2)]">
              {form.paper_mode
                ? "PAPER - orders are simulated locally. Safe to test strategies."
                : "LIVE - orders route through Upstox. Real money at risk."}
            </div>
          </div>
        </div>
        <button
          onClick={() => setShowReadiness(true)}
          className={`px-5 py-2.5 font-mono text-sm uppercase tracking-wider rounded-sm ${
            form.paper_mode ? "border border-[var(--qd-border)] text-[var(--qd-text)] hover:border-[var(--qd-border-strong)]" : "qd-btn-sell"
          }`}
          data-testid="toggle-mode-btn"
        >
          {form.paper_mode ? "Go LIVE" : "Back to Paper"}
        </button>
      </div>

      {showReadiness && (
        <ReadinessModal
          isPaper={form.paper_mode}
          busy={busy}
          onClose={() => setShowReadiness(false)}
          onConfirm={async (strategyId) => {
            if (form.paper_mode) await activateLive(strategyId);
            else await switchToPaper();
            setShowReadiness(false);
          }}
        />
      )}

      <div className="qd-card p-5" data-testid="basic-info-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><User size={16} /> Basic Info</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Input label="Name" value={form.name} onChange={(v) => setForm({ ...form, name: v })} testid="input-name" />
          <Input label="Email" value={p.email} disabled />
        </div>
      </div>

      <div className="qd-card p-5" data-testid="prefs-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><Settings2 size={16} /> Trading Preferences</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Input label="Default Qty" type="number" value={form.default_qty} onChange={(v) => setForm({ ...form, default_qty: +v })} testid="input-qty" />
          <SelectIn label="Default Product" value={form.default_product} onChange={(v) => setForm({ ...form, default_product: v })} options={["MIS", "CNC", "NRML"]} testid="input-product" />
          <Input label="Max Daily Loss (INR)" type="number" value={form.max_daily_loss} onChange={(v) => setForm({ ...form, max_daily_loss: +v })} testid="input-max-loss" />
          <Input label="Max Position Size (INR)" type="number" value={form.max_position_size} onChange={(v) => setForm({ ...form, max_position_size: +v })} testid="input-max-size" />
          <Input label="Per-Strategy Capital (INR)" type="number" value={form.per_strategy_capital} onChange={(v) => setForm({ ...form, per_strategy_capital: +v })} testid="input-strategy-capital" />
          <Input label="Max Trades Per Day" type="number" value={form.max_trades_per_day} onChange={(v) => setForm({ ...form, max_trades_per_day: +v })} testid="input-max-trades" />
          <SelectIn label="Data Broker" value="upstox" onChange={() => {}} options={["upstox"]} testid="input-data-broker" />
          <SelectIn label="Execution Broker" value="upstox" onChange={() => {}} options={["upstox"]} testid="input-execution-broker" />
          <SelectIn label="Fallback Broker" value="none" onChange={() => {}} options={["none"]} testid="input-fallback-broker" />
        </div>
        <button onClick={save} disabled={busy} className="qd-force-white mt-4 bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 px-4 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2" data-testid="save-profile-btn">
          {busy ? <Loader2 size={13} className="animate-spin" /> : null}
          {busy ? "Saving..." : "Save Preferences"}
        </button>
      </div>

      <div className="qd-card p-5" data-testid="notification-prefs-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><Bell size={16} /> Notification Preferences</h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <PrefToggle
            label="Browser Alerts"
            description="Show desktop alerts for critical trading events after permission."
            checked={!!notificationPrefs.browser_alerts}
            onChange={(v) => updateNotificationPref("browser_alerts", v)}
          />
          <PrefToggle
            label="Critical Only"
            description="Keep the drawer focused on rejected orders and risk mismatches."
            checked={notificationPrefs.critical_only !== false}
            onChange={(v) => updateNotificationPref("critical_only", v)}
          />
          <PrefToggle
            label="Sound Off"
            description="Keep QuantG quiet while still showing visual alerts."
            checked={notificationPrefs.sound_off !== false}
            onChange={(v) => updateNotificationPref("sound_off", v)}
          />
        </div>
      </div>

      <form onSubmit={changePassword} className="qd-card p-5 space-y-3" data-testid="password-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2"><Lock size={16} /> Change Password</h2>
        <Input label="Current Password" type="password" value={pw.current_password} onChange={(v) => setPw({ ...pw, current_password: v })} testid="input-current-pw" />
        <Input label="New Password" type="password" value={pw.new_password} onChange={(v) => setPw({ ...pw, new_password: v })} testid="input-new-pw" />
        <button type="submit" disabled={busy || !pw.current_password || !pw.new_password} className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] disabled:opacity-50 text-white px-4 py-2 font-mono text-xs uppercase tracking-wider rounded-sm flex items-center gap-2" data-testid="change-pw-btn">
          {busy ? <Loader2 size={13} className="animate-spin" /> : null}
          {busy ? "Saving..." : "Change Password"}
        </button>
      </form>

      <div className="qd-card p-5" data-testid="session-card">
        <h2 className="font-head text-lg text-white flex items-center gap-2 mb-3"><Activity size={16} /> Session</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <Stat label="User ID" value={p.id.slice(0, 8) + "..."} mono />
          <Stat label="Role" value={p.role ? p.role.toUpperCase() : "TRADER"} mono tone={p.role === "owner" ? "warn" : "normal"} />
          <Stat label="Created" value={new Date(p.created_at).toLocaleDateString()} mono />
          <Stat label="Mode" value={form.paper_mode ? "PAPER" : "LIVE"} mono tone={form.paper_mode ? "warn" : "loss"} />
        </div>
      </div>
    </div>
  );
}

const Input = ({ label, value, onChange, type = "text", testid, disabled }) => (
  <div>
    <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</label>
    <input
      data-testid={testid}
      type={type}
      value={value ?? ""}
      onChange={(e) => onChange && onChange(e.target.value)}
      disabled={disabled}
      className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] focus:border-[var(--qd-accent)] outline-none px-3 py-2 text-sm text-[var(--qd-text)] font-mono rounded-sm disabled:opacity-50"
    />
  </div>
);

const SelectIn = ({ label, value, onChange, options, testid }) => (
  <div>
    <label className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</label>
    <select data-testid={testid} value={value} onChange={(e) => onChange(e.target.value)} className="w-full mt-1 bg-[var(--qd-bg)] border border-[var(--qd-border)] px-3 py-2 text-sm text-[var(--qd-text)] font-mono rounded-sm">
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  </div>
);

const Stat = ({ label, value, mono, tone }) => (
  <div>
    <div className="font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">{label}</div>
    <div className={`mt-1 text-sm ${mono ? "font-mono" : ""} ${
      tone === "warn" ? "text-[var(--qd-warn)]" : tone === "loss" ? "text-[var(--qd-loss)]" : "text-[var(--qd-text)]"
    }`}>{value}</div>
  </div>
);

const PrefToggle = ({ label, description, checked, onChange }) => (
  <label className="flex cursor-pointer items-start gap-3 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
    <input
      type="checkbox"
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
      className="mt-1 h-4 w-4 accent-[var(--qd-accent)]"
    />
    <span>
      <span className="block font-head text-sm font-bold text-[var(--qd-text)]">{label}</span>
      <span className="mt-1 block text-xs leading-relaxed text-[var(--qd-text-2)]">{description}</span>
    </span>
  </label>
);

function ReadinessModal({ isPaper, busy, onClose, onConfirm }) {
  const [data, setData] = useState(null);
  const [strategies, setStrategies] = useState([]);
  const [margins, setMargins] = useState({});
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [activationError, setActivationError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.get("/trading/live-readiness"),
      api.get("/strategies"),
      api.get("/strategies/margin-estimates").catch(() => ({ data: { estimates: {} } })),
    ])
      .then(([readyRes, strategiesRes, marginRes]) => {
        if (!alive) return;
        const rows = strategiesRes.data || [];
        setData(readyRes.data);
        setStrategies(rows);
        setMargins(marginRes.data?.estimates || {});
        const first = rows.find((s) => {
          const opts = s.visual_config?.options || {};
          return s.status !== "archived" && opts.structure === "credit_spread";
        });
        if (first) setSelectedStrategyId(first.id);
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, []);

  if (isPaper === false) {
    return (
      <div className="fixed inset-0 bg-black/70 z-[80] flex items-center justify-center p-4" onClick={onClose}>
        <div className="qd-card max-w-md w-full max-h-[90vh] qd-modal-card p-6" onClick={(e) => e.stopPropagation()}>
          <h2 className="font-head text-xl text-white mb-2">Switch back to Paper?</h2>
          <p className="text-sm text-[var(--qd-text-2)] mb-4">Live strategies will continue running but orders will be simulated locally.</p>
          <div className="flex gap-2">
            <button onClick={onClose} className="flex-1 border border-[var(--qd-border)] py-2 text-xs font-mono uppercase rounded-sm text-white">Cancel</button>
            <button onClick={() => onConfirm()} disabled={busy} className="flex-1 bg-[var(--qd-warn)] text-black py-2 text-xs font-mono uppercase rounded-sm disabled:opacity-50" data-testid="confirm-paper">Switch to Paper</button>
          </div>
        </div>
      </div>
    );
  }

  const liveChecksOk = !!data?.live_order_placement_ready;
  const candidates = strategies.filter((s) => {
    const opts = s.visual_config?.options || {};
    return s.status !== "archived" && opts.structure === "credit_spread";
  });
  const selectedMargin = margins[selectedStrategyId];
  const canConfirm = !!selectedStrategyId && !busy;
  const detailText = (value) => value == null ? "-" : Number(value).toLocaleString("en-IN", { style: "currency", currency: "INR" });

  const confirmLive = async () => {
    setActivationError(null);
    try {
      await onConfirm(selectedStrategyId);
    } catch (e) {
      setActivationError(e.response?.data?.detail || e.message || "Live activation failed");
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 z-[80] flex items-center justify-center p-4" onClick={onClose}>
      <div className="qd-card max-w-xl w-full max-h-[90vh] qd-modal-card p-6" onClick={(e) => e.stopPropagation()} data-testid="readiness-modal">
        <div className="flex items-center gap-2 mb-3">
          <Zap size={22} className="text-[var(--qd-loss)]" />
          <h2 className="font-head text-xl text-white">Go LIVE - Pre-flight Check</h2>
        </div>
        <p className="text-xs text-[var(--qd-text-2)] mb-4">This calls the backend live activation gate. It arms exactly one strategy only if the server, broker, funds, and reconciliation checks are real.</p>
        {loading ? (
          <div className="text-center py-10 font-mono text-sm text-[var(--qd-text-2)]">Running checks...</div>
        ) : (
          <>
            <div className="space-y-2 mb-4">
              {data?.checks?.map((c) => (
                <div key={c.id} className="flex items-start gap-3 p-2 bg-[var(--qd-bg)] border border-[var(--qd-border)] rounded-sm" data-testid={`check-${c.id}`}>
                  {c.ok ? <CheckCircle2 size={18} className="text-[var(--qd-profit)] mt-0.5 flex-shrink-0" />
                        : <XCircle size={18} className="text-[var(--qd-loss)] mt-0.5 flex-shrink-0" />}
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-white">{c.label}</div>
                    {c.detail && <div className="text-xs font-mono text-[var(--qd-text-3)]">{c.detail}</div>}
                    {!c.ok && c.hint && <div className="text-xs font-mono text-[var(--qd-warn)] mt-0.5">{c.hint}</div>}
                  </div>
                </div>
              ))}
            </div>

            <div className="mb-4 rounded-sm border border-[var(--qd-border)] bg-[var(--qd-bg)] p-3">
              <div className="mb-2 font-mono text-[11px] uppercase tracking-widest text-[var(--qd-text-3)]">Live strategy</div>
              {candidates.length ? (
                <select
                  value={selectedStrategyId}
                  onChange={(e) => setSelectedStrategyId(e.target.value)}
                  className="w-full rounded-sm border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 font-mono text-xs text-[var(--qd-text)] outline-none"
                  data-testid="live-strategy-select"
                >
                  {candidates.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
              ) : (
                <div className="text-xs text-[var(--qd-loss)]">No live-capable credit-spread strategy is available.</div>
              )}
              {selectedMargin?.required_margin ? (
                <div className="mt-2 grid grid-cols-2 gap-2 font-mono text-[11px] text-[var(--qd-text-3)]">
                  <span>Broker margin/lot</span>
                  <span className="text-right text-[var(--qd-warn)]">{detailText(selectedMargin.required_margin)}</span>
                  <span>Structure</span>
                  <span className="text-right">{selectedMargin.underlying} {selectedMargin.option_type} {selectedMargin.short_strike}/{selectedMargin.long_strike}</span>
                </div>
              ) : null}
            </div>

            {activationError ? (
              <div className="mb-4 rounded-sm border border-[rgba(255,59,48,0.35)] bg-[rgba(255,59,48,0.08)] p-3 text-xs text-[var(--qd-loss)]">
                {Array.isArray(activationError.blockers)
                  ? activationError.blockers.join(" ")
                  : activationError.message || String(activationError)}
              </div>
            ) : null}
          </>
        )}
        <div className="flex gap-2">
          <button onClick={onClose} className="flex-1 border border-[var(--qd-border)] py-2.5 text-xs font-mono uppercase rounded-sm text-white" data-testid="cancel-readiness">Stay Paper</button>
          <button
            onClick={confirmLive}
            disabled={!canConfirm}
            className={`flex-1 py-2.5 text-xs font-mono uppercase rounded-sm ${
              canConfirm ? "qd-btn-sell" : "bg-[var(--qd-surface-2)] text-[var(--qd-text-3)] cursor-not-allowed"
            }`}
            data-testid="confirm-live"
          >
            {busy ? "Arming..." : liveChecksOk ? "Arm selected strategy" : "Run backend activation check"}
          </button>
        </div>
        {!data?.market_open && liveChecksOk && (
          <p className="mt-3 text-xs font-mono text-[var(--qd-warn)] text-center">
            Market is currently closed. Live mode can still be armed for setup.
          </p>
        )}
      </div>
    </div>
  );
}
