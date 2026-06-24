import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  Plus,
  MessageSquare,
  Trash2,
  History,
  Sparkles,
  PanelLeftClose,
  Languages,
  X,
} from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { Conversation } from "@/hooks/useConversations";
import { api } from "@/api/client";
import CreditBar from "@/components/ui/CreditBar";
import { normalizeAppLanguage, persistClientLanguage, type AppLanguage } from "@/i18n/language";

interface Props {
  conversations: Conversation[];
  currentConvId: string | null;
  onNewConversation: () => void;
  onSwitchConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  open: boolean;
  onClose: () => void;
  onLogout?: () => void;
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

function relativeTime(ts: number, language: AppLanguage, t: Translate): string {
  const diff = Date.now() - ts;
  const m = Math.floor(diff / 60000);
  if (m < 1) return t("sidebar.justNow");
  if (m < 60) return t("sidebar.minutesAgo", { count: m });
  const h = Math.floor(m / 60);
  if (h < 24) return t("sidebar.hoursAgo", { count: h });
  const d = Math.floor(h / 24);
  if (d < 7) return t("sidebar.daysAgo", { count: d });
  return new Date(ts).toLocaleDateString(language, { month: "short", day: "numeric" });
}

function groupByAge(conversations: Conversation[], t: Translate) {
  const now = Date.now();
  const day = 24 * 60 * 60 * 1000;
  const groups: Array<{ label: string; items: Conversation[] }> = [
    { label: t("sidebar.groupToday"), items: [] },
    { label: t("sidebar.groupLast7Days"), items: [] },
    { label: t("sidebar.groupLast30Days"), items: [] },
    { label: t("sidebar.groupEarlier"), items: [] },
  ];

  for (const c of conversations) {
    const age = Math.max(0, now - c.updatedAt);
    if (age < day) groups[0].items.push(c);
    else if (age < 7 * day) groups[1].items.push(c);
    else if (age < 30 * day) groups[2].items.push(c);
    else groups[3].items.push(c);
  }
  return groups.filter((g) => g.items.length > 0);
}

export default function AppSidebar({
  conversations,
  currentConvId,
  onNewConversation,
  onSwitchConversation,
  onDeleteConversation,
  open,
  onClose,
  onLogout,
}: Props) {
  const location = useLocation();
  const navigate = useNavigate();
  const { t, i18n } = useTranslation();
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [isSwitchingLanguage, setIsSwitchingLanguage] = useState(false);
  const currentLanguage = normalizeAppLanguage(i18n.resolvedLanguage ?? i18n.language);
  const groupedConversations = groupByAge(conversations, t);

  const handleNavigate = (to: string, beforeNavigate?: () => void) => {
    beforeNavigate?.();
    navigate(to);
    onClose();
  };

  const handleDelete = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (deleteConfirm === id) {
      onDeleteConversation(id);
      setDeleteConfirm(null);
    } else {
      setDeleteConfirm(id);
      setTimeout(() => setDeleteConfirm(null), 3000);
    }
  };

  const handleLanguageToggle = async () => {
    if (isSwitchingLanguage) return;

    const nextLanguage: AppLanguage = currentLanguage === "zh-CN" ? "en-US" : "zh-CN";
    setIsSwitchingLanguage(true);
    try {
      persistClientLanguage(nextLanguage);
      await i18n.changeLanguage(nextLanguage);
      if (api.isLoggedIn()) {
        await api.setLanguage(nextLanguage);
      }
    } catch (error) {
      console.error("Failed to sync language preference", error);
    } finally {
      setIsSwitchingLanguage(false);
    }
  };

  const navItems = [
    { to: "/history", icon: History, label: t("sidebar.history") },
  ];

