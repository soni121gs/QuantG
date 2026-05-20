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
  Activity,
  Wallet,
  Menu,
  X,
  UserCircle,
  TrendingUp,
  AlertCircle,
  ShieldAlert,
  HeartPulse,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { api, formatINR } from "../lib/api";
import { APP_VERSION_LABEL } from "../lib/version";

const NAV = [
  { to: "/dashboard", icon: LayoutDashboard, label: "Dashboard", id: "nav-dashboard" },
  { to: "/strategies", icon: Blocks, label: "Strategies", id: "nav-strategies" },
  { to: "/python", icon: Code2, label: "Python", id: "nav-python" },
  { to: "/visual", icon: Blocks, label: "Visual Builder", id: "nav-visual" },
  { to: "/ai-bot", icon: Bot, label: "AI Bot", id: "nav-aibot" },
  { to: "/orders", icon: ListOrdered, label: "Orders", id: "nav-orders" },
  { to: "/positions", icon: PieChart, label: "Positions", id: "nav-positions" },
  { to: "/market-hub", icon: HeartPulse, label: "Market Hub", id: "nav-market-hub" },
  { to: "/broker-keys", icon: KeyRound, label: "Broker Keys", id: "nav-keys" },
  { to: "/ops", icon: ShieldAlert, label: "Ops Console", id: "nav-ops" },
  { to: "/profile", icon: UserCircle, label: "Profile", id: "nav-profile" },
];

// Bottom-bar items for mobile (5 most-used)
const MOBILE_NAV = [
  { to: "/dashboard", icon: LayoutDashboard, label: "Home", id: "mnav-dashboard" },
  { to: "/strategies", icon: Blocks, label: "Strats", id: "mnav-strategies" },
  { to: "/ai-bot", icon: Bot, label: "AI Bot", id: "mnav-aibot" },
  { to: "/orders", icon: ListOrdered, label: "Orders", id: "mnav-orders" },
  { to: "/positions", icon: PieChart, label: "Holdings", id: "mnav-positions" },
];

