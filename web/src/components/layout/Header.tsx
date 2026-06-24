import { Link, useLocation } from "react-router-dom";
import { Sparkles, LogOut, User, History, Menu, X } from "lucide-react";
import { useState, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslation } from "react-i18next";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";
import type { HealthResponse } from "@/types";

interface Props {
  onLogout?: () => void;
}

function isConversationStoreDegraded(health: HealthResponse | null): boolean {
  return Boolean(health && health.conversation_store && health.conversation_store !== "ok");
}

export default function Header({ onLogout }: Props) {
  const location = useLocation();
  const { t } = useTranslation();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const savedUser = api.getSavedUser();
  const conversationStoreDegraded = isConversationStoreDegraded(health);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
  }, []);

  // Close menu on route change
  useEffect(() => { setMenuOpen(false); }, [location.pathname]);

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen]);

  const navItems = [
    { to: "/history", icon: History, label: t("components.header.nav.history") },
  ];

  return (
    <header ref={menuRef} className="sticky top-0 z-50 border-b border-white/[0.04] bg-surface-0/80 backdrop-blur-xl">
      <div className="max-w-5xl mx-auto px-3 sm:px-4 h-12 flex items-center justify-between gap-4">
        {/* Logo */}
        <Link to="/" className="flex items-center gap-2 group shrink-0" onClick={() => setMenuOpen(false)}>
          <div className="relative w-6 h-6 rounded-lg bg-gradient-to-br from-oracle-400 to-oracle-600 flex items-center justify-center shadow-lg shadow-oracle-500/20 transition-transform group-hover:scale-110">
            <Sparkles size={13} className="text-surface-0" />
          </div>
          <span className="font-display text-sm font-semibold text-zinc-100 tracking-wide">Synthora</span>
        </Link>

        {/* Desktop nav */}
        <div className="hidden md:flex items-center gap-1 flex-1 justify-center">
          {navItems.map(item => (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                "relative flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[13px] font-medium transition-all duration-150",
                location.pathname === item.to
                  ? "text-zinc-100 bg-white/[0.07]"
                  : "text-zinc-400 hover:text-zinc-300 hover:bg-white/[0.04]"
              )}
            >
              {location.pathname === item.to && (
                <motion.div
                  layoutId="nav-indicator"
                  className="absolute inset-0 rounded-lg bg-white/[0.07]"
                  transition={{ type: "spring", bounce: 0.2, duration: 0.4 }}
                />
              )}
              <item.icon size={13} className="relative z-10" />
              <span className="relative z-10">{item.label}</span>
            </Link>
          ))}
        </div>

        {/* Desktop right: status + user + logout */}
        <div className="hidden md:flex items-center gap-2 shrink-0">
          {health && (
            <div
              className={cn(
                "flex items-center gap-1.5 text-[13px]",
                conversationStoreDegraded ? "text-amber-300" : "text-zinc-400"
              )}
            >
              <span
                className={cn(
                  "w-1.5 h-1.5 rounded-full shadow-sm",
                  conversationStoreDegraded
                    ? "bg-amber-400 shadow-amber-400/50"
                    : "bg-emerald-500 shadow-emerald-500/50"
                )}
              />
              {conversationStoreDegraded && <span>{t("components.header.status.historyNotSaved")}</span>}
            </div>
          )}
          {savedUser && (
            <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white/[0.04] border border-white/[0.06] text-[13px] text-zinc-300">
              <User size={11} />
              <span className="max-w-[100px] truncate">{savedUser.display_name}</span>
            </div>
          )}
          {onLogout && (
            <button
              onClick={onLogout}
              className="flex items-center justify-center w-8 h-8 rounded-lg text-zinc-500 hover:text-red-400 hover:bg-red-500/[0.08] transition-all duration-150"
              title={t("components.header.actions.logout")}
            >
              <LogOut size={14} />
            </button>
          )}
        </div>

        {/* Mobile: hamburger */}
        <button
          onClick={() => setMenuOpen(!menuOpen)}
          className="md:hidden flex items-center justify-center w-8 h-8 rounded-lg text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.06] transition-colors active:scale-95"
          aria-label={menuOpen ? t("components.header.actions.closeMenu") : t("components.header.actions.openMenu")}
        >
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={menuOpen ? "close" : "open"}
              initial={{ opacity: 0, rotate: -90 }}
              animate={{ opacity: 1, rotate: 0 }}
              exit={{ opacity: 0, rotate: 90 }}
              transition={{ duration: 0.15 }}
            >
              {menuOpen ? <X size={16} /> : <Menu size={16} />}
            </motion.div>
          </AnimatePresence>
        </button>
      </div>

      {/* Mobile dropdown menu */}
      <AnimatePresence>
        {menuOpen && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="md:hidden border-t border-white/[0.04] bg-surface-0/98 backdrop-blur-xl"
          >
            <nav className="px-3 py-2 space-y-0.5">
              {navItems.map(item => (
                <Link
                  key={item.to}
                  to={item.to}
                  className={cn(
                    "flex items-center gap-3 px-3 py-3 rounded-xl text-sm transition-colors active:scale-[0.98]",
                    location.pathname === item.to
                      ? "text-oracle-400 bg-oracle-500/[0.08]"
                      : "text-zinc-200 hover:bg-white/[0.04]"
                  )}
                >
                  <item.icon size={16} />
                  <span>{item.label}</span>
                </Link>
              ))}
            </nav>
            <div className="border-t border-white/[0.04] px-3 py-2">
              {savedUser && (
                <div className="flex items-center gap-2.5 px-3 py-2.5 text-sm text-zinc-400 rounded-xl">
                  <div className="w-7 h-7 rounded-full bg-surface-3 border border-white/[0.06] flex items-center justify-center">
                    <User size={13} />
                  </div>
                  <span className="flex-1 truncate">{savedUser.display_name}</span>
                  {health && (
                    <span
                      className={cn(
                        "flex items-center gap-1.5 text-[13px] shrink-0",
                        conversationStoreDegraded ? "text-amber-300" : ""
                      )}
                    >
                      <span
                        className={cn(
                          "w-1.5 h-1.5 rounded-full",
                          conversationStoreDegraded ? "bg-amber-400" : "bg-emerald-500"
                        )}
                      />
                      {conversationStoreDegraded ? t("components.header.status.historyNotSaved") : t("components.header.status.normal")}
                    </span>
                  )}
                </div>
              )}
              {onLogout && (
                <button
                  onClick={() => { setMenuOpen(false); onLogout(); }}
                  className="w-full flex items-center gap-3 px-3 py-3 rounded-xl text-sm text-zinc-400 hover:text-red-400 hover:bg-red-500/[0.06] transition-colors active:scale-[0.98]"
                >
                  <LogOut size={16} />
                  <span>{t("components.header.actions.logout")}</span>
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  );
}
