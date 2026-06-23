import React, { Suspense } from "react";
import "@/index.css";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { ExecutionStateProvider } from "./contexts/ExecutionStateContext";
import Layout from "./components/Layout";
import InstallPWA from "./components/InstallPWA";
import ErrorBoundary from "./components/ErrorBoundary";
import { Toaster } from "sonner";

// Lazy-loaded pages to optimize bundle size
const Auth = React.lazy(() => import("./pages/Auth"));
const Dashboard = React.lazy(() => import("./pages/Dashboard"));
const ApiKeys = React.lazy(() => import("./pages/ApiKeys"));
const Strategies = React.lazy(() => import("./pages/Strategies"));
const PythonEditor = React.lazy(() => import("./pages/PythonEditor"));
const VisualBuilder = React.lazy(() => import("./pages/VisualBuilder"));
const AIBot = React.lazy(() => import("./pages/AIBot"));
const Orders = React.lazy(() => import("./pages/Orders"));
const Profile = React.lazy(() => import("./pages/Profile"));
const OpsConsole = React.lazy(() => import("./pages/OpsConsole"));
const MarketHub = React.lazy(() => import("./pages/MarketHub"));
const Calendar = React.lazy(() => import("./pages/Calendar"));
const Wiki = React.lazy(() => import("./pages/Wiki"));
const Analytics = React.lazy(() => import("./pages/Analytics"));

const Protected = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return <div className="min-h-screen bg-[var(--qd-bg)] flex items-center justify-center text-[var(--qd-text-2)] font-mono text-xs">Loading...</div>;
  if (!user) return <Navigate to="/login" replace />;
  return <Layout><ErrorBoundary>{children}</ErrorBoundary></Layout>;
};

const PublicOnly = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (user) return <Navigate to="/dashboard" replace />;
  return children;
};

const LazySpinner = () => (
  <div className="min-h-screen bg-[var(--qd-bg)] flex flex-col items-center justify-center text-[var(--qd-text-2)] font-mono text-xs gap-3">
    <div className="w-6 h-6 border-2 border-[var(--qd-accent)] border-t-transparent rounded-full animate-spin"></div>
    <span>Loading resource...</span>
  </div>
);

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
        <ExecutionStateProvider>
          <Toaster theme="system" position="top-right" />
          <Suspense fallback={<LazySpinner />}>
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
            <Route path="/positions" element={<Protected><Orders /></Protected>} />
            <Route path="/analytics" element={<Protected><Analytics /></Protected>} />
            <Route path="/market-hub" element={<Protected><MarketHub /></Protected>} />
            <Route path="/broker-keys" element={<Protected><ApiKeys /></Protected>} />
            <Route path="/profile" element={<Protected><Profile /></Protected>} />
            <Route path="/ops" element={<Protected><OpsConsole /></Protected>} />
            <Route path="/calendar" element={<Protected><Calendar /></Protected>} />
            <Route path="/wiki" element={<Protected><Wiki /></Protected>} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </Suspense>
        </ExecutionStateProvider>
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
