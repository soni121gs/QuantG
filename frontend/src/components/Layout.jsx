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
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { api, formatINR } from "../lib/api";
import { APP_VERSION_LABEL } from "../lib/version";
import { Button } from "./ui/button";
import { CommandBar, StatusBadge } from "./ui/app-shell";

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

// Bottom-bar items for mobile (5 most-used)
const MOBILE_NAV = [
  { to: "/dashboard", icon: LayoutDashboard, label: "Home", id: "mnav-dashboard" },
  { to: "/strategies", icon: Blocks, label: "Strats", id: "mnav-strategies" },
  { to: "/orders", icon: ListOrdered, label: "Execution", id: "mnav-orders" },
  { to: "/ai-bot", icon: Bot, label: "Agent", id: "mnav-aibot" },
  { to: "/ops", icon: ShieldAlert, label: "Risk", id: "mnav-ops" },
];

export default function Layout({ children }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [now, setNow] = useState(new Date());
  const [pnl, setPnl] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [profile, setProfile] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [commandBusy, setCommandBusy] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

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
      {/* Top Bar */}
      <header className="sticky top-0 z-50 qd-topbar" data-testid="top-bar">
        <div className="flex items-center justify-between px-3 md:px-4 h-14 gap-3">
          <div className="flex items-center gap-3 md:gap-6 min-w-0">
            {/* Mobile hamburger */}
            <button
              type="button"
              className="lg:hidden text-white p-1"
              onClick={() => setDrawerOpen(true)}
              data-testid="open-drawer"
              aria-label="Open menu"
            >
              <Menu size={20} />
            </button>
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 bg-[var(--qd-accent)] flex items-center justify-center rounded-[var(--qd-radius-sm)] shadow-sm">
                <TrendingUp size={14} className="text-white" strokeWidth={2.5} />
              </div>
              <span className="font-head font-extrabold text-white text-base">
                QUANT<span className="text-[var(--qd-accent)]">G</span>
              </span>
              <span
                className="hidden sm:inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono font-bold uppercase tracking-widest bg-[var(--qd-accent)]/15 text-[var(--qd-accent)] border border-[var(--qd-accent)]/30"
                data-testid="version-badge"
                title="QuantG Terminal Version"
              >
                {APP_VERSION_LABEL}
              </span>
            </div>
            <div className="hidden md:flex items-center gap-2 text-xs">
              <span className="qd-live-dot" />
              <span className="font-mono uppercase tracking-wider text-[var(--qd-text-2)]">
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
          <div className="flex items-center gap-3 md:gap-6">
            <div className="flex items-center gap-1.5 md:gap-2">
              <Wallet size={14} className="text-[var(--qd-text-2)] hidden md:block" />
              <span
                className="font-mono text-[10px] md:text-xs text-[var(--qd-text-2)] uppercase tracking-wider"
                title={`${portfolio?.open_positions ?? 0} open positions`}
              >
                P&L
              </span>
              <span
                className={`font-mono text-xs md:text-sm font-semibold ${
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
            <div className="flex items-center gap-3">
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
        <CommandBar data-testid="global-command-bar" className="hidden lg:flex">
          <span className="mr-1 whitespace-nowrap qd-section-title">Command Center</span>
          <Button variant="outline" size="sm" onClick={refreshShell} data-testid="cmd-refresh">
            <RefreshCw size={14} /> Refresh
          </Button>
          <Button variant="outline" size="sm" onClick={syncBroker} disabled={commandBusy} data-testid="cmd-broker-sync">
            <ShieldCheck size={14} /> Broker Sync
          </Button>
          <Button variant="primary" size="sm" onClick={() => navigate("/strategies")} data-testid="cmd-new-strategy">
            <Plus size={14} /> New Strategy
          </Button>
          <Button variant="secondary" size="sm" onClick={() => navigate("/ai-bot")} data-testid="cmd-ask-ai">
            <Bot size={14} /> Ask AI
          </Button>
          <Button variant="danger" size="sm" onClick={() => navigate("/ops")} data-testid="cmd-risk-ops">
            <ShieldAlert size={14} /> Risk Ops
          </Button>
        </CommandBar>
        <CommandBar data-testid="mobile-command-row" className="lg:hidden">
          <Button variant="outline" size="sm" onClick={refreshShell} data-testid="mcmd-refresh">
            <RefreshCw size={14} /> Refresh
          </Button>
          <Button variant="outline" size="sm" onClick={syncBroker} disabled={commandBusy} data-testid="mcmd-broker-sync">
            <ShieldCheck size={14} /> Sync
          </Button>
          <Button variant="secondary" size="sm" onClick={() => navigate("/ai-bot")} data-testid="mcmd-ask-ai">
            <Bot size={14} /> AI
          </Button>
          <Button variant="danger" size="sm" onClick={() => navigate("/ops")} data-testid="mcmd-risk-ops">
            <ShieldAlert size={14} /> Risk
          </Button>
        </CommandBar>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Desktop Sidebar */}
        <aside
          className="hidden lg:flex lg:flex-col w-64 qd-sidebar sticky top-[98px] self-start h-[calc(100vh-98px)] overflow-y-auto"
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
              </nav>
              <div className="text-[9px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider border-t border-[var(--qd-border)] pt-2">
                Advanced Trading Platform {APP_VERSION_LABEL}
              </div>
            </aside>
          </div>
        )}

        {/* Main */}
        <main className="flex-1 min-w-0 p-3 md:p-6 pb-24 lg:pb-6 qd-grid-bg">
          {children}

          {/* SEBI Compliance F&O Risk Disclosure */}
          <div className="mt-8 p-4 qd-card bg-[rgba(255,159,10,0.03)] border-l-2 border-[var(--qd-warn)] text-xs text-[var(--qd-text-2)] leading-relaxed rounded-sm" data-testid="sebi-disclosure">
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

      {/* Mobile bottom nav */}
      <nav
        className="lg:hidden fixed bottom-0 inset-x-0 z-50 grid grid-cols-5 bg-[rgba(8,10,13,0.95)] backdrop-blur-xl border-t border-[var(--qd-border)]"
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
