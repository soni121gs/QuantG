import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null); // null=checking, false=anon, obj=authed
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    // Debug log
    console.log("[AUTH] Initializing...");
    console.log("[AUTH] API baseURL:", api.defaults.baseURL);
    console.log("[AUTH] REACT_APP_BACKEND_URL:", process.env.REACT_APP_BACKEND_URL);
    
    const token = localStorage.getItem("qd_token");
    console.log("[AUTH] Token from localStorage:", token ? "exists" : "not found");
    
    if (!token) {
      console.log("[AUTH] No token, user is anonymous");
      setUser(false);
      setLoading(false);
      return;
    }
    
    console.log("[AUTH] Token found, validating...");
    api
      .get("/auth/me")
      .then((r) => {
        console.log("[AUTH] Validated token, user:", r.data.email);
        setUser(r.data);
        setError(null);
      })
      .catch((err) => {
        console.error("[AUTH] Token validation failed:", err.response?.status, err.message);
        localStorage.removeItem("qd_token");
        setUser(false);
        setError(err.message);
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email, password) => {
    console.log("[AUTH] Attempting login:", email);
    setError(null);
    try {
      const r = await api.post("/auth/login", { email, password });
      console.log("[AUTH] Login successful, token received");
      localStorage.setItem("qd_token", r.data.access_token);
      setUser(r.data.user);
      return r.data.user;
    } catch (err) {
      const errMsg = err.response?.data?.detail || err.message || "Login failed";
      console.error("[AUTH] Login error:", errMsg);
      setError(errMsg);
      throw err;
    }
  }, []);

  const register = useCallback(async (email, password, name) => {
    console.log("[AUTH] Attempting register:", email);
    setError(null);
    try {
      const r = await api.post("/auth/register", { email, password, name });
      console.log("[AUTH] Register successful, token received");
      localStorage.setItem("qd_token", r.data.access_token);
      setUser(r.data.user);
      return r.data.user;
    } catch (err) {
      const errMsg = err.response?.data?.detail || err.message || "Register failed";
      console.error("[AUTH] Register error:", errMsg);
      setError(errMsg);
      throw err;
    }
  }, []);

  const logout = useCallback(() => {
    console.log("[AUTH] Logging out");
    localStorage.removeItem("qd_token");
    setUser(false);
    setError(null);
  }, []);

  const value = useMemo(
    () => ({ user, loading, error, login, register, logout }),
    [user, loading, error, login, register, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    console.warn("[AUTH] useAuth called outside AuthProvider");
  }
  return ctx;
};
