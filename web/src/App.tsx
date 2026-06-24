import { useState, useEffect, useCallback, lazy, Suspense } from "react";
import { createBrowserRouter, Navigate, RouterProvider, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import Layout from "./components/layout/Layout";
import AuthPage from "./components/ui/AuthPage";
import ErrorBoundary from "./components/ErrorBoundary";
import LoadingOracle from "./components/ui/LoadingOracle";
import ConsentBanner, { useConsentBanner } from "./components/ui/ConsentBanner";
import { persistClientLanguage, readStoredLanguage } from "./i18n/language";
import PrivacyPage from "./pages/PrivacyPage";
import { api } from "./api/client";

const HomePage = lazy(() => import("./pages/HomePage"));
const RoundtablePage = lazy(() => import("./pages/RoundtablePage"));
const SocraticPage = lazy(() => import("./pages/SocraticPage"));
const HistoryPage = lazy(() => import("./pages/HistoryPage"));
const CapabilityMapPage = lazy(() => import("./pages/CapabilityMapPage"));
const ProfilePage = lazy(() => import("./pages/ProfilePage"));

function getApiErrorStatus(err: unknown): number | null {
  if (!err || typeof err !== "object") return null;
  const status = (err as { status?: unknown }).status;
  return typeof status === "number" ? status : null;
}

function RouteLoadingFallback() {
  return (
    <div className="flex items-center justify-center min-h-screen bg-surface-0">
      <LoadingOracle />
    </div>
  );
}

function BackendUnreachable() {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-surface-0 gap-4">
      <div className="w-12 h-12 rounded-full bg-amber-500/10 flex items-center justify-center">
        <span className="text-2xl">{"\u26A0\uFE0F"}</span>
      </div>
      <div className="text-center">
        <div className="text-base font-semibold text-zinc-200 mb-1">
          {t("common.app.backendUnavailable.title")}
        </div>
        <div className="text-sm text-zinc-500">{t("common.app.backendUnavailable.detail")}</div>
      </div>
      <button
        onClick={() => window.location.reload()}
        className="px-4 py-2 rounded-lg bg-oracle-500/15 text-oracle-300 border border-oracle-500/30 text-sm hover:bg-oracle-500/25 transition-colors"
      >
        {t("common.actions.refreshRetry")}
      </button>
    </div>
  );
}

/**
 * AuthShell — the single route element for "/".
 *
 * When NOT authed  → renders AuthPage (the URL stays as-is, e.g. "/history").
 * When authed      → renders Layout (which contains <Outlet/> → child routes resolve naturally).
 *
 * Because the router is never recreated, navigating to /history while logged-out
 * keeps the URL intact. After login the shell re-renders Layout and the router
 * resolves /history to HistoryPage automatically — no sessionStorage tricks needed.
 */
function AuthShell() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [authed, setAuthed] = useState(api.isLoggedIn());
  const [checking, setChecking] = useState(api.isLoggedIn());
  const [backendUnreachable, setBackendUnreachable] = useState(false);

  // ?_clear=1: startup script cleans stale cookies then redirects to clean URL
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("_clear") === "1" && api.isLoggedIn()) {
      localStorage.clear();
      api.logout().catch(() => {});
      window.location.replace("/");
    }
  }, []);

  // Validate saved token against backend on mount
  useEffect(() => {
    if (!api.isLoggedIn()) {
      setChecking(false);
      return;
    }
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled) return;
      setBackendUnreachable(true);
      setChecking(false);
    }, 5000);

    api.me()
      .then((user) => {
        settled = true;
        clearTimeout(timeout);
        if (!readStoredLanguage() && user.preferred_language) {
          persistClientLanguage(user.preferred_language);
        }
        setAuthed(true);
        setBackendUnreachable(false);
        setChecking(false);
      })
      .catch((err: unknown) => {
        settled = true;
        clearTimeout(timeout);
        const status = getApiErrorStatus(err);
        if (err instanceof TypeError) {
          setBackendUnreachable(true);
          setChecking(false);
        } else if (status === 401) {
          setBackendUnreachable(false);
          setAuthed(false);
          setChecking(false);
        } else {
          setBackendUnreachable(false);
          localStorage.removeItem("synthora_user");
          setAuthed(false);
          setChecking(false);
        }
      });
    return () => {
      settled = true;
      clearTimeout(timeout);
    };
  }, []);

  // F-06: force-logout event from API client
  useEffect(() => {
    const handleForceLogout = () => setAuthed(false);
    window.addEventListener("synthora:force-logout", handleForceLogout);
    return () =>
      window.removeEventListener("synthora:force-logout", handleForceLogout);
  }, []);

  // Dev shortcut: Cmd+Shift+C to clear localStorage and reload
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key === "C") {
        e.preventDefault();
        if (confirm(t("common.app.clearLocalStorageConfirm"))) {
          localStorage.clear();
          window.location.href = "/";
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const handleLogout = useCallback(async () => {
    await api.logout();
    setAuthed(false);
    navigate("/", { replace: true });
  }, [navigate]);

  const handleAuth = useCallback(() => {
    setAuthed(true);
  }, []);

  if (checking) return <RouteLoadingFallback />;
  if (backendUnreachable) return <BackendUnreachable />;
  if (!authed) return <AuthPage onAuth={handleAuth} />;
  return <Layout onLogout={handleLogout} />;
}

// --- Router: created ONCE at module level, never recreated ---
const router = createBrowserRouter([
  { path: "privacy", element: <PrivacyPage /> },
  {
    path: "/",
    element: <AuthShell />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "history", element: <HistoryPage /> },
      { path: "profile", element: <ProfilePage /> },
      { path: "capability-map", element: <CapabilityMapPage /> },
      { path: "socratic/:sessionId", element: <SocraticPage /> },
      { path: "roundtable", element: <RoundtablePage /> },
    ],
  },
  { path: "*", element: <Navigate to="/" replace /> },
]);

export default function App() {
  const { show: showConsent, accept: acceptConsent } = useConsentBanner();

  return (
    <ErrorBoundary>
      <Suspense fallback={<RouteLoadingFallback />}>
        <RouterProvider router={router} />
      </Suspense>
      {showConsent && <ConsentBanner onAccept={acceptConsent} />}
    </ErrorBoundary>
  );
}