  const sidebarContent = (
    <div className="flex flex-col h-full bg-surface-1 border-r border-zinc-800/50">
      {/* Logo + collapse */}
      <div className="shrink-0 flex items-center justify-between px-3 h-11 border-b border-zinc-800/50">
        <Link to="/" className="flex items-center gap-2 group" onClick={onClose}>
          <div className="w-5 h-5 rounded-md bg-gradient-to-br from-oracle-400 to-oracle-600 flex items-center justify-center">
            <Sparkles size={11} className="text-surface-0" />
          </div>
          <span className="font-display text-sm text-zinc-100 tracking-wide">Synthora</span>
        </Link>
        <button
          onClick={onClose}
          className="p-1.5 rounded-lg text-zinc-400 hover:text-zinc-300 hover:bg-surface-3 transition-colors"
          aria-label={t("sidebar.close")}
        >
          <PanelLeftClose size={16} />
        </button>
      </div>

      {/* New conversation */}
      <div className="shrink-0 p-2">
        <button
          onClick={() => { handleNavigate("/", onNewConversation); }}
          className="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl border border-zinc-800/60 hover:border-zinc-700/60 hover:bg-surface-2 text-zinc-200 text-sm transition-all active:scale-[0.98]"
        >
          <Plus size={16} className="text-zinc-400" />
          <span>{t("sidebar.newConversation")}</span>
        </button>
      </div>

      {/* Conversation list */}
      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {conversations.length === 0 ? (
          <div className="text-center text-zinc-500 text-[13px] mt-8 px-4">
            {t("sidebar.empty")}
          </div>
        ) : (
          <div className="space-y-2">
            {groupedConversations.map((group) => (
              <div key={group.label}>
                <div className="px-2 py-1 text-[12px] uppercase tracking-wider text-zinc-500">{group.label}</div>
                <div className="space-y-0.5">
                  {group.items.map((conv) => {
                    const isActive = conv.id === currentConvId && location.pathname === "/";
                    const isDeleting = deleteConfirm === conv.id;
                    return (
                      <div
                        key={conv.id}
                        onClick={() => { handleNavigate("/", () => onSwitchConversation(conv.id)); }}
                        className={`
                          group relative px-3 py-2 rounded-lg cursor-pointer transition-colors
                          ${isActive ? "bg-surface-3/80 text-zinc-200" : "text-zinc-400 hover:bg-surface-3/40 hover:text-zinc-300"}
                        `}
                      >
                        <div className="flex items-start gap-2">
                          <MessageSquare size={13} className="mt-0.5 shrink-0" />
                          <div className="flex-1 min-w-0">
                            <div className="text-[13px] truncate">{conv.title}</div>
                            <div className="text-[12px] text-zinc-500 mt-0.5">
                              {relativeTime(conv.updatedAt, currentLanguage, t)}
                            </div>
                          </div>
                          <button
                            onClick={(e) => handleDelete(conv.id, e)}
                            className={`shrink-0 p-0.5 rounded opacity-0 group-hover:opacity-100 transition-opacity ${
                              isDeleting ? "text-red-400 opacity-100" : "text-zinc-600 hover:text-red-400"
                            }`}
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                        {isDeleting && (
                          <div className="absolute inset-0 flex items-center justify-center bg-surface-2/90 rounded-lg text-[13px] text-red-400">
                            {t("sidebar.deleteConfirm")}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Credit bar — compact in sidebar */}
      <div className="shrink-0 border-t border-zinc-800/50 px-2 pt-2">
        <CreditBar compact />
      </div>

      {/* Bottom nav */}
      <div className="shrink-0 p-2 space-y-0.5">
        <button
          type="button"
          onClick={() => { void handleLanguageToggle(); }}
          disabled={isSwitchingLanguage}
          className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] text-zinc-400 hover:text-zinc-300 hover:bg-surface-3/50 transition-colors disabled:opacity-60"
        >
          <Languages size={14} />
          <span>{t("sidebar.language")}</span>
          <span className="ml-auto rounded-md border border-zinc-700/70 px-1.5 py-0.5 text-[11px] text-zinc-300">
            {currentLanguage === "zh-CN" ? t("sidebar.languageShortZh") : t("sidebar.languageShortEn")}
          </span>
        </button>
        {navItems.map((item) => (
          <button
            key={item.to}
            type="button"
            onClick={() => handleNavigate(item.to)}
            className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] transition-colors ${
              location.pathname === item.to
                ? "text-oracle-400 bg-oracle-500/10"
                : "text-zinc-400 hover:text-zinc-300 hover:bg-surface-3/50"
            }`}
          >
            <item.icon size={14} />
            <span>{item.label}</span>
          </button>
        ))}
        {onLogout && (
          <button
            onClick={() => { onClose(); onLogout?.(); }}
            className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] text-zinc-500 hover:text-red-400 hover:bg-red-500/5 transition-colors"
          >
            <X size={14} />
            <span>{t("sidebar.logout")}</span>
          </button>
        )}
      </div>
    </div>
  );

  return (
    <>
      {/* Desktop sidebar — always rendered when open */}
      <div
        className={`hidden md:block shrink-0 transition-all duration-200 ${
          open ? "w-64" : "w-0"
        } overflow-hidden`}
      >
        <div className="w-64 h-full">{sidebarContent}</div>
      </div>

      {/* Mobile sidebar — overlay drawer */}
      {open && (
        <div className="md:hidden fixed inset-0 z-[60]">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={onClose}
          />
          {/* Drawer */}
          <div className="absolute inset-y-0 left-0 w-72 animate-slide-in-left">
            {sidebarContent}
          </div>
        </div>
      )}
    </>
  );
}
