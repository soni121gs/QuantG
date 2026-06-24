import React, { useCallback, useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  KeyRound,
  Code2,
  Blocks,
  Bot,
  ListOrdered,
  Calendar,
  LogOut,
  Menu,
  X,
  UserCircle,
  TrendingUp,
  TrendingDown,
  ShieldAlert,
  HeartPulse,
  RefreshCw,
  Plus,
  ShieldCheck,
  Bell,
  BellRing,
  Check,
  CheckCheck,
  Settings,
  ChevronDown,
  ExternalLink,
  AlertTriangle,
  BookOpen,
  BarChart3,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { api, formatINR } from "../lib/api";
import { toast } from "sonner";
import { APP_VERSION_LABEL } from "../lib/version";
import { useExecutionState } from "../hooks/useExecutionState";
import { usePolling } from "../hooks/usePolling";
import { Button } from "./ui/button";
import { StatusBadge } from "./ui/app-shell";
import ReadinessBanner from "./ReadinessBanner";

const NAV_GROUPS = [
  {
    label: "Trading",
    items: [
      { to: "/dashboard", icon: LayoutDashboard, label: "Dashboard", id: "nav-dashboard" },
      { to: "/strategies", icon: Blocks, label: "Strategies", id: "nav-strategies" },
      { to: "/orders", icon: ListOrdered, label: "Execution", id: "nav-orders" },
      { to: "/analytics", icon: BarChart3, label: "Analytics", id: "nav-analytics" },
    ],
  },
  {
    label: "Monitor",
    items: [
      { to: "/market-hub", icon: HeartPulse, label: "Markets", id: "nav-market-hub" },
      { to: "/calendar", icon: Calendar, label: "Calendar", id: "nav-calendar" },
      { to: "/wiki", icon: BookOpen, label: "Wiki & Notes", id: "nav-wiki" },
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
  { id: "daylight", label: "Light", detail: "Warm paper — calm, readable" },
  { id: "slate", label: "Dark", detail: "Warm graphite — easy on the eyes" },
];

const VALID_THEME_IDS = new Set(THEMES.map((theme) => theme.id));

// Bottom-bar items for mobile (5 most-used)
const MOBILE_NAV = [
  { to: "/dashboard", icon: LayoutDashboard, label: "Home", id: "mnav-dashboard" },
  { to: "/strategies", icon: Blocks, label: "Strats", id: "mnav-strategies" },
  { to: "/orders", icon: ListOrdered, label: "Execution", id: "mnav-orders" },
  { to: "/ai-bot", icon: Bot, label: "Agent", id: "mnav-aibot" },
  { to: "/ops", icon: ShieldAlert, label: "Risk", id: "mnav-ops" },
];

// Honest P&L direction glyph. (A real sparkline needs an intraday equity series,
// which the shell doesn't carry — a hardcoded fake line would be data-theater.)
const PnlTrend = ({ positive = true }) => {
  const Icon = positive ? TrendingUp : TrendingDown;
  return (
    <Icon
      size={16}
      className={positive ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}
      aria-hidden="true"
    />
  );
};

const getSeverityTone = (severity) => {
  switch (severity) {
    case "critical":
      return {
        icon: AlertTriangle,
        label: "Critical",
        className: "border-l-[var(--qd-loss)] bg-[color-mix(in_srgb,var(--qd-loss)_8%,var(--qd-surface))]",
        text: "text-[var(--qd-loss)]",
      };
    case "warning":
      return {
        icon: ShieldAlert,
        label: "Warning",
        className: "border-l-[var(--qd-warn)] bg-[color-mix(in_srgb,var(--qd-warn)_8%,var(--qd-surface))]",
        text: "text-[var(--qd-warn)]",
      };
    case "success":
      return {
        icon: Check,
        label: "Done",
        className: "border-l-[var(--qd-profit)] bg-[color-mix(in_srgb,var(--qd-profit)_8%,var(--qd-surface))]",
        text: "text-[var(--qd-profit)]",
      };
    default:
      return {
        icon: Bell,
        label: "Info",
        className: "border-l-[var(--qd-accent)] bg-[color-mix(in_srgb,var(--qd-accent)_8%,var(--qd-surface))]",
        text: "text-[var(--qd-accent)]",
      };
  }
};

const formatNotificationTime = (value) => {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
};

// Computes NSE market-open state from a Date (IST 09:15–15:30, Mon–Fri).
const computeMarketOpen = (d) => {
  const day = d.getUTCDay();
  const totalMinutesIST = d.getUTCHours() * 60 + d.getUTCMinutes() + 330;
  const istHours = Math.floor(totalMinutesIST / 60) % 24;
  const istMinutes = totalMinutesIST % 60;
  const minutes = istHours * 60 + istMinutes;
  const isWeekday = day >= 1 && day <= 5;
  if (!isWeekday) return false;
  return minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 30;
};

// Self-contained second clock — owns its own interval so it does NOT re-render
// the entire Layout shell every second (a real mobile perf cost during market).
const LiveTime = React.memo(function LiveTime() {
  const [time, setTime] = useState(() =>
    new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }),
  );
  useEffect(() => {
    const t = setInterval(() => {
      setTime(new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }));
    }, 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <span className="hidden md:block font-mono text-xs text-[var(--qd-text-2)]" data-testid="top-time">
      {time} IST
    </span>
  );
});

// Market badge only flips twice a day — recompute on a slow 20s cadence.
const MarketBadge = React.memo(function MarketBadge() {
  const [open, setOpen] = useState(() => computeMarketOpen(new Date()));
  useEffect(() => {
    const t = setInterval(() => setOpen(computeMarketOpen(new Date())), 20000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="hidden md:flex items-center gap-2 rounded-full border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-1.5 text-xs">
      <span className="qd-live-dot" />
      <span className="font-head text-[11px] font-bold uppercase tracking-wide text-[var(--qd-text-2)]">
        {open ? "MARKET OPEN" : "MARKET CLOSED"}
      </span>
    </div>
  );
});

export default function Layout({ children }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const {
    summary: executionSummary,
    refresh: refreshExecution,
  } = useExecutionState({ pollMs: 15000 });
  const [profile, setProfile] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [commandBusy, setCommandBusy] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [disclosureOpen, setDisclosureOpen] = useState(false);
  const [notificationOpen, setNotificationOpen] = useState(false);
  const [notificationTab, setNotificationTab] = useState("all");
  const [notifications, setNotifications] = useState([]);
  const [notificationCounts, setNotificationCounts] = useState({ unread: 0, critical: 0 });
  const [notificationPrefs, setNotificationPrefs] = useState(() => {
    if (typeof window === "undefined") return {};
    try {
      return JSON.parse(window.localStorage.getItem("quantg-notification-prefs") || "{}");
    } catch {
      return {};
    }
  });
  const [browserAlertsEnabled, setBrowserAlertsEnabled] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem("quantg-browser-alerts") === "true";
  });
  const [alertedNotificationIds, setAlertedNotificationIds] = useState(() => {
    if (typeof window === "undefined") return [];
    try {
      return JSON.parse(window.localStorage.getItem("quantg-alerted-notifications") || "[]");
    } catch {
      return [];
    }
  });
  const [theme, setTheme] = useState(() => {
    if (typeof window === "undefined") return "daylight";
    const saved = window.localStorage.getItem("quantg-theme-v2") || "daylight";
    return VALID_THEME_IDS.has(saved) ? saved : "daylight";
  });
  const [pendingActionsCount, setPendingActionsCount] = useState(0);

  useEffect(() => {
    document.documentElement.dataset.quantgTheme = theme;
    window.localStorage.setItem("quantg-theme-v2", theme);
  }, [theme]);

  const loadShellMeta = useCallback(async () => {
    await api.get("/profile").then((r) => setProfile(r.data)).catch(() => {});
  }, []);
  usePolling(loadShellMeta, 60000, { hiddenMs: 0 });

  const [shellRefreshing, setShellRefreshing] = useState(false);
  const refreshShell = async () => {
    setShellRefreshing(true);
    try {
      await Promise.all([
        refreshExecution(),
        api.get("/profile").then((r) => setProfile(r.data)).catch(() => {}),
      ]);
    } catch {
      toast.error("Refresh failed — data may be stale");
    } finally {
      setShellRefreshing(false);
    }
  };

  const loadNotifications = useCallback(() => {
    api.get("/notifications", { params: { limit: 60 } })
      .then((r) => setNotifications(r.data.notifications || []))
      .catch(() => {});
    api.get("/notifications/unread-count")
      .then((r) => setNotificationCounts({
        unread: r.data.unread || 0,
        critical: r.data.critical || 0,
      }))
      .catch(() => {});
    api.get("/agent/actions/pending")
      .then((r) => setPendingActionsCount(r.data.length || 0))
      .catch(() => {});
  }, []);

  usePolling(loadNotifications, 30000, { hiddenMs: 0 });

  useEffect(() => {
    const handleUpdate = () => {
      api.get("/agent/actions/pending")
        .then((r) => setPendingActionsCount(r.data.length || 0))
        .catch(() => {});
    };
    window.addEventListener("quantg-pending-actions-updated", handleUpdate);
    return () => window.removeEventListener("quantg-pending-actions-updated", handleUpdate);
  }, []);

  useEffect(() => {
    if (!notificationOpen || typeof window === "undefined") return;
    try {
      setNotificationPrefs(JSON.parse(window.localStorage.getItem("quantg-notification-prefs") || "{}"));
    } catch {
      setNotificationPrefs({});
    }
  }, [notificationOpen]);

  useEffect(() => {
    if (
      !browserAlertsEnabled ||
      typeof window === "undefined" ||
      !("Notification" in window) ||
      Notification.permission !== "granted"
    ) {
      return;
    }

    const fresh = notifications.filter((item) =>
      item.browser_alert &&
      !item.read &&
      !alertedNotificationIds.includes(item.id)
    );
    if (!fresh.length) return;

    fresh.slice(0, 3).forEach((item) => {
      const title = item.title || "QuantG notification";
      const options = {
        body: item.message || "Open QuantG for details.",
        tag: item.id,
      };
      // Android Chrome forbids `new Notification()` ("Illegal constructor") and
      // only allows notifications via the service worker. Prefer that path; fall
      // back to the constructor on desktop. Wrap everything so a notification
      // failure can never crash the app.
      try {
        if (navigator.serviceWorker && navigator.serviceWorker.ready) {
          navigator.serviceWorker.ready
            .then((reg) => reg.showNotification(title, options))
            .catch(() => {});
        } else {
          const note = new Notification(title, options);
          note.onclick = () => {
            window.focus();
            if (item.action_url) navigate(item.action_url);
          };
        }
      } catch {
        /* notifications unsupported on this browser — ignore */
      }
    });

    const next = [...fresh.map((item) => item.id), ...alertedNotificationIds].slice(0, 120);
    setAlertedNotificationIds(next);
    window.localStorage.setItem("quantg-alerted-notifications", JSON.stringify(next));
  }, [alertedNotificationIds, browserAlertsEnabled, navigate, notifications]);

  const enableBrowserAlerts = async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    const permission = await Notification.requestPermission();
    const enabled = permission === "granted";
    setBrowserAlertsEnabled(enabled);
    window.localStorage.setItem("quantg-browser-alerts", enabled ? "true" : "false");
  };

  const markNotificationRead = async (id) => {
    await api.post(`/notifications/${id}/read`);
    loadNotifications();
  };

  const markAllNotificationsRead = async () => {
    await api.post("/notifications/read-all");
    loadNotifications();
  };

  const visibleNotifications = notifications.filter((item) => {
    if (notificationTab === "unread") return !item.read;
    if (notificationTab === "critical") return item.severity === "critical";
    if (notificationPrefs.critical_only) return item.severity === "critical";
    return true;
  });

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
                <TrendingUp size={14} className="qd-force-white" strokeWidth={2.5} />
              </div>
              <div className="leading-none">
                <span className="font-head font-extrabold text-[var(--qd-text)] text-base">
                  Quant<span className="text-[var(--qd-accent)]">G</span>
                </span>
                <div className="mt-1 hidden text-[11px] font-semibold uppercase tracking-wide text-[var(--qd-text-3)] sm:block">
                  Trading Cockpit {APP_VERSION_LABEL}
                </div>
              </div>
            </div>
            <MarketBadge />
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
            <div className="hidden items-center gap-3 rounded-full border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-1.5 md:flex">
              <span
                className="font-head text-[11px] font-bold uppercase tracking-wide text-[var(--qd-text-3)]"
                title={`${executionSummary?.open_positions ?? 0} open positions`}
              >
                P&L
              </span>
              <PnlTrend positive={(executionSummary?.net_pnl ?? 0) >= 0} />
              <span
                className={`font-mono text-xs md:text-sm font-bold ${
                  (executionSummary?.net_pnl ?? 0) >= 0 ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"
                }`}
                data-testid="top-pnl"
              >
                INR {formatINR(executionSummary?.net_pnl ?? 0)}
              </span>
            </div>
            <LiveTime />
            <Button variant="ghost" size="icon" onClick={refreshShell} disabled={shellRefreshing} data-testid="cmd-refresh" aria-label="Refresh">
              <RefreshCw size={15} className={shellRefreshing ? "animate-spin" : ""} />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setNotificationOpen(true)}
              data-testid="notification-bell"
              aria-label="Notifications"
              className="relative"
            >
              <Bell size={16} />
              {notificationCounts.unread > 0 && (
                <span
                  className={`qd-force-white absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full px-1 font-mono text-[11px] font-bold ${
                    notificationCounts.critical > 0 ? "bg-[var(--qd-loss)]" : "bg-[var(--qd-accent)]"
                  }`}
                >
                  {notificationCounts.unread > 9 ? "9+" : notificationCounts.unread}
                </span>
              )}
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
                <div className="absolute right-0 mt-2 w-72 rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-elevated)] p-3 shadow-lg backdrop-blur-md">
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
                            : "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)]"
                        }`}
                        title={item.detail}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <div className="hidden lg:flex flex-col items-end leading-none font-mono">
                <span className="text-[11px] text-[var(--qd-text-2)]">{user?.email}</span>
                {user?.role && (
                  <span className={`text-[11px] uppercase tracking-widest mt-0.5 px-1.5 py-0.5 rounded-sm border font-semibold ${
                    user.role === "owner" 
                      ? "bg-[rgba(255,159,10,0.08)] text-[var(--qd-warn)] border-[var(--qd-warn)]/30" 
                      : "bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] border-[var(--qd-border)]"
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
                            ? "bg-[var(--qd-surface-3)] text-[var(--qd-text)] border border-[var(--qd-border-strong)]"
                            : "text-[var(--qd-text-2)] border border-transparent hover:text-[var(--qd-text)] hover:bg-[var(--qd-surface-2)]"
                        }`
                      }
                    >
                      <n.icon size={16} strokeWidth={1.7} />
                      <span>{n.label}</span>
                      {n.id === "nav-aibot" && pendingActionsCount > 0 && (
                        <span className="ml-auto inline-flex h-5 w-5 items-center justify-center rounded-full bg-[var(--qd-warn)] text-[10px] font-bold text-white shadow-sm">
                          {pendingActionsCount}
                        </span>
                      )}
                    </NavLink>
                  ))}
                </div>
              </div>
            ))}
          </nav>
          <div className="mt-auto p-3 text-[11px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider border-t border-[var(--qd-border)]">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1 mb-0">
                <span className="w-2 h-2 bg-[var(--qd-accent)] rounded-full animate-pulse" />
                Advanced Trading
              </div>
              <span
                className="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-mono font-bold uppercase tracking-widest bg-[var(--qd-accent)]/15 text-[var(--qd-accent)] border border-[var(--qd-accent)]/30"
                data-testid="sidebar-version-badge"
              >
                {APP_VERSION_LABEL}
              </span>
            </div>
            <div className="text-[11px] text-[var(--qd-text-2)] mt-1.5">
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
                <span className="font-head font-bold text-[var(--qd-text)]">QuantG {APP_VERSION_LABEL}</span>
                <button onClick={() => setDrawerOpen(false)} data-testid="close-drawer" className="text-[var(--qd-text)]">
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
                                ? "bg-[var(--qd-surface-2)] text-[var(--qd-text)] border border-[var(--qd-border-strong)]"
                                : "text-[var(--qd-text-2)] hover:text-[var(--qd-text)] hover:bg-[var(--qd-surface)]"
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
                        className={`rounded-[var(--qd-radius-sm)] border px-3 py-2 text-left font-mono text-[11px] font-bold uppercase tracking-wider ${
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
              <div className="text-[11px] font-mono text-[var(--qd-text-3)] uppercase tracking-wider border-t border-[var(--qd-border)] pt-2">
                Advanced Trading Platform {APP_VERSION_LABEL}
              </div>
            </aside>
          </div>
        )}

        {/* Main */}
        <main className="flex-1 min-w-0 qd-grid-bg">
          <div className="grid gap-4 p-3 pb-24 md:p-5 lg:pb-5 lg:grid-cols-1">
            <div className="min-w-0">
              <ReadinessBanner />
              {children}
              <button
                type="button"
                onClick={() => setDisclosureOpen(true)}
                className="mt-5 inline-flex items-center gap-2 rounded-full border border-[var(--qd-border)] bg-[var(--qd-surface)] px-3 py-2 text-xs font-semibold text-[var(--qd-text-2)] shadow-sm hover:text-[var(--qd-text)]"
                data-testid="sebi-disclosure-link"
              >
                <ShieldAlert size={13} /> F&O Risk Disclosure
              </button>
            </div>
          </div>
        </main>
      </div>

      {disclosureOpen && (
        <div className="fixed inset-0 z-[70] bg-slate-950/30 backdrop-blur-sm" onClick={() => setDisclosureOpen(false)}>
          <aside
            className="absolute right-0 top-0 h-full w-full max-w-md overflow-y-auto border-l border-[var(--qd-border)] bg-[var(--qd-elevated)] p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            data-testid="sebi-disclosure-drawer"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="qd-section-title text-[var(--qd-warn)]">Compliance</div>
                <h3 className="mt-2 font-head text-xl font-bold text-[var(--qd-text)]">SEBI F&O Risk Disclosure</h3>
              </div>
              <button type="button" onClick={() => setDisclosureOpen(false)} className="rounded-full p-2 text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)]" aria-label="Close disclosure">
                <X size={18} />
              </button>
            </div>
            <ul className="mt-5 list-disc space-y-3 pl-5 text-sm leading-relaxed text-[var(--qd-text-2)]">
              <li>9 out of 10 individual traders in equity Futures and Options segment incurred net losses.</li>
              <li>On average, loss makers registered a net trading loss close to INR 50,000.</li>
              <li>Loss makers expended an additional 28% of net trading losses as transaction costs.</li>
              <li>Those making net profits incurred transaction costs of 15% to 50% of net profits.</li>
            </ul>
            <p className="mt-5 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 text-xs text-[var(--qd-text-3)]">
              Source: SEBI study dated January 25, 2023 on analysis of profit and loss of individual traders dealing in equity Futures and Options segment.
            </p>
          </aside>
        </div>
      )}

      {notificationOpen && (
        <div className="fixed inset-0 z-[75] bg-slate-950/30 backdrop-blur-sm" onClick={() => setNotificationOpen(false)}>
          <aside
            className="absolute right-0 top-0 h-full w-full max-w-md overflow-y-auto border-l border-[var(--qd-border)] bg-[var(--qd-elevated)] p-4 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            data-testid="notification-drawer"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="qd-section-title">Notifications</div>
                <h3 className="mt-2 font-head text-xl font-extrabold text-[var(--qd-text)]">Trading alerts</h3>
              </div>
              <button type="button" onClick={() => setNotificationOpen(false)} className="rounded-full p-2 text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)]" aria-label="Close notifications">
                <X size={18} />
              </button>
            </div>

            <div className="mt-4 grid grid-cols-3 gap-2">
              {[
                { id: "all", label: "All" },
                { id: "unread", label: "Unread" },
                { id: "critical", label: "Critical" },
              ].map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setNotificationTab(item.id)}
                  className={`rounded-[var(--qd-radius-sm)] border px-3 py-2 font-head text-xs font-bold ${
                    notificationTab === item.id
                      ? "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-[var(--qd-accent-contrast)]"
                      : "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
                  }`}
                >
                  {item.label}
                </button>
              ))}
            </div>

            <div className="mt-4 grid grid-cols-2 gap-2">
              <Button variant="secondary" size="sm" onClick={enableBrowserAlerts}>
                <BellRing size={14} /> {browserAlertsEnabled ? "Alerts on" : "Browser alerts"}
              </Button>
              <Button variant="outline" size="sm" onClick={markAllNotificationsRead} disabled={!notificationCounts.unread}>
                <CheckCheck size={14} /> Mark read
              </Button>
            </div>
            {notificationPrefs.critical_only && notificationTab === "all" && (
              <div className="mt-3 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-3 py-2 text-xs text-[var(--qd-text-2)]">
                Profile preference is showing critical notifications only.
              </div>
            )}

            <div className="mt-4 space-y-3">
              {visibleNotifications.length === 0 && (
                <div className="rounded-[var(--qd-radius)] border border-dashed border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-6 text-center">
                  <Bell size={20} className="mx-auto text-[var(--qd-accent)]" />
                  <p className="mt-3 font-head text-sm font-bold text-[var(--qd-text)]">Nothing needs attention</p>
                  <p className="mt-1 text-xs text-[var(--qd-text-2)]">Order, strategy, and risk alerts will appear here.</p>
                </div>
              )}

              {visibleNotifications.map((item) => {
                const tone = getSeverityTone(item.severity);
                const Icon = tone.icon;
                return (
                  <article
                    key={item.id}
                    className={`border border-l-4 border-[var(--qd-border)] ${tone.className} rounded-[var(--qd-radius)] p-3 shadow-sm`}
                  >
                    <div className="flex items-start gap-3">
                      <div className={`mt-0.5 rounded-full bg-[var(--qd-surface-3)] p-2 ${tone.text}`}>
                        <Icon size={16} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <div className={`font-mono text-[11px] font-bold uppercase tracking-wide ${tone.text}`}>{tone.label}</div>
                            <h4 className="mt-1 font-head text-sm font-extrabold text-[var(--qd-text)]">{item.title}</h4>
                          </div>
                          {!item.read && <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-[var(--qd-accent)]" />}
                        </div>
                        <p className="mt-2 text-sm leading-relaxed text-[var(--qd-text-2)]">{item.message}</p>
                        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                          <span className="font-mono text-[11px] text-[var(--qd-text-3)]">{formatNotificationTime(item.created_at)}</span>
                          <div className="flex items-center gap-2">
                            {item.action_url && (
                              <button
                                type="button"
                                onClick={() => {
                                  navigate(item.action_url);
                                  setNotificationOpen(false);
                                }}
                                className="inline-flex items-center gap-1 rounded-full border border-[var(--qd-border)] bg-[var(--qd-surface-2)] px-2.5 py-1 text-xs font-semibold text-[var(--qd-text-2)] hover:text-[var(--qd-text)]"
                              >
                                Open <ExternalLink size={12} />
                              </button>
                            )}
                            {!item.read && (
                              <button
                                type="button"
                                onClick={() => markNotificationRead(item.id)}
                                className="qd-force-white inline-flex items-center gap-1 rounded-full bg-[var(--qd-accent)] px-2.5 py-1 text-xs font-semibold"
                              >
                                <Check size={12} /> Read
                              </button>
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          </aside>
        </div>
      )}

      {/* Mobile bottom nav */}
      <nav
        className="lg:hidden fixed bottom-0 inset-x-0 z-50 grid grid-cols-5 bg-[var(--qd-surface)] backdrop-blur-xl border-t border-[var(--qd-border)] shadow-[0_-12px_30px_rgba(15,23,42,0.08)]"
        data-testid="mobile-bottom-nav"
      >
        {MOBILE_NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            data-testid={n.id}
            className={({ isActive }) =>
              `flex min-w-0 flex-col items-center justify-center gap-0.5 py-2.5 text-[10px] font-mono uppercase tracking-wide transition-colors relative ${
                isActive ? "text-[var(--qd-accent)] bg-[var(--qd-surface)]/20" : "text-[var(--qd-text-2)]"
              }`
            }
          >
            <n.icon size={18} strokeWidth={1.5} />
            <span className="max-w-full truncate">{n.label}</span>
            {n.id === "mnav-aibot" && pendingActionsCount > 0 && (
              <span className="qd-force-white absolute right-[25%] top-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--qd-warn)] text-[9px] font-bold">
                {pendingActionsCount}
              </span>
            )}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
