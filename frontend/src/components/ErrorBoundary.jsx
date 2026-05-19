import React from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("[QuantG] UI error", error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div className="min-h-[60vh] flex items-center justify-center p-4">
        <div className="qd-card max-w-lg w-full p-6 text-center">
          <AlertTriangle className="mx-auto text-[var(--qd-warn)] mb-3" size={28} />
          <h1 className="font-head text-2xl text-white">This page hit a display error</h1>
          <p className="text-sm text-[var(--qd-text-2)] mt-2">
            Your session is still safe. Refresh the page, or go back to Dashboard.
          </p>
          <pre className="mt-4 text-left text-xs font-mono text-[var(--qd-loss)] bg-black/30 border border-[var(--qd-border)] p-3 overflow-auto">
            {this.state.error?.message || "Unknown UI error"}
          </pre>
          <div className="mt-4 flex gap-2 justify-center">
            <button
              type="button"
              onClick={() => window.location.assign("/dashboard")}
              className="bg-[var(--qd-accent)] text-white px-4 py-2 rounded-sm font-mono text-xs uppercase"
            >
              Dashboard
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="border border-[var(--qd-border)] text-white px-4 py-2 rounded-sm font-mono text-xs uppercase flex items-center gap-2"
            >
              <RefreshCw size={14} /> Refresh
            </button>
          </div>
        </div>
      </div>
    );
  }
}
