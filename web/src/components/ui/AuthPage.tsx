import React, { useState, useEffect, useRef } from "react";
import { Sparkles, LogIn, UserPlus, Loader2, Zap, ArrowRight } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api } from "@/api/client";
import type { ApiError } from "@/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface Props {
  onAuth: () => void;
}

type Mode = "login" | "register";

type Translate = (key: string, options?: Record<string, unknown>) => string;

function errMsg(t: Translate, e: unknown): string {
  if (e instanceof TypeError || (e instanceof Error && (
    e.message.toLowerCase().includes("cross-origin") ||
    e.message.toLowerCase().includes("cors") ||
    e.message.toLowerCase().includes("failed to fetch") ||
    e.message.toLowerCase().includes("network")
  ))) {
    return t("errors.networkConnectionFailed");
  }
  if (e && typeof e === "object" && "detail" in e) {
    return (e as ApiError).detail || t("errors.genericActionFailed");
  }
  return t("errors.genericActionFailed");
}

export default function AuthPage({ onAuth }: Props) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<Mode>("login");

  // ── Login fields
  const [loginId, setLoginId] = useState("");   // phone or username
  const [loginPw, setLoginPw] = useState("");

  // ── Register fields
  const [regUsername, setRegUsername] = useState("");
  const [regDisplayName, setRegDisplayName] = useState("");
  const [regPw, setRegPw] = useState("");
  const [regPwConfirm, setRegPwConfirm] = useState("");

  // ── Shared state
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [apiReachable, setApiReachable] = useState<boolean | null>(null);
  const [retryAfter, setRetryAfter] = useState(0);
  const failCountRef = useRef(0);

  useEffect(() => {
    api.health()
      .then(() => setApiReachable(true))
      .catch(() => setApiReachable(false));
  }, []);

  // ── Retry-after ticker
  useEffect(() => {
    if (retryAfter <= 0) return;
    const t = setTimeout(() => setRetryAfter((v) => v - 1), 1000);
    return () => clearTimeout(t);
  }, [retryAfter]);

  const switchMode = (m: Mode) => {
    setMode(m);
    setError(null);
    setRetryAfter(0);
    failCountRef.current = 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (retryAfter > 0 || loading) return;
    setError(null);

    if (mode === "register") {
      if (regUsername.trim().length < 2) { setError(t("components.authPage.errors.usernameTooShort")); return; }
      if (regPw.length < 8) { setError(t("components.authPage.errors.passwordTooShort")); return; }
      if (regPw !== regPwConfirm) { setError(t("components.authPage.errors.passwordsMismatch")); return; }
    }

    setLoading(true);

    const doRequest = () => mode === "login"
      ? api.login(loginId.trim(), loginPw)
      : api.register(regUsername.trim(), regPw, regDisplayName.trim() || regUsername.trim());

    const isNetworkError = (e: unknown) =>
      e instanceof TypeError || (e instanceof Error && (
        e.message.toLowerCase().includes("cross-origin") ||
        e.message.toLowerCase().includes("cors") ||
        e.message.toLowerCase().includes("failed to fetch") ||
        e.message.toLowerCase().includes("network")
      ));

    try {
      let res;
      try {
        res = await doRequest();
      } catch (e1) {
        if (isNetworkError(e1)) {
          await new Promise(r => setTimeout(r, 800));
          res = await doRequest();
        } else {
          throw e1;
        }
      }
      api.saveUser({
        username: res.username,
        display_name: res.display_name,
        is_admin: res.is_admin,
      });
      onAuth();
    } catch (e) {
      setError(errMsg(t, e));
      if (mode === "login") {
        failCountRef.current += 1;
        const delay = Math.min(Math.pow(2, failCountRef.current - 1), 16);
        setRetryAfter(delay);
      }
    } finally {
      setLoading(false);
    }
  };

  const loginValid = loginId.trim().length >= 3 && loginPw.length >= 1;
  const registerValid =
    regUsername.trim().length >= 2 &&
    regPw.length >= 8 &&
    regPw === regPwConfirm;
  const canSubmit = (mode === "login" ? loginValid : registerValid) && retryAfter === 0 && !loading;
  const apiStatusText = apiReachable === null
    ? t("components.authPage.apiStatus.checking")
    : apiReachable
      ? t("components.authPage.apiStatus.healthy")
      : t("components.authPage.apiStatus.unavailable");

  return (
    <div className="relative min-h-screen flex flex-col items-center justify-center px-4 overflow-hidden">
      {/* ── Ambient background effects ── */}
      <div className="pointer-events-none absolute inset-0">
        {/* Top-center oracle glow */}
        <div className="absolute top-[-20%] left-1/2 -translate-x-1/2 w-[600px] h-[600px] rounded-full bg-oracle-500/[0.04] blur-[120px]" />
        {/* Bottom-left subtle accent */}
        <div className="absolute bottom-[-10%] left-[-10%] w-[400px] h-[400px] rounded-full bg-oracle-600/[0.03] blur-[100px]" />
        {/* Grid pattern overlay */}
        <div
          className="absolute inset-0 opacity-[0.015]"
          style={{
            backgroundImage: `linear-gradient(rgba(255,255,255,0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.1) 1px, transparent 1px)`,
            backgroundSize: "64px 64px",
          }}
        />
      </div>

      {/* ── Brand header ── */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 mb-10 text-center"
      >
        {/* Logo mark */}
        <div className="relative w-16 h-16 mx-auto mb-6">
          <div className="absolute inset-0 rounded-2xl bg-gradient-to-br from-oracle-400 to-oracle-600 blur-xl opacity-40" />
          <div className="relative w-full h-full rounded-2xl bg-gradient-to-br from-oracle-400 to-oracle-600 flex items-center justify-center shadow-2xl shadow-oracle-500/20">
            <Sparkles size={28} className="text-surface-0" />
          </div>
        </div>

        <h1 className="font-display text-5xl sm:text-6xl text-zinc-50 tracking-tight">
          <span className="text-gradient italic">Synthora</span>
        </h1>
        <p className="text-zinc-500 text-base mt-3 font-light tracking-wide">
          {t("components.authPage.brandTagline")}
        </p>
      </motion.div>

      {/* ── Auth card ── */}
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, delay: 0.15, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 w-full max-w-[400px]"
      >
        {/* Card glow ring */}
        <div className="absolute -inset-px rounded-2xl bg-gradient-to-b from-oracle-500/20 via-oracle-500/5 to-transparent" />
        <div className="absolute -inset-[1px] rounded-2xl bg-gradient-to-b from-surface-4/80 to-surface-4/20" />

        {/* Card body */}
        <div className="relative rounded-2xl bg-surface-1/90 backdrop-blur-2xl p-8">

          {/* Tab switcher */}
          <div className="flex rounded-xl bg-surface-0/60 p-1 mb-6">
            {(["login", "register"] as Mode[]).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => switchMode(m)}
                className={cn(
                  "relative flex-1 py-2.5 text-sm font-medium rounded-lg transition-all duration-200",
                  mode === m
                    ? "text-surface-0"
                    : "text-zinc-500 hover:text-zinc-300"
                )}
              >
                {mode === m && (
                  <motion.div
                    layoutId="auth-tab"
                    className="absolute inset-0 rounded-lg bg-oracle-500"
                    transition={{ type: "spring", bounce: 0.15, duration: 0.5 }}
                  />
                )}
                <span className="relative z-10">{m === "login" ? t("components.authPage.tabs.login") : t("components.authPage.tabs.registerTrial")}</span>
              </button>
            ))}
          </div>

          {/* Register credits hint */}
          <AnimatePresence>
            {mode === "register" && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="mb-5 flex items-center gap-2.5 px-3.5 py-2.5 rounded-xl bg-oracle-500/[0.06] border border-oracle-500/15 text-xs text-oracle-400">
                  <Zap size={13} className="shrink-0 text-oracle-500" />
                  <span>{t("components.authPage.registerCreditsHint.prefix")} <strong className="text-oracle-300">{t("components.authPage.registerCreditsHint.credits")}</strong>{t("components.authPage.registerCreditsHint.suffix")}</span>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Form */}
          <form onSubmit={handleSubmit} className="space-y-4">
            <AnimatePresence mode="wait">
              {mode === "login" ? (
                <motion.div
                  key="login"
                  initial={{ opacity: 0, x: -12 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: 12 }}
                  transition={{ duration: 0.2 }}
                  className="space-y-4"
                >
                  <div className="space-y-2">
                    <Label htmlFor="loginId">{t("components.authPage.fields.loginId.label")}</Label>
                    <Input
                      id="loginId"
                      type="text"
                      value={loginId}
                      onChange={(e) => setLoginId(e.target.value)}
                      placeholder={t("components.authPage.fields.loginId.placeholder")}
                      maxLength={32}
                      autoFocus
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="loginPw">{t("components.authPage.fields.loginPassword.label")}</Label>
                    <Input
                      id="loginPw"
                      type="password"
                      value={loginPw}
                      onChange={(e) => setLoginPw(e.target.value)}
                      placeholder={t("components.authPage.fields.loginPassword.placeholder")}
                      disabled={loading}
                    />
                  </div>
                </motion.div>
              ) : (
                <motion.div
                  key="register"
                  initial={{ opacity: 0, x: 12 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -12 }}
                  transition={{ duration: 0.2 }}
                  className="space-y-4"
                >
                  <div className="space-y-2">
                    <Label htmlFor="regUsername">
                      {t("components.authPage.fields.registerUsername.label")} <span className="text-zinc-600 text-xs font-normal">{t("components.authPage.fields.registerUsername.hint")}</span>
                    </Label>
                    <Input
                      id="regUsername"
                      type="text"
                      value={regUsername}
                      onChange={(e) => setRegUsername(e.target.value.replace(/[^\w\u4e00-\u9fa5]/g, ""))}
                      placeholder={t("components.authPage.fields.registerUsername.placeholder")}
                      maxLength={30}
                      autoFocus
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="regDisplayName">
                      {t("components.authPage.fields.registerDisplayName.label")} <span className="text-zinc-600 text-xs font-normal">{t("components.authPage.fields.registerDisplayName.hint")}</span>
                    </Label>
                    <Input
                      id="regDisplayName"
                      type="text"
                      value={regDisplayName}
                      onChange={(e) => setRegDisplayName(e.target.value)}
                      placeholder={t("components.authPage.fields.registerDisplayName.placeholder")}
                      maxLength={30}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="regPw">
                      {t("components.authPage.fields.registerPassword.label")} <span className="text-zinc-600 text-xs font-normal">{t("components.authPage.fields.registerPassword.hint")}</span>
                    </Label>
                    <Input
                      id="regPw"
                      type="password"
                      value={regPw}
                      onChange={(e) => setRegPw(e.target.value)}
                      placeholder={t("components.authPage.fields.registerPassword.placeholder")}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="regPwConfirm">{t("components.authPage.fields.registerPasswordConfirm.label")}</Label>
                    <Input
                      id="regPwConfirm"
                      type="password"
                      value={regPwConfirm}
                      onChange={(e) => setRegPwConfirm(e.target.value)}
                      placeholder={t("components.authPage.fields.registerPasswordConfirm.placeholder")}
                      disabled={loading}
                    />
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Error message */}
            <AnimatePresence>
              {error && (
                <motion.div
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  className="p-3 rounded-xl bg-red-500/[0.08] border border-red-500/20 text-red-400 text-sm"
                >
                  {error}
                </motion.div>
              )}
            </AnimatePresence>

            {/* Submit */}
            <Button
              type="submit"
              disabled={!canSubmit}
              variant="oracle"
              size="lg"
              className="w-full mt-2"
            >
              {loading ? (
                <Loader2 size={18} className="animate-spin" />
              ) : mode === "login" ? (
                <LogIn size={18} />
              ) : (
                <UserPlus size={18} />
              )}
              {loading
                ? t("components.authPage.submit.processing")
                : retryAfter > 0
                ? t("components.authPage.submit.retryAfter", { seconds: retryAfter })
                : mode === "login"
                ? t("components.authPage.submit.login")
                : t("components.authPage.submit.registerReward")}
              {!loading && retryAfter === 0 && (
                <ArrowRight size={16} className="ml-1 opacity-60" />
              )}
            </Button>
          </form>

          <div className="mt-4 text-center">
            <Link
              to="/privacy"
              className="text-sm text-zinc-400 underline underline-offset-4 transition-colors hover:text-zinc-200"
            >
              {t("components.authPage.privacyPolicy")}
            </Link>
          </div>
        </div>
      </motion.div>

      {/* ── Footer ── */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.4, duration: 0.6 }}
        className="relative z-10 mt-8 text-center"
      >
        <p className="text-xs text-zinc-600 max-w-[280px] mx-auto leading-relaxed">
          {t("components.authPage.footer")}
        </p>
        <div className="mt-3 flex items-center justify-center gap-1.5 text-[11px] text-zinc-700">
          <span className={cn(
            "inline-block w-1.5 h-1.5 rounded-full",
            apiReachable ? "bg-emerald-500" : "bg-red-500"
          )} />
          {apiStatusText}
        </div>
      </motion.div>
    </div>
  );
}
