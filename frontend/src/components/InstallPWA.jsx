import React, { useEffect, useState } from "react";
import { Download, X } from "lucide-react";

/**
 * Listens for the PWA `beforeinstallprompt` event and shows a small floating
 * banner with an Install button. Once installed (or dismissed), it hides.
 */
export default function InstallPWA() {
  const [evt, setEvt] = useState(null);
  const [dismissed, setDismissed] = useState(() => localStorage.getItem("qd_install_dismissed") === "1");

  useEffect(() => {
    const onPrompt = (e) => {
      e.preventDefault();
      setEvt(e);
    };
    const onInstalled = () => {
      setEvt(null);
      localStorage.setItem("qd_install_dismissed", "1");
    };
    window.addEventListener("beforeinstallprompt", onPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  if (!evt || dismissed) return null;

  const install = async () => {
    try {
      evt.prompt();
      const choice = await evt.userChoice;
      if (choice.outcome === "dismissed") {
        localStorage.setItem("qd_install_dismissed", "1");
        setDismissed(true);
      }
      setEvt(null);
    } catch {
      setEvt(null);
    }
  };

  const dismiss = () => {
    localStorage.setItem("qd_install_dismissed", "1");
    setDismissed(true);
  };

  return (
    <div
      className="fixed bottom-20 lg:bottom-6 inset-x-3 lg:inset-x-auto lg:right-6 z-[70] max-w-sm bg-[var(--qd-surface)] border border-[var(--qd-accent)] rounded-sm p-3 flex items-center gap-3 shadow-xl"
      data-testid="install-pwa-banner"
    >
      <div className="w-9 h-9 bg-[var(--qd-accent)] flex items-center justify-center rounded-sm flex-shrink-0">
        <Download size={18} className="text-white" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-head text-sm text-white">Install QuantG</div>
        <div className="font-mono text-[10px] text-[var(--qd-text-2)] uppercase tracking-wider">Use as an app, even offline</div>
      </div>
      <button
        onClick={install}
        className="bg-[var(--qd-accent)] hover:bg-[var(--qd-accent-hover)] text-white px-3 py-1.5 text-xs font-mono uppercase tracking-wider rounded-sm"
        data-testid="install-pwa-btn"
      >
        Install
      </button>
      <button onClick={dismiss} className="text-[var(--qd-text-3)] hover:text-[var(--qd-text)] p-1" aria-label="Dismiss" data-testid="install-pwa-dismiss">
        <X size={14} />
      </button>
    </div>
  );
}
