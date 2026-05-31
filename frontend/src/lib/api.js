import axios from "axios";

const normalizeApiBase = (value) => {
  if (!value) return null;
  const trimmed = value.trim().replace(/\/+$/, "");
  return trimmed.endsWith("/api") ? trimmed : `${trimmed}/api`;
};

export const API =
  process.env.REACT_APP_API_BASE ||
  normalizeApiBase(process.env.REACT_APP_API_URL) ||
  normalizeApiBase(process.env.REACT_APP_BACKEND_URL) ||
  "/api";

export const api = axios.create({ baseURL: API });

api.interceptors.request.use((cfg) => {
  const t = localStorage.getItem("qd_token");
  if (t) cfg.headers.Authorization = `Bearer ${t}`;
  return cfg;
});

export const formatINR = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "-";
  const num = Number(n);
  return num.toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
};

export const pctFmt = (n) => {
  if (n === null || n === undefined) return "-";
  const v = Number(n);
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
};

export function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e)))
      .filter(Boolean)
      .join(" ");
  }
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
}
