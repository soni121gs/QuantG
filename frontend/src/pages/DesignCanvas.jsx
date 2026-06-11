import React from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  CheckCircle2,
  Clock3,
  Gauge,
  LineChart,
  Palette,
  Radio,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-react";

const marketRows = [
  { symbol: "NIFTY 50", price: "23,468.35", change: "+0.42%", tone: "good" },
  { symbol: "BANKNIFTY", price: "51,284.10", change: "+0.18%", tone: "good" },
  { symbol: "FINNIFTY", price: "23,104.75", change: "-0.11%", tone: "bad" },
  { symbol: "INDIA VIX", price: "13.82", change: "-2.70%", tone: "good" },
];

const strategies = [
  { name: "NIFTY ORB Option Buy", state: "Scanning", pnl: "+INR 2,840", score: 82 },
  { name: "BankNifty Pullback", state: "Cooldown", pnl: "-INR 690", score: 64 },
  { name: "MCX Crude Micro", state: "Waiting", pnl: "+INR 1,120", score: 76 },
];

const themes = [
  {
    id: "slate",
    label: "Slate Institutional",
    note: "Refined dark-blue, calm, execution-first.",
    colors: ["#07111f", "#54a3ff", "#37d5e8", "#35d07f"],
  },
  {
    id: "graphite",
    label: "Warm Graphite",
    note: "Warm dark desk with restrained gold energy.",
    colors: ["#11100d", "#f0b84f", "#82d8c8", "#49d48b"],
  },
  {
    id: "daylight",
    label: "Daylight",
    note: "Light institutional workspace for long sessions.",
    colors: ["#f3f6fb", "#2563eb", "#0891b2", "#15803d"],
  },
  {
    id: "aurora",
    label: "Aurora",
    note: "Refined indigo with tasteful color and glow.",
    colors: ["#0c1021", "#8b7dff", "#44d9e6", "#40d99b"],
  },
];

const toneClass = {
  good: "text-[var(--qd-profit)]",
  bad: "text-[var(--qd-loss)]",
  warn: "text-[var(--qd-warn)]",
};

function MiniMetric({ icon: Icon, label, value, tone = "neutral" }) {
  return (
    <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface)] p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="qd-section-title text-[8px]">{label}</span>
        <Icon size={13} className="text-[var(--qd-text-3)]" />
      </div>
      <div className={`mt-2 font-mono text-lg font-bold ${toneClass[tone] || "text-white"}`}>{value}</div>
    </div>
  );
}

