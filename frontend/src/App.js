import React from "react";
import "@/index.css";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import Layout from "./components/Layout";
import Auth from "./pages/Auth";
import Dashboard from "./pages/Dashboard";
import ApiKeys from "./pages/ApiKeys";
import Strategies from "./pages/Strategies";
import PythonEditor from "./pages/PythonEditor";
import VisualBuilder from "./pages/VisualBuilder";
import AIBot from "./pages/AIBot";
import Orders from "./pages/Orders";
import Positions from "./pages/Positions";
import { Toaster } from "sonner";

const Protected = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return <div className="min-h-screen bg-[var(--qd-bg)] flex items-center justify-center text-[var(--qd-text-2)] font-mono text-xs">Loading...</div>;
  if (!user) return <Navigate to="/login" replace />;
  return <Layout>{children}</Layout>;
};

const PublicOnly = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (user) return <Navigate to="/dashboard" replace />;
  return children;
};

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Toaster theme="dark" position="top-right" />
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/login" element={<PublicOnly><Auth mode="login" /></PublicOnly>} />
          <Route path="/signup" element={<PublicOnly><Auth mode="register" /></PublicOnly>} />
          <Route path="/dashboard" element={<Protected><Dashboard /></Protected>} />
          <Route path="/strategies" element={<Protected><Strategies /></Protected>} />
          <Route path="/python" element={<Protected><PythonEditor /></Protected>} />
          <Route path="/visual" element={<Protected><VisualBuilder /></Protected>} />
          <Route path="/ai-bot" element={<Protected><AIBot /></Protected>} />
          <Route path="/orders" element={<Protected><Orders /></Protected>} />
          <Route path="/positions" element={<Protected><Positions /></Protected>} />
          <Route path="/api-keys" element={<Protected><ApiKeys /></Protected>} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
