import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null); // null=checking, false=anon, obj=authed
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem("qd_token");
    if (!token) {
      setUser(false);
      setLoading(false);
      return;
    }
    api
      .get("/auth/me")
      .then((r) => setUser(r.data))
      .catch(() => {
        localStorage.removeItem("qd_token");
        setUser(false);
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email, password) => {
    const r = await api.post("/auth/login", { email, password });
    localStorage.setItem("qd_token", r.data.access_token);
    setUser(r.data.user);
    return r.data.user;
  }, []);

  const register = useCallback(async (email, password, name) => {
    const r = await api.post("/auth/register", { email, password, name });
    localStorage.setItem("qd_token", r.data.access_token);
    setUser(r.data.user);
    return r.data.user;
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("qd_token");
    setUser(false);
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, register, logout }),
    [user, loading, login, register, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export const useAuth = () => useContext(AuthContext);
