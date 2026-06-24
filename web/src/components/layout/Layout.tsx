import { useState, useCallback, useEffect, useRef } from "react";
import { Outlet } from "react-router-dom";
import { Sparkles, PanelLeft, Menu, Share2, Link as LinkIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import ErrorBoundary from "@/components/ErrorBoundary";
import AppSidebar from "./AppSidebar";
import { useConversations } from "@/hooks/useConversations";
import { ConversationContext } from "@/contexts/ConversationContext";
import { useQueryTasks } from "@/hooks/useQueryTasks";
import { QueryTasksContext } from "@/contexts/QueryTasksContext";

interface Props {
  onLogout?: () => void;
}

export default function Layout({ onLogout }: Props) {
  const { t } = useTranslation();
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    // Default: open on desktop, closed on mobile
    if (typeof window !== "undefined") return window.innerWidth >= 768;
    return false;
  });

  const convState = useConversations();
  const queryTasksState = useQueryTasks();
  const {
    conversations,
    current,
    currentId,
    createConversation,
    deleteConversation,
    switchConversation,
  } = convState;
  const [shareOpen, setShareOpen] = useState(false);
  const shareRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!shareOpen) return;
    const handlePointerDown = (evt: MouseEvent) => {
      const target = evt.target as Node | null;
      if (shareRef.current && target && !shareRef.current.contains(target)) {
        setShareOpen(false);
      }
    };
    const handleKeyDown = (evt: KeyboardEvent) => {
      if (evt.key === "Escape") {
        setShareOpen(false);
      }
    };
    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [shareOpen]);

  const handleNewConversation = useCallback(() => {
    createConversation();
  }, [createConversation]);

  const buildShareText = useCallback(() => {
    if (!current?.messages?.length) return "";
    return current.messages
      .map((m) => {
        const role = m.role === "user" ? t("components.layout.share.roles.user") : t("components.layout.share.roles.assistant");
        const content = m.response?.final_answer || m.content;
        return `${role}: ${content}`;
      })
      .join("\n\n");
  }, [current, t]);

  return (
    <div className="h-[100dvh] md:h-screen flex overflow-hidden">
      {/* Sidebar */}
      <AppSidebar
        conversations={conversations}
        currentConvId={currentId}
        onNewConversation={handleNewConversation}
        onSwitchConversation={switchConversation}
        onDeleteConversation={deleteConversation}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onLogout={onLogout}
      />

      {/* Main column */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Minimal top bar */}
        <div className="shrink-0 flex items-center h-11 px-3 border-b border-zinc-800/50 bg-[#09090b]/95">
          {/* Sidebar toggle */}
          {!sidebarOpen && (
            <button
              onClick={() => setSidebarOpen(true)}
              className="p-1.5 rounded-lg text-zinc-500 hover:text-zinc-300 hover:bg-surface-3 transition-colors mr-2"
              aria-label={t("components.layout.actions.openSidebar")}
            >
              <PanelLeft size={16} className="hidden md:block" />
              <Menu size={18} className="md:hidden" />
            </button>
          )}
          {/* Mobile logo (when sidebar closed) */}
          {!sidebarOpen && (
            <Link to="/" className="flex items-center gap-2 md:hidden">
              <div className="w-5 h-5 rounded-md bg-gradient-to-br from-oracle-400 to-oracle-600 flex items-center justify-center">
                <Sparkles size={11} className="text-surface-0" />
              </div>
              <span className="font-display text-sm text-zinc-100 tracking-wide">Synthora</span>
            </Link>
          )}
          <div className="flex-1" />
          <div ref={shareRef} className="relative">
            <button
              type="button"
              onClick={() => {
                if (!current?.messages?.length) return;
                setShareOpen((v) => !v);
              }}
              disabled={!current?.messages?.length}
              className={`p-1.5 rounded-lg transition-colors ${
                current?.messages?.length
                  ? "text-zinc-400 hover:text-zinc-200 hover:bg-surface-3"
                  : "text-zinc-700 cursor-not-allowed"
              }`}
              title={current?.messages?.length ? t("components.layout.share.button") : t("components.layout.share.empty")}
              aria-label={t("components.layout.share.button")}
            >
              <Share2 size={15} />
            </button>
            {shareOpen && current?.messages?.length ? (
              <div className="absolute right-0 mt-1.5 w-36 rounded-lg border border-white/[0.08] bg-surface-2 p-1.5 shadow-xl shadow-black/40 z-30 animate-fade-in">
                <button
                  type="button"
                  onClick={() => {
                    const text = buildShareText();
                    if (text) navigator.clipboard.writeText(text).catch(() => {});
                    setShareOpen(false);
                  }}
                  className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-zinc-300 hover:bg-surface-3 transition-colors"
                >
                  <LinkIcon size={12} />
                  {t("components.layout.share.copyContent")}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const text = buildShareText();
                    if (!text) return;
                    if (navigator.share) {
                      navigator.share({ title: t("components.layout.share.nativeTitle"), text }).catch(() => {});
                    } else {
                      navigator.clipboard.writeText(text).catch(() => {});
                    }
                    setShareOpen(false);
                  }}
                  className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-zinc-300 hover:bg-surface-3 transition-colors"
                >
                  <Share2 size={12} />
                  {t("components.layout.share.systemShare")}
                </button>
              </div>
            ) : null}
          </div>
        </div>

        {/* Content — ConversationContext + QueryTasksContext shared with all child pages */}
        <ConversationContext.Provider value={convState}>
          <QueryTasksContext.Provider value={queryTasksState}>
            <main className="flex-1 min-h-0 overflow-hidden">
              <ErrorBoundary>
                <Outlet />
              </ErrorBoundary>
            </main>
          </QueryTasksContext.Provider>
        </ConversationContext.Provider>
      </div>
    </div>
  );
}
