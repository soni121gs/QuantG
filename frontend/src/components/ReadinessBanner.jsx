import React, { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { usePolling } from "../hooks/usePolling";
import { AlertTriangle, X, KeyRound, Play } from "lucide-react";

// Surfaces the two recurring "why isn't it trading?" blockers:
//  - Upstox token disconnected/expired (expires ~3:30 IST daily, no refresh)
//  - No strategies armed (live)
// Hidden entirely when both are satisfied. Dismissible for the session.
export default function ReadinessBanner() {
  const [connected, setConnected] = useState(true);
  const [feedStalled, setFeedStalled] = useState(false);
  const [liveCount, setLiveCount] = useState(null);
  const [dismissed, setDismissed] = useState(false);

  const load = useCallback(async () => {
    const [u, s] = await Promise.all([
      api.get("/upstox/status").catch(() => ({ data: { connected: false, feed_stalled: false } })),
      api.get("/strategies").catch(() => ({ data: [] })),
    ]);
    setConnected(!!u.data?.connected);
    setFeedStalled(!!u.data?.feed_stalled);
    const list = Array.isArray(s.data) ? s.data : s.data?.strategies || [];
    setLiveCount(list.filter((x) => x.status === "live").length);
  }, []);
  usePolling(load, 60000, { hiddenMs: 0 });

  if (dismissed || liveCount === null) return null;
  const needsToken = !connected;
  const needsFeed = feedStalled;
  const needsArm = liveCount === 0;
  if (!needsToken && !needsFeed && !needsArm) return null;

  return (
    <div
      className="mb-4 flex items-start gap-3 rounded-[var(--qd-radius)] border border-[var(--qd-border)] border-l-4 border-l-[var(--qd-warn)] bg-[color-mix(in_srgb,var(--qd-warn)_8%,var(--qd-surface))] p-3"
      data-testid="readiness-banner"
    >
      <AlertTriangle size={18} className="mt-0.5 shrink-0 text-[var(--qd-warn)]" />
      <div className="min-w-0 flex-1">
        <div className="font-head text-sm font-bold text-[var(--qd-text)]">Not ready to trade</div>
        <div className="mt-1 space-y-1 text-xs text-[var(--qd-text-2)]">
          {needsToken && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>Upstox token expired — reconnect.</span>
              <Link to="/broker-keys" className="inline-flex items-center gap-1 font-semibold text-[var(--qd-accent)] hover:underline">
                <KeyRound size={12} /> Reconnect
              </Link>
            </div>
          )}
          {needsFeed && !needsToken && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>Live feed stalled — 0 ticks.</span>
              <Link to="/ops" className="inline-flex items-center gap-1 font-semibold text-[var(--qd-accent)] hover:underline">
                <AlertTriangle size={12} /> View status
              </Link>
            </div>
          )}
          {needsArm && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>No strategies are armed (0 live) — nothing will trade even with a feed.</span>
              <Link to="/strategies" className="inline-flex items-center gap-1 font-semibold text-[var(--qd-accent)] hover:underline">
                <Play size={12} /> Arm strategies
              </Link>
            </div>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        className="rounded p-1 text-[var(--qd-text-3)] hover:bg-[var(--qd-surface-2)] hover:text-[var(--qd-text)]"
        aria-label="Dismiss"
        data-testid="readiness-dismiss"
      >
        <X size={15} />
      </button>
    </div>
  );
}
