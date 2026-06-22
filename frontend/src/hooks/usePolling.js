import { useEffect, useRef } from "react";

/**
 * Visibility-aware polling.
 *
 * Runs `callback` immediately, then repeatedly. Key properties that keep the
 * app fast and the network calm:
 *   - Recursive setTimeout (not setInterval) → the next run is scheduled only
 *     after the current one settles, so slow responses never pile up.
 *   - Pauses while the tab is hidden (or polls on a slow `hiddenMs` cadence),
 *     then refreshes immediately when the tab becomes visible again.
 *   - Awaits async callbacks before scheduling the next tick.
 *
 * @param {Function} callback  async or sync function to run each tick
 * @param {number}   intervalMs active (visible) cadence
 * @param {object}   [opts]
 * @param {number}   [opts.hiddenMs=0] cadence while hidden; 0 = fully pause
 * @param {boolean}  [opts.enabled=true] when false, polling is stopped
 */
export function usePolling(callback, intervalMs, opts = {}) {
  const { hiddenMs = 0, enabled = true } = opts;
  const savedCallback = useRef(callback);
  savedCallback.current = callback;

  useEffect(() => {
    if (!enabled) return undefined;

    let active = true;
    let timer = null;
    let inFlight = false;

    const run = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        await savedCallback.current();
      } catch {
        /* swallow — callbacks own their own error handling */
      } finally {
        inFlight = false;
      }
    };

    const schedule = () => {
      if (!active) return;
      if (document.hidden) {
        if (hiddenMs > 0) timer = setTimeout(tick, hiddenMs);
        // hiddenMs === 0 → stop; visibilitychange will resume.
        return;
      }
      timer = setTimeout(tick, intervalMs);
    };

    const tick = async () => {
      await run();
      schedule();
    };

    const onVisibility = () => {
      if (document.hidden) return;
      if (timer) clearTimeout(timer);
      tick();
    };

    tick();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      active = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs, hiddenMs, enabled]);
}

export default usePolling;