function DashboardArtboard({ theme }) {
  return (
    <article className="qd-card qd-artboard" data-quantg-theme={theme.id}>
      <div className="flex flex-col gap-4 border-b border-[var(--qd-border)] p-4 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="qd-chip">
              <Palette size={12} /> {theme.label}
            </span>
            <span className="qd-chip">
              <Radio size={12} /> NSE Live
            </span>
          </div>
          <h2 className="mt-4 font-head text-2xl font-extrabold text-white">QuantG Control Room</h2>
          <p className="mt-2 max-w-xl text-sm text-[var(--qd-text-2)]">{theme.note}</p>
        </div>
        <div className="grid grid-cols-2 gap-2 text-right font-mono text-xs">
          <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
            <div className="qd-section-title text-[8px]">Session</div>
            <div className="mt-1 font-bold text-[var(--qd-profit)]">OPEN</div>
          </div>
          <div className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3">
            <div className="qd-section-title text-[8px]">Mode</div>
            <div className="mt-1 font-bold text-[var(--qd-warn)]">PAPER</div>
          </div>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1.25fr_0.75fr]">
        <section className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MiniMetric icon={Wallet} label="Capital" value="INR 7.84L" />
            <MiniMetric icon={TrendingUp} label="Day P&L" value="+INR 6,920" tone="good" />
            <MiniMetric icon={Gauge} label="Risk Used" value="18%" tone="warn" />
            <MiniMetric icon={ShieldCheck} label="Protection" value="3/3" tone="good" />
          </div>

          <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface)] p-4">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <div className="qd-section-title">Live Execution Flow</div>
                <h3 className="mt-1 font-head text-base font-bold text-white">Strategy Desk</h3>
              </div>
              <span className="qd-chip">
                <Bot size={12} /> Agent ready
              </span>
            </div>
            <div className="space-y-2">
              {strategies.map((item) => (
                <div key={item.name} className="grid gap-3 rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface-2)] p-3 md:grid-cols-[1fr_auto_auto] md:items-center">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-bold text-white">{item.name}</div>
                    <div className="mt-1 flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-[var(--qd-text-3)]">
                      <Clock3 size={11} /> {item.state}
                    </div>
                  </div>
                  <div className={`font-mono text-sm font-bold ${item.pnl.startsWith("+") ? "text-[var(--qd-profit)]" : "text-[var(--qd-loss)]"}`}>
                    {item.pnl}
                  </div>
                  <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--qd-bg)] md:w-28">
                    <div className="h-full rounded-full bg-[var(--qd-accent)]" style={{ width: `${item.score}%` }} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        <aside className="space-y-4">
          <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface)] p-4">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="flex items-center gap-2 font-head text-sm font-bold text-white">
                <BarChart3 size={15} /> NIFTY Watch
              </h3>
              <span className="qd-live-dot" />
            </div>
            <div className="divide-y divide-[var(--qd-border)]">
              {marketRows.map((row) => (
                <div key={row.symbol} className="flex items-center justify-between py-2.5">
                  <div className="font-mono text-xs font-bold text-white">{row.symbol}</div>
                  <div className="text-right">
                    <div className="font-mono text-xs text-white">{row.price}</div>
                    <div className={`font-mono text-[10px] font-bold ${toneClass[row.tone]}`}>{row.change}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-[var(--qd-radius)] border border-[var(--qd-border)] bg-[var(--qd-surface)] p-4">
            <h3 className="flex items-center gap-2 font-head text-sm font-bold text-white">
              <LineChart size={15} /> Flow Quality
            </h3>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <MiniMetric icon={CheckCircle2} label="Ticks" value="Fresh" tone="good" />
              <MiniMetric icon={Activity} label="Latency" value="214ms" />
              <MiniMetric icon={AlertTriangle} label="Skipped" value="2" tone="warn" />
              <MiniMetric icon={Zap} label="Signals" value="17" tone="good" />
            </div>
          </div>
        </aside>
      </div>
    </article>
  );
}

export default function DesignCanvas() {
  return (
    <div className="space-y-5" data-testid="design-canvas-page">
      <section className="qd-card qd-hero-panel p-5">
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="qd-section-title">Frontend Direction</div>
            <h1 className="mt-2 font-head text-3xl font-extrabold text-white">Design Canvas</h1>
            <p className="mt-2 max-w-3xl text-sm text-[var(--qd-text-2)]">
              Four polished dashboard artboards for the full QuantG redesign: calm, faster to scan, slightly playful, and still serious enough for trading decisions.
            </p>
          </div>
          <span className="qd-chip">
            <Sparkles size={12} /> Shared dashboard component
          </span>
        </div>
      </section>

      <section className="grid grid-cols-1 gap-5 2xl:grid-cols-2">
        {themes.map((theme) => (
          <DashboardArtboard key={theme.id} theme={theme} />
        ))}
      </section>

      <section className="qd-card p-5">
        <div className="qd-section-title">Selection Notes</div>
        <div className="mt-3 grid gap-3 md:grid-cols-4">
          {themes.map((theme) => (
            <div key={theme.id} className="rounded-[var(--qd-radius-sm)] border border-[var(--qd-border)] bg-[var(--qd-surface)] p-3">
              <div className="flex gap-1.5">
                {theme.colors.map((color) => (
                  <span key={color} className="h-5 flex-1 rounded-sm border border-[var(--qd-border)]" style={{ background: color }} />
                ))}
              </div>
              <div className="mt-3 font-head text-sm font-bold text-white">{theme.label}</div>
              <p className="mt-1 text-xs text-[var(--qd-text-2)]">{theme.note}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