export default function Layout({ children }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [now, setNow] = useState(new Date());
  const [pnl, setPnl] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [profile, setProfile] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [marketTrend, setMarketTrend] = useState(null);

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
    const t = setInterval(fetch, 5000);
    return () => clearInterval(t);
  }, []);

  const isMarketOpen = (() => {
    const h = now.getUTCHours() + 5;
    const m = now.getUTCMinutes() + 30;
    const minutes = (h % 24) * 60 + m;
    return minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 30;
  })();

  return (
    <div className="min-h-screen flex flex-col bg-[var(--qd-bg)]">
      {/* Top Bar */}
      <header
        className="sticky top-0 z-50 border-b border-[var(--qd-border)] bg-[#0a0a0b]/95 backdrop-blur"
        data-testid="top-bar"
      >
        <div className="flex items-center justify-between px-3 md:px-4 h-12 gap-3">
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
              <div className="w-6 h-6 bg-gradient-to-br from-[var(--qd-accent)] to-[#00d9ff] flex items-center justify-center rounded-sm">
                <TrendingUp size={14} className="text-white" strokeWidth={2.5} />
              </div>
              <span className="font-head font-bold tracking-tight text-white text-base">
                QUANT<span className="text-[var(--qd-accent)]">G</span> {APP_VERSION_LABEL}
              </span>
            </div>
            <div className="hidden md:flex items-center gap-2 text-xs">
              <span className="qd-live-dot" />
              <span className="font-mono uppercase tracking-wider text-[var(--qd-text-2)]">
                {isMarketOpen ? "🟢 MARKET OPEN" : "🔴 MARKET CLOSED"}
              </span>
            </div>
            {profile && (
              <span
                className={`font-mono text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-sm ${
                  profile.paper_mode
                    ? "bg-[rgba(255,159,10,0.12)] text-[var(--qd-warn)] border border-[var(--qd-warn)]"
                    : "bg-[rgba(76,255,0,0.12)] text-[#4cff00] border border-[#4cff00]"
                }`}
                data-testid="mode-badge"
                title={profile.paper_mode ? "Paper mode — orders are simulated" : "LIVE — real money at risk"}
              >
                {profile.paper_mode ? "📊 PAPER" : "🔴 LIVE"}
              </span>
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
                ₹{formatINR(pnl ?? 0)}
              </span>
            </div>
            <span className="hidden md:block font-mono text-xs text-[var(--qd-text-2)]" data-testid="top-time">
              {now.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} IST
            </span>
            <button
              type="button"
              className="text-xs font-mono text-[var(--qd-text-2)] hover:text-white flex items-center gap-1.5 px-2 py-1 border border-transparent hover:border-[var(--qd-border)] rounded-sm"
              onClick={() => {
                logout();
                navigate("/login");
              }}
              data-testid="logout-btn"
              aria-label="Logout"
            >
              <span className="hidden md:inline truncate max-w-[140px]">{user?.email}</span>
              <LogOut size={14} />
            </button>
          </div>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Desktop Sidebar */}
        <aside
          className="hidden lg:block w-52 border-r border-[var(--qd-border)] bg-[#08080a] sticky top-[48px] self-start h-[calc(100vh-48px)] overflow-y-auto"
          data-testid="sidebar"
        >
          <nav className="flex flex-col p-2 gap-0.5">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                data-testid={n.id}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-3 py-2 text-sm transition-colors rounded-sm ${
                    isActive
                      ? "bg-[var(--qd-surface-2)] text-white border-l-2 border-[var(--qd-accent)]"
                      : "text-[var(--qd-text-2)] hover:text-white hover:bg-[var(--qd-surface)]"
                  }`
                }
              >
                <n.icon size={16} strokeWidth={1.5} />
                <span>{n.label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="mt-auto p-3 text-[9px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider border-t border-[var(--qd-border)]">
            <div className="flex items-center gap-1 mb-2">
              <span className="w-2 h-2 bg-[var(--qd-accent)] rounded-full animate-pulse" />
              Advanced Trading
            </div>
            <div className="text-[8px] text-[var(--qd-text-2)]">
              {APP_VERSION_LABEL} • Real-time • Live
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
              className="absolute left-0 top-0 bottom-0 w-64 bg-[#08080a] border-r border-[var(--qd-border)] p-3 flex flex-col"
              onClick={(e) => e.stopPropagation()}
              data-testid="mobile-drawer"
            >
              <div className="flex items-center justify-between mb-4 px-1">
                <span className="font-head font-bold text-white">QuantG {APP_VERSION_LABEL}</span>
                <button onClick={() => setDrawerOpen(false)} data-testid="close-drawer" className="text-white">
                  <X size={20} />
                </button>
              </div>
              <nav className="flex flex-col gap-1 flex-1">
                {NAV.map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    onClick={() => setDrawerOpen(false)}
                    data-testid={`m-${n.id}`}
                    className={({ isActive }) =>
                      `flex items-center gap-3 px-3 py-2.5 text-sm rounded-sm ${
                        isActive
                          ? "bg-[var(--qd-surface-2)] text-white border-l-2 border-[var(--qd-accent)]"
                          : "text-[var(--qd-text-2)] hover:text-white hover:bg-[var(--qd-surface)]"
                      }`
                    }
                  >
                    <n.icon size={16} strokeWidth={1.5} />
                    <span>{n.label}</span>
                  </NavLink>
                ))}
              </nav>
              <div className="text-[9px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider border-t border-[var(--qd-border)] pt-2">
                Advanced Trading Platform {APP_VERSION_LABEL}
              </div>
            </aside>
          </div>
        )}

        {/* Main */}
        <main className="flex-1 min-w-0 p-3 md:p-6 pb-20 lg:pb-6 qd-grid-bg">
          {profile?.zerodha?.reason === "expired" && (
            <div className="qd-card border-l-2 border-l-[var(--qd-warn)] p-3 mb-4 flex items-center justify-between gap-3" data-testid="token-expired-banner">
              <div className="flex items-center gap-2">
                <AlertCircle size={16} className="text-[var(--qd-warn)]" />
                <span className="text-sm text-[var(--qd-text-2)]">
                  <span className="text-[var(--qd-warn)] font-semibold">⚠ Zerodha session expired.</span> Re-connect to resume live trading.
                </span>
              </div>
              <button onClick={() => navigate("/broker-keys")} className="bg-[var(--qd-warn)] text-black px-3 py-1.5 text-xs font-mono uppercase rounded-sm whitespace-nowrap">Re-connect</button>
            </div>
          )}
          {children}
        </main>
      </div>

      {/* Mobile bottom nav */}
      <nav
        className="lg:hidden fixed bottom-0 inset-x-0 z-50 grid grid-cols-5 bg-[#0a0a0b]/95 backdrop-blur border-t border-[var(--qd-border)]"
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
