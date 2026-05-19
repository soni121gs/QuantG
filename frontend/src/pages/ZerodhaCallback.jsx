import React, { useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { toast } from "sonner";

export default function ZerodhaCallback() {
  const [params] = useSearchParams();
  const navigate = useNavigate();

  useEffect(() => {
    const requestToken = params.get("request_token");
    const status = params.get("status");

    if (status === "success" && requestToken) {
      // Exchange request_token for access_token via backend
      api
        .post("/zerodha/exchange", { request_token: requestToken })
        .then((r) => {
          toast.success(`✓ Connected to Zerodha as ${r.data.kite_user_id}`);
          // Redirect back to broker-keys with success indicator
          navigate("/broker-keys?zerodha=connected", { replace: true });
        })
        .catch((err) => {
          toast.error(
            err.response?.data?.detail || "Failed to exchange Zerodha token"
          );
          navigate("/broker-keys?zerodha=failed", { replace: true });
        });
    } else if (status === "failed") {
      toast.error("Zerodha login was cancelled or failed");
      navigate("/broker-keys?zerodha=failed", { replace: true });
    } else {
      // No valid callback params — redirect to broker-keys
      navigate("/broker-keys", { replace: true });
    }
  }, [params, navigate]);

  return (
    <div className="min-h-screen bg-[var(--qd-bg)] flex items-center justify-center font-mono">
      <div className="text-center">
        <div className="animate-spin mb-4">⟳</div>
        <p className="text-[var(--qd-text-2)]">Processing Zerodha authentication...</p>
      </div>
    </div>
  );
}
