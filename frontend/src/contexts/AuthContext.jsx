import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null); // null=checking, false=anon, obj=authed
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const token = localStorage.getItem("qd_token");
    
    if (!token) {
      setUser(false);
      setLoading(false);
      return;
    }
    
    api
      .get("/auth/me")
      .then((r) => {
        setUser(r.data);
        setError(null);
      })
      .catch((err) => {
        localStorage.removeItem("qd_token");
        setUser(false);
        setError(err.message);
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email, password) => {
    setError(null);
    try {
      const r = await api.post("/auth/login", { email, password });
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
    setError(null);
    try {
      const r = await api.post("/auth/register", { email, password, name });
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
  return ctx;
};
