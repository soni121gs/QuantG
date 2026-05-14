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
} from "lucide-react";
import TickerTape from "./TickerTape";
import { useAuth } from "../contexts/AuthContext";
import { api, formatINR } from "../lib/api";

const NAV = [
  { to: "/dashboard", icon: LayoutDashboard, label: "Dashboard", id: "nav-dashboard" },
  { to: "/strategies", icon: Blocks, label: "Strategies", id: "nav-strategies" },
  { to: "/python", icon: Code2, label: "Python Editor", id: "nav-python" },
  { to: "/visual", icon: Blocks, label: "Visual Builder", id: "nav-visual" },
  { to: "/ai-bot", icon: Bot, label: "AI Bot", id: "nav-aibot" },
  { to: "/orders", icon: ListOrdered, label: "Orders", id: "nav-orders" },
  { to: "/positions", icon: PieChart, label: "Positions", id: "nav-positions" },
  { to: "/api-keys", icon: KeyRound, label: "Broker Keys", id: "nav-keys" },
];

export default function Layout({ children }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [now, setNow] = useState(new Date());
  const [pnl, setPnl] = useState(null);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const fetch = () =>
      api.get("/portfolio").then((r) => setPnl(r.data.total_pnl)).catch(() => {});
    fetch();
    const t = setInterval(fetch, 4000);
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
        <div className="flex items-center justify-between px-4 h-12">
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-2">
              <div className="w-6 h-6 bg-[var(--qd-accent)] flex items-center justify-center">
                <Activity size={14} className="text-white" strokeWidth={2.5} />
              </div>
              <span className="font-head font-bold tracking-tight text-white text-base">
                QUANT<span className="text-[var(--qd-accent)]">DESK</span>
              </span>
            </div>
            <div className="hidden md:flex items-center gap-2 text-xs">
              <span className="qd-live-dot" />
              <span className="font-mono uppercase tracking-wider text-[var(--qd-text-2)]">
                {isMarketOpen ? "MARKET OPEN" : "MARKET CLOSED"}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-6">
            <div className="hidden md:flex items-center gap-2">
              <Wallet size={14} className="text-[var(--qd-text-2)]" />
              <span className="font-mono text-xs text-[var(--qd-text-2)] uppercase tracking-wider">PnL</span>
              <span
                className={`font-mono text-sm font-semibold ${
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
              className="text-xs font-mono text-[var(--qd-text-2)] hover:text-white flex items-center gap-1"
              onClick={() => {
                logout();
                navigate("/login");
              }}
              data-testid="logout-btn"
            >
              <LogOut size={14} /> {user?.email}
            </button>
          </div>
        </div>
        <TickerTape />
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Sidebar */}
        <aside
          className="w-52 border-r border-[var(--qd-border)] bg-[#08080a] sticky top-[80px] self-start h-[calc(100vh-80px)]"
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
          <div className="mt-auto p-3 text-[10px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider absolute bottom-2">
            v1.0 • Paper Trading
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 min-w-0 p-4 md:p-6 qd-grid-bg">{children}</main>
      </div>
    </div>
  );
}
