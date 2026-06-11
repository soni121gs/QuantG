import React, { useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  KeyRound,
  Code2,
  Blocks,
  Bot,
  ListOrdered,
  PieChart,
  Calendar,
  LogOut,
  Wallet,
  Menu,
  X,
  UserCircle,
  TrendingUp,
  ShieldAlert,
  HeartPulse,
  RefreshCw,
  Plus,
  ShieldCheck,
  Bell,
  Settings,
  ChevronDown,
  Gauge,
  PanelRight,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { api, formatINR } from "../lib/api";
import { APP_VERSION_LABEL } from "../lib/version";
import { Button } from "./ui/button";
import { StatusBadge } from "./ui/app-shell";

const NAV_GROUPS = [
  {
    label: "Trading",
    items: [
      { to: "/dashboard", icon: LayoutDashboard, label: "Dashboard", id: "nav-dashboard" },
      { to: "/strategies", icon: Blocks, label: "Strategies", id: "nav-strategies" },
      { to: "/orders", icon: ListOrdered, label: "Orders", id: "nav-orders" },
      { to: "/positions", icon: PieChart, label: "Positions", id: "nav-positions" },
    ],
  },
  {
    label: "Monitor",
    items: [
      { to: "/market-hub", icon: HeartPulse, label: "Markets", id: "nav-market-hub" },
      { to: "/calendar", icon: Calendar, label: "Calendar", id: "nav-calendar" },
      { to: "/ops", icon: ShieldAlert, label: "Risk Ops", id: "nav-ops" },
      { to: "/ai-bot", icon: Bot, label: "Ask Agent", id: "nav-aibot" },
    ],
  },
  {
    label: "Build",
    items: [
      { to: "/python", icon: Code2, label: "Python", id: "nav-python" },
      { to: "/visual", icon: Blocks, label: "Visual Builder", id: "nav-visual" },
    ],
  },
  {
    label: "Account",
    items: [
      { to: "/broker-keys", icon: KeyRound, label: "Brokers", id: "nav-keys" },
      { to: "/profile", icon: UserCircle, label: "Profile", id: "nav-profile" },
    ],
  },
];

const THEMES = [
  { id: "slate", label: "Slate", detail: "Institutional blue" },
  { id: "graphite", label: "Graphite", detail: "Warm gold" },
  { id: "daylight", label: "Daylight", detail: "Light desk" },
  { id: "aurora", label: "Aurora", detail: "Indigo color" },
];

// Bottom-bar items for mobile (5 most-used)
const MOBILE_NAV = [
  { to: "/dashboard", icon: LayoutDashboard, label: "Home", id: "mnav-dashboard" },
  { to: "/strategies", icon: Blocks, label: "Strats", id: "mnav-strategies" },
  { to: "/orders", icon: ListOrdered, label: "Execution", id: "mnav-orders" },
  { to: "/ai-bot", icon: Bot, label: "Agent", id: "mnav-aibot" },
  { to: "/ops", icon: ShieldAlert, label: "Risk", id: "mnav-ops" },
];

const Sparkline = ({ positive = true }) => (
  <svg viewBox="0 0 92 30" className="h-8 w-24" aria-hidden="true">
    <path
      d={positive ? "M2 22 L16 18 L28 20 L42 12 L56 15 L70 7 L90 10" : "M2 8 L16 12 L28 10 L42 18 L56 15 L70 24 L90 20"}
      fill="none"
      stroke={positive ? "var(--qd-profit)" : "var(--qd-loss)"}
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

const MiniBlotterDock = ({ portfolio, pnl, profile }) => {
  const cash = portfolio?.net_liquidation ?? portfolio?.total_value ?? portfolio?.available_cash ?? 0;
  const usedMargin = Number(portfolio?.used_margin ?? portfolio?.margin_used ?? 0);
  const marginPct = cash ? Math.min(100, Math.max(0, (usedMargin / Math.max(1, Number(cash))) * 100)) : 0;
  const activePositions = portfolio?.open_positions ?? portfolio?.positions_count ?? 0;
  const pnlPositive = Number(pnl || 0) >= 0;

  return (
    <aside className="qd-mini-blotter" data-testid="mini-blotter">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="qd-section-title">Mini Blotter</div>
          <div className="mt-1 text-xs text-[var(--qd-text-2)]">Always-on account context</div>
        </div>
        <PanelRight size={17} className="text-[var(--qd-accent)]" />
      </div>
      <div className="mt-4 grid gap-3">
        <div className="qd-blotter-metric">
          <span>Net Liq.</span>
          <strong>INR {formatINR(cash)}</strong>
        </div>
        <div className="qd-blotter-metric">
          <span>Open P&L</span>
          <strong className={pnlPositive ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}>INR {formatINR(pnl ?? 0)}</strong>
        </div>
        <div className="flex items-center justify-between rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-white/70 p-3">
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wide text-[var(--qd-text-3)]">P&L Flow</div>
            <div className="font-mono text-xs text-[var(--qd-text-2)]">{pnlPositive ? "Positive" : "Negative"} session</div>
          </div>
          <Sparkline positive={pnlPositive} />
        </div>
        <div>
          <div className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wide text-[var(--qd-text-3)]">
            <span>Margin Usage</span>
            <span className="font-mono">{marginPct.toFixed(0)}%</span>
          </div>
          <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-200">
            <div className="h-full rounded-full bg-[var(--qd-accent)]" style={{ width: `${marginPct}%` }} />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-white/70 p-3">
            <div className="text-[10px] font-semibold uppercase tracking-wide text-[var(--qd-text-3)]">Positions</div>
            <div className="mt-1 font-mono text-lg font-bold text-[var(--qd-text)]">{activePositions}</div>
          </div>
          <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-white/70 p-3">
            <div className="text-[10px] font-semibold uppercase tracking-wide text-[var(--qd-text-3)]">Mode</div>
            <div className={`mt-1 font-mono text-lg font-bold ${profile?.paper_mode ? "text-[var(--qd-warn)]" : "text-[var(--qd-loss)]"}`}>
              {profile?.paper_mode ? "PAPER" : "LIVE"}
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
};

export default function Layout({ children }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [now, setNow] = useState(new Date());
  const [pnl, setPnl] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [profile, setProfile] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [commandBusy, setCommandBusy] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [disclosureOpen, setDisclosureOpen] = useState(false);
  const [theme, setTheme] = useState(() => {
    if (typeof window === "undefined") return "daylight";
    return window.localStorage.getItem("quantg-theme-v2") || "daylight";
  });

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.quantgTheme = theme;
    window.localStorage.setItem("quantg-theme-v2", theme);
  }, [theme]);

  useEffect(() => {
    const fetch = () => {
      api.get("/portfolio").then((r) => {
        setPnl(r.data.total_pnl);
        setPortfolio(r.data);
      }).catch(() => {});
      api.get("/profile").then((r) => setProfile(r.data)).catch(() => {});
    };
    fetch();
    const t = setInterval(fetch, 60000);
    return () => clearInterval(t);
  }, []);

  const isMarketOpen = (() => {
    const day = now.getUTCDay();
    // Calculate IST hours and minutes
    const totalMinutesIST = now.getUTCHours() * 60 + now.getUTCMinutes() + 330;
    const istHours = Math.floor(totalMinutesIST / 60) % 24;
    const istMinutes = totalMinutesIST % 60;
    const minutes = istHours * 60 + istMinutes;
    
    const isWeekday = day >= 1 && day <= 5;
    if (!isWeekday) return false;
    
    const nseOpen = minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 30;
    return nseOpen;
  })();

  const refreshShell = () => {
    api.get("/portfolio").then((r) => {
      setPnl(r.data.total_pnl);
      setPortfolio(r.data);
    }).catch(() => {});
    api.get("/profile").then((r) => setProfile(r.data)).catch(() => {});
  };

  const syncBroker = async () => {
    setCommandBusy(true);
    try {
      await api.post("/ops/orders/sync");
      refreshShell();
    } finally {
      setCommandBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col qd-shell">
      <header className="sticky top-0 z-50 qd-topbar" data-testid="top-bar">
        <div className="flex h-16 items-center justify-between gap-3 px-3 md:px-4">
          <div className="flex items-center gap-3 md:gap-6 min-w-0">
            <button
              type="button"
              className="lg:hidden text-[var(--qd-text)] p-1"
              onClick={() => setDrawerOpen(true)}
              data-testid="open-drawer"
              aria-label="Open menu"
            >
              <Menu size={20} />
            </button>
            <div className="flex items-center gap-2">
              <div className="w-9 h-9 bg-[var(--qd-accent)] flex items-center justify-center rounded-[var(--qd-radius-sm)] shadow-sm">
                <TrendingUp size={14} className="text-white" strokeWidth={2.5} />
              </div>
              <div className="leading-none">
                <span className="font-head font-extrabold text-[var(--qd-text)] text-base">
                  Quant<span className="text-[var(--qd-accent)]">G</span>
                </span>
                <div className="mt-1 hidden text-[9px] font-semibold uppercase tracking-wide text-[var(--qd-text-3)] sm:block">
                  Trading Cockpit {APP_VERSION_LABEL}
                </div>
              </div>
            </div>
            <div className="hidden md:flex items-center gap-2 rounded-full border border-[var(--qd-border)] bg-white/70 px-3 py-1.5 text-xs">
              <span className="qd-live-dot" />
              <span className="font-head text-[11px] font-bold uppercase tracking-wide text-[var(--qd-text-2)]">
                {isMarketOpen ? "MARKET OPEN" : "MARKET CLOSED"}
              </span>
            </div>
            {profile && (
              <StatusBadge
                tone={profile.paper_mode ? "paper" : "live"}
                data-testid="mode-badge"
                title={profile.paper_mode ? "Paper mode - orders are simulated" : "LIVE - real money at risk"}
              >
                {profile.paper_mode ? "PAPER" : "LIVE"}
              </StatusBadge>
            )}
          </div>
          <div className="flex items-center gap-2 md:gap-3">
            <div className="hidden items-center gap-3 rounded-full border border-[var(--qd-border)] bg-white/70 px-3 py-1.5 md:flex">
              <span
                className="font-head text-[10px] font-bold uppercase tracking-wide text-[var(--qd-text-3)]"
                title={`${portfolio?.open_positions ?? 0} open positions`}
              >
                P&L
              </span>
              <Sparkline positive={(pnl ?? 0) >= 0} />
              <span
                className={`font-mono text-xs md:text-sm font-bold ${
                  (pnl ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"
                }`}
                data-testid="top-pnl"
              >
                INR {formatINR(pnl ?? 0)}
              </span>
            </div>
            <span className="hidden md:block font-mono text-xs text-[var(--qd-text-2)]" data-testid="top-time">
              {now.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} IST
            </span>
            <Button variant="ghost" size="icon" onClick={refreshShell} data-testid="cmd-refresh" aria-label="Refresh">
              <RefreshCw size={15} />
            </Button>
            <Button variant="ghost" size="icon" data-testid="notification-bell" aria-label="Notifications">
              <Bell size={16} />
            </Button>
            <div className="relative">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setSettingsOpen((v) => !v)}
                data-testid="shell-settings"
              >
                <Settings size={14} /> Settings <ChevronDown size={13} />
              </Button>
              {settingsOpen && (
                <div className="absolute right-0 mt-2 w-72 rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-white/90 p-3 shadow-lg backdrop-blur-md">
                  <div className="qd-section-title px-1">Quick Actions</div>
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    <Button variant="secondary" size="sm" onClick={syncBroker} disabled={commandBusy}>
                      <ShieldCheck size={14} /> Sync
                    </Button>
                    <Button variant="secondary" size="sm" onClick={() => navigate("/strategies")}>
                      <Plus size={14} /> Strategy
                    </Button>
                    <Button variant="secondary" size="sm" onClick={() => navigate("/ai-bot")}>
                      <Bot size={14} /> Agent
                    </Button>
                    <Button variant="danger" size="sm" onClick={() => navigate("/ops")}>
                      <ShieldAlert size={14} /> Risk
                    </Button>
                  </div>
                  <div className="mt-4 qd-section-title px-1">Theme</div>
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    {THEMES.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setTheme(item.id)}
                        className={`rounded-[var(--qd-radius-sm)] border px-3 py-2 text-left font-head text-[11px] font-bold ${
                          theme === item.id
                            ? "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-[var(--qd-accent-contrast)]"
                            : "border-[var(--qd-border)] bg-white text-[var(--qd-text-2)]"
                        }`}
                        title={item.detail}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                  <button
                    type="button"
                    onClick={() => navigate("/design-canvas")}
                    className="mt-3 w-full rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-white px-3 py-2 text-left text-xs font-semibold text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
                  >
                    Open design canvas
                  </button>
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <div className="hidden lg:flex flex-col items-end leading-none font-mono">
                <span className="text-[10px] text-[var(--qd-text-2)]">{user?.email}</span>
                {user?.role && (
                  <span className={`text-[8px] uppercase tracking-widest mt-0.5 px-1.5 py-0.5 rounded-sm border font-semibold ${
                    user.role === "owner" 
                      ? "bg-[rgba(255,159,10,0.08)] text-[var(--qd-warn)] border-[var(--qd-warn)]/30" 
                      : "bg-indigo-500/10 text-indigo-300 border-indigo-500/20"
                  }`}>
                    {user.role}
                  </span>
                )}
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => {
                  logout();
                  navigate("/login");
                }}
                data-testid="logout-btn"
                aria-label="Logout"
              >
                <LogOut size={14} />
              </Button>
            </div>
          </div>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Desktop Sidebar */}
        <aside
          className="hidden lg:flex lg:flex-col w-64 qd-sidebar sticky top-16 self-start h-[calc(100vh-64px)] overflow-y-auto"
          data-testid="sidebar"
        >
          <nav className="flex flex-col p-3 gap-4">
            {NAV_GROUPS.map((group) => (
              <div key={group.label}>
                <div className="qd-section-title px-3 pb-2">{group.label}</div>
                <div className="space-y-1">
                  {group.items.map((n) => (
                    <NavLink
                      key={n.to}
                      to={n.to}
                      data-testid={n.id}
                      className={({ isActive }) =>
                        `flex items-center gap-3 px-3 py-2.5 text-sm font-semibold rounded-[var(--qd-radius-sm)] ${
                          isActive
                            ? "bg-[var(--qd-surface-3)] text-white border border-[var(--qd-border-strong)]"
                            : "text-[var(--qd-text-2)] border border-transparent hover:text-white hover:bg-[var(--qd-surface-2)]"
                        }`
                      }
                    >
                      <n.icon size={16} strokeWidth={1.7} />
                      <span>{n.label}</span>
                    </NavLink>
                  ))}
                </div>
              </div>
            ))}
          </nav>
          <div className="mt-auto p-3 text-[9px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider border-t border-[var(--qd-border)]">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1 mb-0">
                <span className="w-2 h-2 bg-[var(--qd-accent)] rounded-full animate-pulse" />
                Advanced Trading
              </div>
              <span
                className="inline-flex items-center px-1.5 py-0.5 rounded text-[8px] font-mono font-bold uppercase tracking-widest bg-[var(--qd-accent)]/15 text-[var(--qd-accent)] border border-[var(--qd-accent)]/30"
                data-testid="sidebar-version-badge"
              >
                {APP_VERSION_LABEL}
              </span>
            </div>
            <div className="text-[8px] text-[var(--qd-text-2)] mt-1.5">
              QuantG Terminal · Real-time · Live
            </div>
          </div>
        </aside>

        {/* Mobile slide-in drawer */}
        {drawerOpen && (
          <div
            className="lg:hidden fixed inset-0 z-[60] bg-black/70"
            onClick={() => setDrawerOpen(false)}
            data-testid="drawer-overlay"
          >
            <aside
              className="absolute left-0 top-0 bottom-0 w-72 bg-[var(--qd-bg-2)] border-r border-[var(--qd-border)] p-3 flex flex-col"
              onClick={(e) => e.stopPropagation()}
              data-testid="mobile-drawer"
            >
              <div className="flex items-center justify-between mb-4 px-1">
                <span className="font-head font-bold text-white">QuantG {APP_VERSION_LABEL}</span>
                <button onClick={() => setDrawerOpen(false)} data-testid="close-drawer" className="text-white">
                  <X size={20} />
                </button>
              </div>
              <nav className="flex flex-col gap-4 flex-1 overflow-y-auto">
                {NAV_GROUPS.map((group) => (
                  <div key={group.label}>
                    <div className="qd-section-title px-3 pb-2">{group.label}</div>
                    <div className="space-y-1">
                      {group.items.map((n) => (
                        <NavLink
                          key={n.to}
                          to={n.to}
                          onClick={() => setDrawerOpen(false)}
                          data-testid={`m-${n.id}`}
                          className={({ isActive }) =>
                            `flex items-center gap-3 px-3 py-2.5 text-sm rounded-[var(--qd-radius-sm)] ${
                              isActive
                                ? "bg-[var(--qd-surface-2)] text-white border border-[var(--qd-border-strong)]"
                                : "text-[var(--qd-text-2)] hover:text-white hover:bg-[var(--qd-surface)]"
                            }`
                          }
                        >
                          <n.icon size={16} strokeWidth={1.7} />
                          <span>{n.label}</span>
                        </NavLink>
                      ))}
                    </div>
                  </div>
                ))}
                <div>
                  <div className="qd-section-title px-3 pb-2">Theme</div>
                  <div className="grid grid-cols-2 gap-2 px-3">
                    {THEMES.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setTheme(item.id)}
                        className={`rounded-[var(--qd-radius-sm)] border px-3 py-2 text-left font-mono text-[10px] font-bold uppercase tracking-wider ${
                          theme === item.id
                            ? "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-[var(--qd-accent-contrast)]"
                            : "border-[var(--qd-border)] bg-[var(--qd-surface)] text-[var(--qd-text-2)]"
                        }`}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>
              </nav>
              <div className="text-[9px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider border-t border-[var(--qd-border)] pt-2">
                Advanced Trading Platform {APP_VERSION_LABEL}
              </div>
            </aside>
          </div>
        )}

        {/* Main */}
        <main className="flex-1 min-w-0 qd-grid-bg">
          <div className="grid gap-4 p-3 pb-24 md:p-5 lg:grid-cols-[minmax(0,1fr)_240px] lg:pb-5 xl:grid-cols-[minmax(0,1fr)_260px]">
            <div className="min-w-0">
              {children}
              <button
                type="button"
                onClick={() => setDisclosureOpen(true)}
                className="mt-5 inline-flex items-center gap-2 rounded-full border border-[var(--qd-border)] bg-white/75 px-3 py-2 text-xs font-semibold text-[var(--qd-text-2)] shadow-sm hover:text-[var(--qd-text)]"
                data-testid="sebi-disclosure-link"
              >
                <ShieldAlert size={13} /> F&O Risk Disclosure
              </button>
            </div>
            <MiniBlotterDock portfolio={portfolio} pnl={pnl} profile={profile} />
          </div>

          {/* SEBI Compliance F&O Risk Disclosure */}
          <div className="hidden" data-testid="sebi-disclosure">
            <h4 className="font-mono font-bold text-[var(--qd-warn)] uppercase tracking-wider mb-2 flex items-center gap-1.5">
              <ShieldAlert size={14} /> SEBI Mandated Derivative Risk Disclosure
            </h4>
            <ul className="list-disc pl-4 space-y-1 font-mono text-[10px]">
              <li>9 out of 10 individual traders in equity Futures and Options (F&O) segment incurred net losses.</li>
              <li>On average, loss makers registered a net trading loss close to ₹ 50,000.</li>
              <li>Over and above the net trading losses incurred, loss-makers expended an additional 28% of net trading losses as transaction costs.</li>
              <li>Those making net profits incurred transaction costs of 15% to 50% of their net profits.</li>
            </ul>
            <p className="mt-2 text-[9px] text-[var(--qd-text-3)] font-mono italic">
              Source: SEBI study dated January 25, 2023 on "Analysis of Profit and Loss of Individual Traders dealing in equity Futures and Options Segment".
            </p>
          </div>
        </main>
      </div>

      {disclosureOpen && (
        <div className="fixed inset-0 z-[70] bg-slate-950/30 backdrop-blur-sm" onClick={() => setDisclosureOpen(false)}>
          <aside
            className="absolute right-0 top-0 h-full w-full max-w-md overflow-y-auto border-l border-[var(--qd-border)] bg-white p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            data-testid="sebi-disclosure-drawer"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="qd-section-title text-[var(--qd-warn)]">Compliance</div>
                <h3 className="mt-2 font-head text-xl font-bold text-[var(--qd-text)]">SEBI F&O Risk Disclosure</h3>
              </div>
              <button type="button" onClick={() => setDisclosureOpen(false)} className="rounded-full p-2 text-[var(--qd-text-2)] hover:bg-slate-100" aria-label="Close disclosure">
                <X size={18} />
              </button>
            </div>
            <ul className="mt-5 list-disc space-y-3 pl-5 text-sm leading-relaxed text-[var(--qd-text-2)]">
              <li>9 out of 10 individual traders in equity Futures and Options segment incurred net losses.</li>
              <li>On average, loss makers registered a net trading loss close to INR 50,000.</li>
              <li>Loss makers expended an additional 28% of net trading losses as transaction costs.</li>
              <li>Those making net profits incurred transaction costs of 15% to 50% of net profits.</li>
            </ul>
            <p className="mt-5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-slate-50 p-3 text-xs text-[var(--qd-text-3)]">
              Source: SEBI study dated January 25, 2023 on analysis of profit and loss of individual traders dealing in equity Futures and Options segment.
            </p>
          </aside>
        </div>
      )}

      {/* Mobile bottom nav */}
      <nav
        className="lg:hidden fixed bottom-0 inset-x-0 z-50 grid grid-cols-5 bg-white/90 backdrop-blur-xl border-t border-[var(--qd-border)] shadow-[0_-12px_30px_rgba(15,23,42,0.08)]"
        data-testid="mobile-bottom-nav"
      >
        {MOBILE_NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            data-testid={n.id}
            className={({ isActive }) =>
              `flex flex-col items-center justify-center gap-0.5 py-2 text-[10px] font-mono uppercase tracking-wider transition-colors ${
                isActive ? "text-[var(--qd-accent)] bg-[var(--qd-surface)]/20" : "text-[var(--qd-text-2)]"
              }`
            }
          >
            <n.icon size={18} strokeWidth={1.5} />
            <span>{n.label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
