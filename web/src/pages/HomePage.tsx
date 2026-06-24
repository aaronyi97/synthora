import { useState, useCallback, useEffect, useRef, type ComponentProps } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { WifiOff, Plus, ChevronDown, ChevronUp, Users, Layers, Edit3, ArrowRight, SkipForward, Sparkles } from "lucide-react";
import { api } from "@/api/client";
import type { HistoryItem, Mode } from "@/types";
import QueryInput from "@/components/query/QueryInput";
import ModeSelector from "@/components/query/ModeSelector";
import ResponseDisplay from "@/components/query/ResponseDisplay";
import CompanionRouteBubble from "@/components/query/CompanionRouteBubble";
import PostAnswerGuidance from "@/components/query/PostAnswerGuidance";
import type { CompanionAction } from "@/components/query/CompanionBubble";
import QueryProgressBars from "@/components/query/QueryProgressBars";
import ResearchTimeline from "@/components/query/ResearchTimeline";
import EnhancedMarkdown from "@/components/query/EnhancedMarkdown";
import TypewriterMarkdown from "@/components/query/TypewriterMarkdown";
import type { OptimizationPoint, QueryTask, WaitIssueCode } from "@/hooks/useQueryTasks";
import { useQueryTasksContext } from "@/contexts/QueryTasksContext";
import { useConversationContext } from "@/contexts/ConversationContext";
import { buildRoundtablePath, buildSocraticPath } from "@/lib/queryNavigation";
import { navigateWithFlushSync } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { SUPPORT_CONTACT } from "@/lib/constants";
import {
  ACTION_CHIP_DESTRUCTIVE,
  ACTION_CHIP_GHOST,
  ACTION_CHIP_PRIMARY,
  ACTION_CHIP_SECONDARY,
  OUTPUT_SECTION_HEADER,
  OUTPUT_SURFACE_ACCENT,
  OUTPUT_SURFACE_SOFT,
  STATUS_PANEL_ERROR,
  STATUS_PANEL_INFO,
  STATUS_PANEL_NEUTRAL,
  STATUS_PANEL_WARNING,
} from "@/lib/outputStyle";

type CompanionRouteAction = Parameters<
  NonNullable<ComponentProps<typeof CompanionRouteBubble>["onAction"]>
>[0];

type Translate = (key: string, options?: Record<string, unknown>) => string;

const CROSS_CHECK_STAGE_TOKEN = "\u4ea4\u53c9\u9a8c\u8bc1";
const POLISH_STAGE_TOKEN = "\u6253\u78e8";
const POLISHING_STAGE = "\u6253\u78e8\u7b54\u6848\u4e2d...";
const SEARCH_STAGE_TOKEN = "\u641c\u7d22";
const SYNTHESIZE_STAGE_TOKEN = "\u7efc\u5408";

// VersionCard: collapsible answer version card
function VersionCard({
  versionNum,
  label,
  labelColor,
  borderColor,
  defaultOpen,
  children,
}: {
  versionNum: number;
  label: string;
  labelColor: string;
  borderColor: string;
  defaultOpen: boolean;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={cn("mt-2 border", OUTPUT_SURFACE_SOFT, borderColor)}>
      <button
        onClick={() => setOpen(!open)}
        className={cn(OUTPUT_SECTION_HEADER, "px-3 py-2 text-[13px] border-b border-white/[0.08]")}
      >
        <div className="flex items-center gap-1.5">
          <Layers size={12} className={labelColor} />
          <span className={labelColor}>{t("pages.home.versionCard.title", { version: versionNum, label })}</span>
        </div>
        {open
          ? <ChevronUp size={12} className="text-zinc-500" />
          : <ChevronDown size={12} className="text-zinc-500" />}
      </button>
      {open && <div className="px-3 py-2">{children}</div>}
    </div>
  );
}

function buildEmptyStateExamples(t: Translate): Array<{ question: string; mode: Mode }> {
  return [
    { question: t("pages.home.examples.reactVsVue"), mode: "deep" },
    { question: t("pages.home.examples.quantumCryptography"), mode: "research" },
    { question: t("pages.home.examples.businessEmail"), mode: "light" },
    { question: t("pages.home.examples.marsPlan"), mode: "deep" },
  ];
}

function buildModeLabels(t: Translate): Record<string, string> {
  return {
    auto: t("common.modes.autoLabel"),
    deep: t("common.modes.deepLabel"),
    light: t("common.modes.lightLabel"),
    research: t("common.modes.researchLabel"),
  };
}

function buildModePlaceholders(t: Translate): Record<string, string> {
  return {
    auto: t("pages.home.modePlaceholders.auto"),
    deep: t("pages.home.modePlaceholders.deep"),
    light: t("pages.home.modePlaceholders.light"),
    research: t("pages.home.modePlaceholders.research"),
  };
}

const LONG_WAIT_THRESHOLD_SECONDS: Record<string, number> = {
  deep: 60,
  research: 90,
};

const REFRESH_RECOVERY_SYNC_TIMEOUT_MS = 3000;

type RefreshRecoveryState =
  | { status: "checking"; conversationId: string; question: string }
  | { status: "interrupted"; conversationId: string; question: string };

type PendingNavigation =
  | { to: string; state?: Record<string, unknown> }
  | null;

function matchesRecoveredHistoryItem(
  candidate: { question: string; timestamp: number; sessionId: string | null },
  item: { question: string; created_at: string; session_id?: string | null },
): boolean {
  if (item.question !== candidate.question) return false;
  if (candidate.sessionId && item.session_id === candidate.sessionId) return true;
  const createdAt = new Date(item.created_at).getTime();
  return Number.isFinite(createdAt) && Math.abs(createdAt - candidate.timestamp) <= 10 * 60 * 1000;
}

function isWaitMode(mode: string | undefined): mode is "deep" | "research" {
  return mode === "deep" || mode === "research";
}

function formatWaitDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remain = seconds % 60;
  return remain > 0 ? `${minutes}m ${remain}s` : `${minutes}m`;
}

function getWaitIssueMeta(t: Translate, code: Exclude<WaitIssueCode, null>, mode: string): { title: string; detail: string } {
  const modeLabel = mode === "research"
    ? t("common.modes.researchName")
    : mode === "deep"
      ? t("common.modes.deepName")
      : t("common.modes.currentMode");
  switch (code) {
    case "timeout":
      return {
        title: t("pages.home.wait.issues.timeout.title"),
        detail: t("pages.home.wait.issues.timeout.detail", { modeLabel }),
      };
    case "all_models_failed":
      return {
        title: t("pages.home.wait.issues.allModelsFailed.title"),
        detail: t("pages.home.wait.issues.allModelsFailed.detail"),
      };
    case "quota_limited":
      return {
        title: t("pages.home.wait.issues.quotaLimited.title"),
        detail: t("pages.home.wait.issues.quotaLimited.detail", { modeLabel }),
      };
    case "user_cancelled":
      return {
        title: t("pages.home.wait.issues.userCancelled.title"),
        detail: t("pages.home.wait.issues.userCancelled.detail"),
      };
  }
}

function getWaitReason(t: Translate, task: QueryTask): string {
  if (task.stageDetail?.trim()) return task.stageDetail.trim();
  if (task.companionRoute?.message?.trim()) return task.companionRoute.message.trim();
  if (task.companionRoute?.route_reason?.trim()) return task.companionRoute.route_reason.trim();
  if (task.stage.includes(SEARCH_STAGE_TOKEN)) return t("pages.home.wait.reasons.searching");
  if (task.stage.includes(CROSS_CHECK_STAGE_TOKEN)) return t("pages.home.wait.reasons.crossChecking");
  if (task.stage.includes(SYNTHESIZE_STAGE_TOKEN)) return t("pages.home.wait.reasons.synthesizing");
  if (task.stage.includes(POLISH_STAGE_TOKEN)) return t("pages.home.wait.reasons.polishing");
  return t("pages.home.wait.reasons.processing");
}

function getWaitRouteReason(task: QueryTask): string | null {
  if (task.companionRoute?.route_reason?.trim()) return task.companionRoute.route_reason.trim();
  return null;
}

function getWaitActionSummary(t: Translate, task: QueryTask, longWait: boolean): string {
  void task;
  if (longWait) return t("pages.home.wait.actionSummary.long");
  return t("pages.home.wait.actionSummary.short");
}

export default function HomePage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [mode, setMode] = useState<Mode>(() => {
    try { const saved = sessionStorage.getItem('synthora_mode'); if (saved && ['auto','light','deep','research','socratic','roundtable'].includes(saved)) return saved as Mode; } catch {}
    return 'auto';
  });
  const [webSearch, setWebSearch] = useState(true);
  const emptyStateExamples = buildEmptyStateExamples(t);
  const modeLabels = buildModeLabels(t);
  const modePlaceholders = buildModePlaceholders(t);

  // Persist mode selection
  useEffect(() => {
    try { sessionStorage.setItem('synthora_mode', mode); } catch { /* */ }
  }, [mode]);
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);
  const [retryingBackend, setRetryingBackend] = useState(false);
  const [submitRequest, setSubmitRequest] = useState<{ id: number; question: string } | null>(null);
  const [pendingNavigation, setPendingNavigation] = useState<PendingNavigation>(null);

  // Session management
  const [sessionId, setSessionId] = useState<string | null>(() => {
    try { return localStorage.getItem('synthora_session_id'); } catch { return null; }
  });
  const [turnCount, setTurnCount] = useState(() => {
    try { const s = localStorage.getItem('synthora_turn_count'); return s ? parseInt(s, 10) : 0; } catch { return 0; }
  });

  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomDockRef = useRef<HTMLDivElement>(null);

  // Multi-query task manager (lifted to Layout via QueryTasksContext for nav persistence)
  const { tasks, selectedId, setSelectedId, startQuery, cancelTask, removeTask } = useQueryTasksContext();

  // Conversation persistence (P0-4 bridge)
  const {
    createConversation,
    addMessage,
    updateSession,
    openHistoryAsConversation,
    syncHistory,
    currentId: convCurrentId,
    current: currentConv,
  } = useConversationContext();
  const activeConvIdRef = useRef<string | null>(null);

  // v3.3f: Collapse state for the content panel (independent of selectedId)
  const [contentCollapsed, setContentCollapsed] = useState(false);
  const [dismissedLongWait, setDismissedLongWait] = useState<Record<string, boolean>>({});
  const [keyboardOffset, setKeyboardOffset] = useState(0);
  const [refreshRecovery, setRefreshRecovery] = useState<RefreshRecoveryState | null>(null);
  const attemptedRefreshRecoveryRef = useRef<string | null>(null);
  const handledHistoryLocationKeyRef = useRef<string | null>(null);

  const scopedTasks = tasks.filter((t) => (t.conversationId ?? null) === (convCurrentId ?? null));
  const task = scopedTasks.find((t) => t.id === selectedId) ?? scopedTasks[0] ?? null;

  const checkBackendHealth = useCallback(async () => {
    try {
      await api.health();
      setBackendOnline(true);
    } catch {
      setBackendOnline(false);
    }
  }, []);

  useEffect(() => {
    checkBackendHealth();
  }, [checkBackendHealth]);

  // Persist session
  useEffect(() => {
    try {
      if (sessionId) { localStorage.setItem('synthora_session_id', sessionId); localStorage.setItem('synthora_turn_count', turnCount.toString()); }
      else { localStorage.removeItem('synthora_session_id'); localStorage.removeItem('synthora_turn_count'); }
    } catch { /* ignore */ }
  }, [sessionId, turnCount]);

  // Auto-scroll only on discrete events (new draft, response done) — NOT on every token
  const userScrolledUp = useRef(false);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el || userScrolledUp.current) return;
    el.scrollTop = el.scrollHeight;
  }, []);

  const keepBottomDockVisible = useCallback((behavior: ScrollBehavior = "smooth") => {
    if (!bottomDockRef.current) return;
    bottomDockRef.current.scrollIntoView({ block: "end", behavior });
  }, []);

  // Detect manual scroll-up to stop auto-scroll; reset when near bottom
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
      userScrolledUp.current = !nearBottom;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || !window.visualViewport) return;
    const vv = window.visualViewport;
    let rafId = 0;
    let focusTimer = 0;

    const updateKeyboardOffset = () => {
      if (rafId) window.cancelAnimationFrame(rafId);
      rafId = window.requestAnimationFrame(() => {
        const activeEl = document.activeElement;
        const dockHasFocus = !!(
          activeEl instanceof HTMLElement
          && bottomDockRef.current?.contains(activeEl)
        );
        const overlap = Math.max(0, window.innerHeight - (vv.height + vv.offsetTop));
        const nextOffset = dockHasFocus && overlap > 80 ? overlap : 0;
        setKeyboardOffset((prev) => (Math.abs(prev - nextOffset) > 1 ? nextOffset : prev));
        if (dockHasFocus) {
          keepBottomDockVisible(nextOffset > 0 ? "smooth" : "auto");
        }
      });
    };

    const handleFocusIn = () => {
      window.clearTimeout(focusTimer);
      focusTimer = window.setTimeout(updateKeyboardOffset, 60);
    };

    const handleFocusOut = () => {
      window.clearTimeout(focusTimer);
      focusTimer = window.setTimeout(updateKeyboardOffset, 140);
    };

    updateKeyboardOffset();
    vv.addEventListener("resize", updateKeyboardOffset);
    vv.addEventListener("scroll", updateKeyboardOffset);
    window.addEventListener("focusin", handleFocusIn);
    window.addEventListener("focusout", handleFocusOut);

    return () => {
      if (rafId) window.cancelAnimationFrame(rafId);
      window.clearTimeout(focusTimer);
      vv.removeEventListener("resize", updateKeyboardOffset);
      vv.removeEventListener("scroll", updateKeyboardOffset);
      window.removeEventListener("focusin", handleFocusIn);
      window.removeEventListener("focusout", handleFocusOut);
    };
  }, [keepBottomDockVisible]);

  // Keep runtime session aligned with the selected sidebar conversation.
  useEffect(() => {
    if (!currentConv) return;
    activeConvIdRef.current = currentConv.id;
    setSessionId(currentConv.sessionId ?? null);
    setTurnCount(currentConv.turnCount ?? 0);
  }, [currentConv?.id, currentConv?.sessionId, currentConv?.turnCount]);

  const handleNewChat = useCallback(() => {
    const newId = createConversation();
    activeConvIdRef.current = newId;
    setSessionId(null); setTurnCount(0);
    setEditingQuestion("");
    try { localStorage.removeItem('synthora_session_id'); localStorage.removeItem('synthora_turn_count'); } catch { /* */ }
    window.setTimeout(() => {
      document.querySelector<HTMLTextAreaElement>("[data-query-input]")?.focus();
    }, 100);
  }, [createConversation]);

  const navigateToSocratic = useCallback((question: string) => {
    const trimmed = question.trim();
    setPendingNavigation({
      to: buildSocraticPath(question),
      state: { q: trimmed },
    });
  }, []);

  const navigateToRoundtable = useCallback((question: string) => {
    const trimmed = question.trim();
    setPendingNavigation({
      to: buildRoundtablePath(question),
      state: { q: trimmed, autoStart: true },
    });
  }, []);

  const handleAsk = useCallback(
    (question: string, fileIds?: string[]) => {
      if (mode === "socratic") {
        navigateToSocratic(question);
        return;
      }
      if (mode === "roundtable") {
        navigateToRoundtable(question);
        return;
      }

      // P0-4: create or reuse conversation, write user message immediately
      let convId = convCurrentId;
      if (!convId) convId = createConversation();
      activeConvIdRef.current = convId;
      if (convId) {
        addMessage(convId, { role: 'user', content: question, timestamp: Date.now() });
      }

      // v4.17 Fix2: if the user just chose to edit manually, skip preflight for that hand-edited version.
      const doSkip = (mode as string) === "light" || (mode as string) === "auto" || skipNextPreflight.current;
      skipNextPreflight.current = false;
      const capturedConvId = convId;
      const activeSessionId = (currentConv && currentConv.id === capturedConvId)
        ? currentConv.sessionId
        : sessionId;
      startQuery({
        question,
        mode,
        conversationId: capturedConvId ?? null,
        webSearch,
        sessionId: activeSessionId || undefined,
        fileIds,
        skipPreflight: doSkip,
        // BUG-4 fix: persist assistant message immediately on complete (not via useEffect)
        onCompleted: (r) => {
          if (capturedConvId) {
            addMessage(capturedConvId, {
              role: 'assistant',
              content: r.final_answer || t("errors.noValidResponseReceived"),
              response: r,
              timestamp: Date.now(),
            });
          }
        },
      });
    },
    [mode, webSearch, sessionId, navigateToSocratic, navigateToRoundtable, startQuery, convCurrentId, currentConv, createConversation, addMessage, t],
  );

  const handleExampleAsk = useCallback((question: string, targetMode: Mode) => {
    setMode(targetMode);
    setSubmitRequest({ id: Date.now(), question });
  }, []);

  // v3.3 → v4.17: Confirm question and re-submit with skip_preflight
  const handleConfirmQuestion = useCallback(
    (question: string, taskId: string) => {
      removeTask(taskId);
      const convId = activeConvIdRef.current ?? convCurrentId ?? null;
      const activeSessionId = (currentConv && currentConv.id === convId)
        ? currentConv.sessionId
        : sessionId;
      const capturedConvId = convId;
      // E3: Only write another user message when the confirmed text differs from the latest one.
      if (capturedConvId) {
        const lastUserMsg = currentConv?.messages?.filter(m => m.role === 'user').slice(-1)[0];
        if (!lastUserMsg || lastUserMsg.content !== question) {
          addMessage(capturedConvId, { role: 'user', content: question, timestamp: Date.now() });
        }
      }
      startQuery({
        question,
        mode,
        conversationId: convId,
        webSearch,
        sessionId: activeSessionId || undefined,
        skipPreflight: true,
        onCompleted: (r) => {
          if (capturedConvId) {
            addMessage(capturedConvId, {
              role: 'assistant',
              content: r.final_answer || t("errors.noValidResponseReceived"),
              response: r,
              timestamp: Date.now(),
            });
          }
        },
      });
    },
    [mode, webSearch, sessionId, startQuery, removeTask, convCurrentId, currentConv, addMessage, t],
  );

  // v3.3: Edit question — populate input (user types new version)
  const [editingQuestion, setEditingQuestion] = useState<string>("");

  // v4.17: Fix2 — track when user has seen preflight and chose to edit manually.
  // Next submit should skip preflight so their hand-edited version isn't re-optimized.
  const skipNextPreflight = useRef(false);

  // v4.17: per-task editable optimized question (user can tweak inline before confirming)
  const [editedOptimized, setEditedOptimized] = useState<Record<string, string>>({});

  const handleLowConfidenceAction = useCallback(
    (action: string, question: string) => {
      const m = (action === "retry_deep" || action === "deep") ? "deep"
        : (action === "retry_research" || action === "research") ? "research" : "deep";
      if (action === "socratic") {
        navigateToSocratic(question);
        return;
      }
      setMode(m as Mode);
      const convId = activeConvIdRef.current ?? convCurrentId ?? null;
      const activeSessionId = (currentConv && currentConv.id === convId)
        ? currentConv.sessionId
        : sessionId;
      // D7: Write user message for low-confidence action retries
      if (convId) {
        addMessage(convId, { role: 'user', content: question, timestamp: Date.now() });
      }
      startQuery({
        question, mode: m, conversationId: convId, webSearch, sessionId: activeSessionId || undefined,
        onCompleted: (r) => {
          if (convId) {
            addMessage(convId, {
              role: 'assistant',
              content: r.final_answer || t("errors.noValidResponseReceived"),
              response: r,
              timestamp: Date.now(),
            });
          }
        },
      });
    },
    [webSearch, sessionId, startQuery, convCurrentId, currentConv, addMessage, navigateToSocratic, t],
  );

  const runCompanionGuideAction = useCallback(
    (action: CompanionAction, baseQuestion: string) => {
      const payload = action.action_payload || {};
      const targetMode = (payload.mode as string) || "deep";
      if (action.action_type === "query_deep" || action.action_type === "explore_divergence" || action.action_type === "query_single") {
        const isSingleModel = action.action_type === "query_single";
        const singleModelId = isSingleModel ? (payload.model_id as string | undefined) : undefined;
        const queryMode = isSingleModel ? "auto" : targetMode;
        setMode(queryMode as Mode);
        const convId = activeConvIdRef.current ?? convCurrentId ?? null;
        const activeSessionId = (currentConv && currentConv.id === convId)
          ? currentConv.sessionId
          : sessionId;
        if (convId) {
          addMessage(convId, { role: "user", content: baseQuestion, timestamp: Date.now() });
        }
        startQuery({
          question: baseQuestion,
          mode: queryMode,
          conversationId: convId,
          webSearch,
          sessionId: activeSessionId || undefined,
          singleModelId,
          onCompleted: (r) => {
            if (convId) {
              addMessage(convId, {
                role: "assistant",
                content: r.final_answer || "",
                response: r,
                timestamp: Date.now(),
              });
            }
          },
        });
      } else if (action.action_type === "query_followup") {
        setMode(targetMode as Mode);
      } else if (action.action_type === "roundtable") {
        navigateToRoundtable((payload.question as string) || baseQuestion);
      }
    },
    [convCurrentId, currentConv, sessionId, startQuery, webSearch, addMessage, navigateToRoundtable],
  );

  const handleCompanionGuideAction = useCallback(
    (action: CompanionAction) => {
      runCompanionGuideAction(action, task?.question || "");
    },
    [runCompanionGuideAction, task?.question],
  );

  const handleRetry = useCallback(
    (question: string, retryMode: string) => {
      const targetMode = (["light", "deep", "research", "socratic", "roundtable"].includes(retryMode) ? retryMode : mode) as Mode;
      if (targetMode === "socratic") {
        navigateToSocratic(question);
        return;
      }
      if (targetMode === "roundtable") {
        navigateToRoundtable(question);
        return;
      }
      setMode(targetMode);
      const convId = activeConvIdRef.current ?? convCurrentId ?? null;
      const activeSessionId = (currentConv && currentConv.id === convId)
        ? currentConv.sessionId
        : sessionId;
      // D7: Write user message for retry
      if (convId) {
        addMessage(convId, { role: 'user', content: question, timestamp: Date.now() });
      }
      startQuery({
        question,
        mode: targetMode,
        conversationId: convId,
        webSearch,
        sessionId: activeSessionId || undefined,
        skipPreflight: true,
        onCompleted: (r) => {
          if (convId) {
            addMessage(convId, {
              role: 'assistant',
              content: r.final_answer || t("errors.noValidResponseReceived"),
              response: r,
              timestamp: Date.now(),
            });
          }
        },
      });
    },
    [mode, webSearch, sessionId, startQuery, convCurrentId, currentConv, navigateToSocratic, navigateToRoundtable, addMessage, t],
  );

  const handleRestartFromWaiting = useCallback((targetMode: string) => {
    if (!task) return;
    if (task.status === "streaming") {
      cancelTask(task.id);
    }
    handleRetry(task.question, targetMode);
  }, [task, cancelTask, handleRetry]);
  const handleDismissLongWait = useCallback(() => {
    if (!task) return;
    setDismissedLongWait((prev) => ({ ...prev, [task.id]: true }));
  }, [task]);
  const handleCancelCurrentWait = useCallback(() => {
    if (!task) return;
    cancelTask(task.id);
  }, [task, cancelTask]);
  const handleCompanionRouteAction = useCallback((action: CompanionRouteAction) => {
    if (!task) return;

    const payload = action.action_payload || {};
    const actionType = action.action_type;

    if (actionType === "cancel") {
      cancelTask(task.id);
      return;
    }

    cancelTask(task.id);

    if (actionType === "query_light") {
      handleRetry(task.question, "light");
      return;
    }
    if (actionType === "query_single") {
      const singleModelId = typeof payload.model_id === "string" ? payload.model_id : undefined;
      const convId = activeConvIdRef.current ?? convCurrentId ?? null;
      const activeSessionId = (currentConv && currentConv.id === convId)
        ? currentConv.sessionId
        : sessionId;
      setMode("auto");
      if (convId) {
        addMessage(convId, { role: 'user', content: task.question, timestamp: Date.now() });
      }
      startQuery({
        question: task.question,
        mode: "auto",
        conversationId: convId,
        webSearch,
        sessionId: activeSessionId || undefined,
        singleModelId,
        onCompleted: (r) => {
          if (convId) {
            addMessage(convId, {
              role: 'assistant',
              content: r.final_answer || t("errors.noValidResponseReceived"),
              response: r,
              timestamp: Date.now(),
            });
          }
        },
      });
      return;
    }
    if (actionType === "query_deep") {
      handleRetry(task.question, "deep");
      return;
    }

    const targetMode = typeof payload.mode === "string" && payload.mode
      ? payload.mode
      : actionType.startsWith("query_")
      ? actionType.replace(/^query_/, "")
      : actionType;

    handleRetry(task.question, targetMode);
  }, [task, cancelTask, handleRetry, convCurrentId, currentConv, sessionId, addMessage, startQuery, webSearch, t]);
  const draftCount = task?.draftAnswers?.length ?? 0;
  const isDone = task?.status === "done" || task?.status === "error";
  useEffect(() => {
    if (!task && scopedTasks.length > 0) {
      setSelectedId(scopedTasks[0].id);
    }
  }, [task, scopedTasks, setSelectedId]);
  useEffect(() => { scrollToBottom(); }, [draftCount, isDone, scrollToBottom]);

  // Track session from completed queries (BUG-4: addMessage moved to onCompleted for immediate persist)
  useEffect(() => {
    if (task?.response?.session_id && task.status === "done") {
      const newSessionId = task.response.session_id;
      const convId = activeConvIdRef.current;
      setSessionId(newSessionId);
      setTurnCount((prev) => {
        const next = prev + 1;
        if (convId) updateSession(convId, newSessionId, next);
        return next;
      });
    }
  }, [task?.response?.session_id, task?.status, updateSession]);

  // Auto-expand content when streaming finishes
  useEffect(() => {
    if (task?.status === "done" || task?.status === "error") {
      setContentCollapsed(false);
    }
  }, [task?.status]);

  useEffect(() => {
    setDismissedLongWait((prev) => {
      const next: Record<string, boolean> = {};
      for (const t of scopedTasks) {
        if (t.status === "streaming" && prev[t.id]) next[t.id] = true;
      }
      return next;
    });
  }, [scopedTasks]);

  const isStreaming = task?.status === "streaming";
  const hasTasks = scopedTasks.length > 0;
  const historyMessages = currentConv?.messages ?? [];
  const activeTaskPersistedToHistory = !!(
    task?.status === "done"
    && task.response?.query_id
    && historyMessages.some(
      (msg) => msg.role === "assistant" && msg.response?.query_id === task.response?.query_id,
    )
  );
  const showCompletedTaskBridge = !!(
    task
    && task.status === "done"
    && task.response
    && !task.error
    && !task.questionConfirmation
    && !task.clarification
    && !activeTaskPersistedToHistory
  );
  const recoveryCandidate = (() => {
    if (!currentConv || scopedTasks.some((t) => t.status === "streaming")) return null;
    const lastMessage = currentConv.messages[currentConv.messages.length - 1];
    if (!lastMessage || lastMessage.role !== "user") return null;
    return {
      conversationId: currentConv.id,
      question: lastMessage.content,
      timestamp: lastMessage.timestamp,
      sessionId: currentConv.sessionId ?? null,
    };
  })();
  const recoveryMarker = recoveryCandidate
    ? `${recoveryCandidate.conversationId}:${recoveryCandidate.timestamp}`
    : null;
  const showHistoryMessages = historyMessages.length > 0;
  const showTaskPanel = !!task && (
    task.status === "streaming" ||
    showCompletedTaskBridge ||
    !!task.error ||
    !!task.questionConfirmation ||
    !!task.clarification
  );
  const hasContent = hasTasks || showHistoryMessages || showCompletedTaskBridge;
  const hasMeaningfulStreamTokens = !!task?.streamTokens?.trim();
  const hasMeaningfulStreamPreview = !!task?.streamPreview?.trim();
  const hasMeaningfulStream = hasMeaningfulStreamTokens || hasMeaningfulStreamPreview;
  const waitIssueCode: WaitIssueCode = task?.waitIssueCode ?? null;
  const waitIssueMeta = waitIssueCode && task ? getWaitIssueMeta(t, waitIssueCode, task.mode) : null;
  const isWaitModeTask = !!task && isWaitMode(task.mode);
  const longWaitThreshold = task ? (LONG_WAIT_THRESHOLD_SECONDS[task.mode] ?? 0) : 0;
  const showLongWaitPrompt = !!(
    task
    && isStreaming
    && isWaitMode(task.mode)
    && longWaitThreshold > 0
    && task.elapsed >= longWaitThreshold
    && !dismissedLongWait[task.id]
  );
  const showWaitCoach = !!(task && isStreaming && isWaitMode(task.mode));
  const waitReason = task && isWaitModeTask ? getWaitReason(t, task) : "";
  const waitRouteReason = task && isWaitModeTask ? getWaitRouteReason(task) : null;
  const currentStageElapsed = task ? Math.max(0, Math.round((Date.now() - task.stageStartedAt) / 1000)) : 0;

  // v4.8: Auto mode removed — resolved mode badge only shows on auto-escalation (Light→Deep)
  const resolvedMode = null;
  const scrollAreaStyle = contentCollapsed
    ? { maxHeight: 0, overflow: "hidden", scrollPaddingBottom: `${128 + keyboardOffset}px` }
    : { maxHeight: "none", scrollPaddingBottom: `${128 + keyboardOffset}px` };
  const bottomDockStyle = {
    paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))",
    transform: keyboardOffset > 0 ? `translateY(-${keyboardOffset}px)` : undefined,
    transition: "transform 180ms ease-out",
  };

  useEffect(() => {
    const navState = (location.state ?? {}) as {
      openHistoryItem?: HistoryItem;
      answerOverride?: string;
    };
    if (!navState.openHistoryItem) return;
    if (handledHistoryLocationKeyRef.current === location.key) return;

    handledHistoryLocationKeyRef.current = location.key;
    openHistoryAsConversation(navState.openHistoryItem, navState.answerOverride);
    navigateWithFlushSync(navigate, "/", { replace: true, state: {} });
  }, [location.key, location.state, navigate, openHistoryAsConversation]);

  useEffect(() => {
    if (!recoveryCandidate) {
      attemptedRefreshRecoveryRef.current = null;
      setRefreshRecovery(null);
      return;
    }
    if (!recoveryMarker) return;
    if (attemptedRefreshRecoveryRef.current === recoveryMarker) return;
    attemptedRefreshRecoveryRef.current = recoveryMarker;

    let cancelled = false;
    setRefreshRecovery({
      status: "checking",
      conversationId: recoveryCandidate.conversationId,
      question: recoveryCandidate.question,
    });

    const delays = [0, 1500, 4000];
    (async () => {
      for (const delay of delays) {
        if (delay > 0) {
          await new Promise((resolve) => window.setTimeout(resolve, delay));
        }
        if (cancelled) return;
        try {
          const res = await Promise.race([
            syncHistory(100, 0),
            new Promise<null>((resolve) => window.setTimeout(() => resolve(null), REFRESH_RECOVERY_SYNC_TIMEOUT_MS)),
          ]);
          if (cancelled) return;
          if (!res) continue;
          const matched = res.history.find((item) => matchesRecoveredHistoryItem(recoveryCandidate, item));
          if (matched) {
            openHistoryAsConversation(matched);
            setRefreshRecovery(null);
            attemptedRefreshRecoveryRef.current = null;
            return;
          }
        } catch {
          // best effort — final degraded state below
        }
      }
      if (!cancelled) {
        setRefreshRecovery({
          status: "interrupted",
          conversationId: recoveryCandidate.conversationId,
          question: recoveryCandidate.question,
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    openHistoryAsConversation,
    recoveryMarker,
    recoveryCandidate?.conversationId,
    recoveryCandidate?.question,
    recoveryCandidate?.sessionId,
    recoveryCandidate?.timestamp,
    syncHistory,
  ]);

  useEffect(() => {
    if (!pendingNavigation) return;
    navigateWithFlushSync(navigate, pendingNavigation.to, {
      state: pendingNavigation.state,
    });
    setPendingNavigation(null);
  }, [navigate, pendingNavigation]);

  return (
    <div className="h-full min-h-0 flex flex-col">
      {/* Backend offline banner */}
      {backendOnline === false && (
        <div className="shrink-0 px-4 py-2 bg-amber-500/[0.06] border-b border-amber-500/15 text-amber-400 text-[13px]">
          <div className="mx-auto flex w-full max-w-3xl items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <WifiOff size={11} />
              <span>{t("pages.home.offlineBanner.message")}</span>
              <span className="text-zinc-400">·</span>
              <span className="text-oracle-400 font-medium select-all">{SUPPORT_CONTACT}</span>
            </div>
            <button
              type="button"
              disabled={retryingBackend}
              onClick={async () => {
                setRetryingBackend(true);
                await checkBackendHealth();
                setRetryingBackend(false);
              }}
              className="shrink-0 rounded-full border border-amber-500/25 bg-amber-500/[0.08] px-2.5 py-1 text-[13px] font-medium text-amber-300 transition-colors hover:bg-amber-500/[0.12] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {retryingBackend ? t("pages.home.offlineBanner.retrying") : t("pages.home.offlineBanner.retryConnection")}
            </button>
          </div>
        </div>
      )}

      {/* ===== Progress Bars — all queries at top ===== */}
      <QueryProgressBars
        tasks={scopedTasks}
        selectedId={task?.id ?? null}
        onSelect={(id) => {
          if (id === task?.id) {
            setContentCollapsed((c) => !c);
          } else {
            setSelectedId(id);
            setContentCollapsed(false);
          }
        }}
        onCancel={cancelTask}
        onRemove={removeTask}
        collapsed={contentCollapsed}
      />

      {/* ===== Main scrollable area ===== */}
      <div
        ref={scrollRef}
        className={`flex-1 overflow-y-auto overscroll-y-contain${!hasContent ? ' flex flex-col' : ''}`}
        style={scrollAreaStyle}
      >
        <div className={`max-w-3xl mx-auto px-3 sm:px-4${!hasContent ? ' flex-1 flex flex-col' : ''} pb-3 sm:pb-4`}>

          {/* Conversation history — always visible for current thread */}
          {showHistoryMessages && (
            <div className="pb-4 space-y-4">
              {refreshRecovery && (
                <div className={cn(
                  "rounded-2xl border px-4 py-3 text-[13px] leading-relaxed",
                  refreshRecovery.status === "checking" ? STATUS_PANEL_INFO : STATUS_PANEL_WARNING,
                )}>
                  {refreshRecovery.status === "checking" ? (
                    <>
                      <p className="text-sky-200">{t("pages.home.refreshRecovery.checking.title")}</p>
                      <p className="text-zinc-300">
                        {t("pages.home.refreshRecovery.checking.detail")}
                      </p>
                    </>
                  ) : (
                    <>
                      <p className="text-amber-300">{t("pages.home.refreshRecovery.interrupted.title")}</p>
                      <p className="text-zinc-300">
                        {t("pages.home.refreshRecovery.interrupted.detail")}
                      </p>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <button
                          onClick={() => handleRetry(refreshRecovery.question, mode)}
                          className={ACTION_CHIP_SECONDARY}
                        >
                          {t("common.actions.retryCurrentMode")}
                        </button>
                        <button
                          onClick={() => navigateWithFlushSync(navigate, "/history")}
                          className={ACTION_CHIP_GHOST}
                        >
                          {t("common.actions.goToHistory")}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
              {historyMessages.map((msg, i) => (
                <div key={i}>
                  {msg.role === 'user' ? (
                    <div className="flex justify-end pt-3 pb-1">
                      <div className="max-w-[85%] px-3.5 py-2 rounded-2xl bg-surface-3/80 text-zinc-200 text-[13px] leading-relaxed whitespace-pre-wrap">
                        {msg.content}
                      </div>
                    </div>
                  ) : (
                    <div className="mt-1">
                      {msg.response ? (
                        (() => {
                          const historyResponse = msg.response!;
                          return (
                            <>
                              <ResponseDisplay response={historyResponse} onAction={handleLowConfidenceAction} onRetry={handleRetry} />
                              <PostAnswerGuidance
                                response={historyResponse}
                                onCompanionAction={(action: CompanionAction) => runCompanionGuideAction(action, historyResponse.question || "")}
                                isHistory
                              />
                            </>
                          );
                        })()
                      ) : (
                        <div className={cn(OUTPUT_SURFACE_SOFT, "px-4 py-3")}>
                          <EnhancedMarkdown content={msg.content} />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Empty state */}
          {!hasContent && (
            <div className="flex flex-col items-center justify-center gap-4 px-2 sm:px-4 flex-1">
              {/* Oracle glyph */}
              <div className="relative">
                <div className="absolute inset-0 rounded-full bg-oracle-500/10 blur-xl scale-150" />
                <span className="relative select-none text-[28px] sm:text-[32px] leading-none" style={{ color: "#eab308", opacity: 0.4 }} aria-hidden="true">✦</span>
              </div>
              <div className="text-center">
                <p className="text-[15px] text-zinc-200 font-medium">{t("pages.home.emptyState.title")}</p>
                <p className="mt-1 text-[13px] text-zinc-400">{t("pages.home.emptyState.subtitle")}</p>
              </div>

              <div className="w-full max-w-2xl">
                <div className="grid grid-cols-2 gap-2">
                  {emptyStateExamples.map((example) => (
                    <button
                      key={`${example.mode}-${example.question}`}
                      type="button"
                      onClick={() => handleExampleAsk(example.question, example.mode)}
                      className="group flex min-h-[92px] flex-col justify-between rounded-xl border border-zinc-800/40 bg-zinc-900/40 px-3 py-2 text-left transition-all hover:border-oracle-500/20 hover:bg-oracle-500/5"
                    >
                      <span className="inline-flex w-fit rounded-full border border-white/[0.08] bg-white/[0.04] px-2 py-0.5 text-[13px] uppercase tracking-wide text-zinc-300 group-hover:text-oracle-200">
                        {modeLabels[example.mode]}
                      </span>
                      <p className="mt-2 text-[13px] leading-relaxed text-zinc-200 group-hover:text-zinc-200">
                        {example.question}
                      </p>
                    </button>
                  ))}
                </div>
              </div>

            </div>
          )}

          {/* Session indicator */}
          {sessionId && turnCount > 0 && !isStreaming && (
            <div className="flex items-center justify-between pt-2 pb-1">
              <span className="text-[13px] text-zinc-400">{t("pages.home.sessionIndicator.turn", { count: turnCount })}</span>
              <button onClick={handleNewChat} className="flex items-center gap-1 text-[13px] text-zinc-300 hover:text-zinc-200 transition-colors">
                <Plus size={12} /> {t("common.actions.newConversation")}
              </button>
            </div>
          )}

          {/* Selected task content */}
          {showTaskPanel && task && (
            <div className="pb-4">
              {/* User question bubble */}
              {!showHistoryMessages && (
                <div className="flex justify-end pt-4 pb-2">
                  <div className="max-w-[85%] rounded-2xl rounded-tr-md border border-white/[0.06] bg-surface-3/90 px-4 py-2.5 text-[13px] leading-relaxed text-zinc-200 whitespace-pre-wrap shadow-[0_8px_20px_rgba(0,0,0,0.16)]">
                    {task.question}
                  </div>
                </div>
              )}

              {/* Resolved mode badge */}
              {resolvedMode && !isStreaming && (
                <div className="mb-1 flex items-center gap-1 text-[12px] text-oracle-300/80">
                  <span>{t("pages.home.badges.modeUpgrade")}</span>
                  <span className="text-oracle-400">{resolvedMode}</span>
                </div>
              )}

              {/* Error — amber warning if preview exists (partial result), red if no result at all */}
              {task.error && !waitIssueMeta && (
                (task.streamPreview || task.streamTokens) ? (
                  <div className={cn("mt-2 space-y-1", STATUS_PANEL_WARNING)}>
                    <p className="text-amber-400">{t("pages.home.failure.partialResult")}</p>
                    <p className="text-zinc-300">{task.error}</p>
                  </div>
                ) : (
                  <div className={cn("mt-2 space-y-1.5", STATUS_PANEL_ERROR)}>
                    <p className="text-red-400">{task.error}</p>
                    <p className="text-zinc-300">
                      {t("pages.home.failure.feedback", { wechat: SUPPORT_CONTACT })}
                    </p>
                  </div>
                )
              )}

              {/* Waiting-mechanics: unified terminal strategy for timeout/all-failed/quota/cancelled */}
              {waitIssueMeta && !isStreaming && (
                <div className={cn("mt-2 space-y-2", STATUS_PANEL_WARNING)}>
                  <p className="text-amber-300">{waitIssueMeta.title}</p>
                  <p className="text-zinc-300">{waitIssueMeta.detail}</p>
                  {task.error && <p className="text-zinc-400">{task.error}</p>}
                  <div className="flex flex-wrap gap-2 pt-0.5">
                    <button
                      onClick={() => handleRestartFromWaiting(task.mode)}
                      className={ACTION_CHIP_SECONDARY}
                    >
                      {waitIssueCode === "user_cancelled" ? t("common.actions.restartCurrentRound") : t("common.actions.retrySameMode")}
                    </button>
                    <button
                      onClick={() => handleRestartFromWaiting("light")}
                      className={ACTION_CHIP_PRIMARY}
                    >
                      {t("common.actions.switchToLight")}
                    </button>
                    <button
                      onClick={() => removeTask(task.id)}
                      className={ACTION_CHIP_GHOST}
                    >
                      {t("common.actions.closeRound")}
                    </button>
                  </div>
                </div>
              )}

              {/* v4.17: Question confirmation — structured per-change breakdown */}
              {task.questionConfirmation && !task.response && !isStreaming && (() => {
                const qc = task.questionConfirmation;
                const points: OptimizationPoint[] = qc.optimization_points ?? [];
                const currentOptimized = editedOptimized[task.id] ?? qc.optimized_question;
                return (
                  <div className="mt-3 animate-fade-in">
                    <div className={cn(OUTPUT_SURFACE_SOFT, "flex items-start gap-2.5 px-3.5 py-3.5")}>
                      <div className="w-7 h-7 rounded-full bg-oracle-500/15 text-oracle-400 flex items-center justify-center shrink-0 mt-0.5">
                        <Sparkles size={13} />
                      </div>
                      <div className="max-w-[90%] min-w-0">
                        <p className="text-[13px] text-zinc-400">{t("pages.home.questionConfirmation.intro")}</p>
                        <p className="mt-1.5 text-[14px] text-zinc-100 leading-relaxed whitespace-pre-wrap">
                          {currentOptimized}
                        </p>
                        {points.length > 0 && (
                          <div className="mt-2 space-y-1">
                            {points.slice(0, 3).map((point, idx) => (
                              <p key={`${task.id}-opt-${idx}`} className="text-[13px] text-zinc-400 leading-relaxed">
                                {idx + 1}. {point.reason || point.type}
                              </p>
                            ))}
                          </div>
                        )}
                        <div className="mt-2.5 flex items-center gap-2 flex-wrap">
                          <button
                            onClick={() => {
                              setEditedOptimized(prev => { const n = { ...prev }; delete n[task.id]; return n; });
                              handleConfirmQuestion(currentOptimized, task.id);
                            }}
                            className={cn(ACTION_CHIP_PRIMARY, "active:scale-95")}
                          >
                            <ArrowRight size={12} />
                            {t("common.actions.sendQuestion")}
                          </button>
                          <button
                            onClick={() => {
                              setEditedOptimized(prev => { const n = { ...prev }; delete n[task.id]; return n; });
                              handleConfirmQuestion(qc.original_question, task.id);
                            }}
                            className={ACTION_CHIP_GHOST}
                          >
                            {t("common.actions.useOriginalQuestion")}
                          </button>
                          <button
                            onClick={() => {
                              const q = currentOptimized;
                              setEditedOptimized(prev => { const n = { ...prev }; delete n[task.id]; return n; });
                              skipNextPreflight.current = true;
                              setEditingQuestion(q);
                              removeTask(task.id);
                              setTimeout(() => {
                                const input = document.querySelector<HTMLTextAreaElement>('[data-query-input]');
                                if (input) { input.value = q; input.dispatchEvent(new Event('input', { bubbles: true })); input.focus(); }
                              }, 80);
                            }}
                            className={ACTION_CHIP_GHOST}
                          >
                            <Edit3 size={12} className="inline mr-1" />
                            {t("common.actions.editAgain")}
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })()}

              {/* Version ladder: preview -> draft(s) -> judge stream -> final. Each tier stays visible independently. */}

              {isStreaming && (
                <div className="max-h-[35vh] overflow-y-auto">
                  {/* v4.24: Research timeline for research-mode pipeline progress */}
                  {task.mode === "research" && task.stageHistory.length > 0 && (
                    <div className="mt-2">
                      <ResearchTimeline
                        stageHistory={task.stageHistory}
                        isStreaming={isStreaming ?? false}
                        taskStatus={task.waitIssueCode === "user_cancelled" ? "user_cancelled" : task.status}
                      />
                    </div>
                  )}

                  {/* v5.1: Companion skeleton bubble — Auto mode <200ms instant feedback */}
                  {task.companionSkeleton && !task.companionRoute && (
                    <div className="mt-2 flex items-start gap-2.5 animate-fade-in">
                      <div className="w-7 h-7 rounded-full bg-oracle-500/15 text-oracle-400 flex items-center justify-center shrink-0 mt-0.5">
                        <Sparkles size={13} className="animate-pulse" />
                      </div>
                      <div className="flex items-center gap-1.5 py-2 text-[13px] text-zinc-300">
                        <span>{t("pages.home.skeleton.analyzingQuestion")}</span>
                        <span className="inline-block w-1.5 h-3.5 bg-oracle-400/60 animate-pulse" />
                      </div>
                    </div>
                  )}

                  {/* v5.1: Companion route status — Dispatcher pre-route info (pipeline already running) */}
                  {task.companionRoute && !task.companionRoute.is_silent && (
                    <CompanionRouteBubble route={task.companionRoute} onAction={handleCompanionRouteAction} />
                  )}

                  {showWaitCoach && task && (
                    <div className={cn("mt-2 space-y-2", STATUS_PANEL_INFO)}>
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-[13px] font-medium text-sky-200">{t("pages.home.wait.coach.currentPhase", { stage: task.stage })}</p>
                        <span className="rounded-full border border-white/[0.12] bg-white/[0.05] px-2 py-0.5 text-[13px] text-zinc-200">
                          {modeLabels[task.mode]}
                        </span>
                        <span className="text-[13px] text-zinc-300">{t("pages.home.wait.coach.elapsed", { elapsed: formatWaitDuration(task.elapsed) })}</span>
                        {currentStageElapsed > 0 && (
                          <span className="text-[13px] text-zinc-300">{t("pages.home.wait.coach.stageElapsed", { elapsed: formatWaitDuration(currentStageElapsed) })}</span>
                        )}
                        {task.contributorsDone > 0 && (
                          <span className="text-[13px] text-zinc-300">{t("pages.home.wait.coach.contributorsDone", { count: task.contributorsDone })}</span>
                        )}
                      </div>
                      <p className="text-[13px] text-zinc-200">
                        {t("pages.home.wait.coach.systemProgress")}
                      </p>
                      <p className="text-zinc-300">{waitReason}</p>
                      {waitRouteReason && waitRouteReason !== waitReason && (
                        <p className="text-[13px] text-zinc-300">{waitRouteReason}</p>
                      )}
                      {!showLongWaitPrompt && (
                        <>
                          <p className="text-[13px] text-zinc-300">{getWaitActionSummary(t, task, false)}</p>
                          <div className="flex flex-wrap gap-2 pt-0.5">
                            <button
                              onClick={handleCancelCurrentWait}
                              className={ACTION_CHIP_GHOST}
                            >
                              {t("common.actions.cancelCurrentRound")}
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  )}

                  {/* Waiting state before any content appears */}
                  {!showWaitCoach && !task.companionSkeleton && !hasMeaningfulStream && task.draftAnswers.length === 0 && !task.streamPreview && (
                    <div className={cn("mt-2 flex items-center gap-2 py-4", STATUS_PANEL_NEUTRAL)}>
                      <div className="w-1.5 h-1.5 rounded-full bg-oracle-500/50 animate-pulse" />
                      <span>{task.stage}</span>
                      {task.contributorsDone > 0 && <span className="text-zinc-400 ml-1">{t("pages.home.wait.waitingState.contributorsDone", { count: task.contributorsDone })}</span>}
                    </div>
                  )}

                  {/* Waiting-mechanics: long-wait actions (continue/retry/light) */}
                  {showLongWaitPrompt && (
                    <div className={cn("mt-2 space-y-2", STATUS_PANEL_WARNING)}>
                      <p className="text-amber-300">{t("pages.home.wait.longPrompt.title", { elapsed: formatWaitDuration(task.elapsed), stage: task.stage })}</p>
                      <p className="text-[13px] text-amber-100">
                        {t("pages.home.wait.longPrompt.detail")}
                      </p>
                      <p className="text-zinc-300">{getWaitActionSummary(t, task, true)}</p>
                      <div className="flex flex-wrap gap-2 pt-0.5">
                        <button
                          onClick={handleDismissLongWait}
                          className={ACTION_CHIP_SECONDARY}
                        >
                          <SkipForward size={12} />
                          {t("common.actions.continueWaiting")}
                        </button>
                        <button
                          onClick={() => handleRestartFromWaiting(task.mode)}
                          className={ACTION_CHIP_SECONDARY}
                        >
                          {t("common.actions.retry")}
                        </button>
                        <button
                          onClick={() => handleRestartFromWaiting("light")}
                          className={ACTION_CHIP_PRIMARY}
                        >
                          {t("common.actions.switchToLight")}
                        </button>
                        <button
                          onClick={handleCancelCurrentWait}
                          className={ACTION_CHIP_DESTRUCTIVE}
                        >
                          {t("common.actions.cancelCurrentRound")}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Version 1: fastest answer preview, stays visible once it appears */}
              {task.streamPreview && (
                <VersionCard
                  versionNum={1}
                  label={t("pages.home.versionLabels.fastestAnswer")}
                  labelColor="text-zinc-400"
                  borderColor="border-zinc-700/50"
                  defaultOpen
                >
                  <TypewriterMarkdown content={task.streamPreview.replace(/\r\n/g, "\n").replace(/\r/g, "\n")} />
                </VersionCard>
              )}

              {/* Version 2+: preliminary aggregation and cross-review drafts */}
              {task.draftAnswers.map((draft, i) => {
                const isDraftFirst = draft.stage === "fan_out_best";
                return (
                  <VersionCard
                    key={draft.stage}
                    versionNum={(task.streamPreview ? 1 : 0) + i + 1}
                    label={isDraftFirst ? t("pages.home.versionLabels.preliminaryAggregation") : t("pages.home.versionLabels.crossReviewImprovement")}
                    labelColor={isDraftFirst ? "text-blue-400/70" : "text-violet-400/70"}
                    borderColor={isDraftFirst ? "border-blue-500/20" : "border-violet-500/20"}
                    defaultOpen={false}
                  >
                    <TypewriterMarkdown content={draft.content.replace(/\r\n/g, "\n").replace(/\r/g, "\n")} />
                  </VersionCard>
                );
              })}

              {/* Judge stream: appended below all drafts while still streaming */}
              {isStreaming && hasMeaningfulStreamTokens && (
                <div className="mt-2">
                  <div className={OUTPUT_SURFACE_ACCENT}>
                    <div className={cn(OUTPUT_SECTION_HEADER, "px-3 py-2 text-[13px] border-b border-white/[0.08]")}>
                      <Layers size={12} className="text-oracle-400/80" />
                      <span className="text-oracle-400/70">
                        {t("pages.home.versionCard.title", {
                          version: (task.streamPreview ? 1 : 0) + task.draftAnswers.length + 1,
                          label: t("pages.home.versionLabels.finalSynthesis"),
                        })}
                      </span>
                      <span className="text-oracle-400/50 animate-pulse ml-1">
                        {task.stage === POLISHING_STAGE ? t("pages.home.versionLabels.additionalOptimizing") : t("pages.home.versionLabels.generating")}
                      </span>
                    </div>
                    <div className="px-3 py-2">
                      <EnhancedMarkdown
                        content={task.streamTokens}
                        citations={task.streamCitations.length > 0 ? task.streamCitations as unknown as import("@/components/query/EnhancedMarkdown").Citation[] : undefined}
                      />
                      <span className="inline-block w-1.5 h-3.5 bg-oracle-400/60 animate-pulse ml-0.5 align-middle" />
                    </div>
                  </div>
                </div>
              )}

              {/* Final answer replaces the judge stream card after streaming stops */}
              {task.response && !isStreaming && (
                <div className="mt-2 animate-fade-in">
                  {task.draftAnswers.length > 0 ? (
                    <VersionCard
                      versionNum={(task.streamPreview ? 1 : 0) + task.draftAnswers.length + 1}
                      label={t("pages.home.versionLabels.finalSynthesis")}
                      labelColor="text-oracle-400"
                      borderColor="border-oracle-500/25"
                      defaultOpen
                    >
                      <ResponseDisplay response={task.response} onAction={handleLowConfidenceAction} onRetry={handleRetry} />
                    </VersionCard>
                  ) : (
                    <ResponseDisplay response={task.response} onAction={handleLowConfidenceAction} onRetry={handleRetry} />
                  )}
                </div>
              )}

              {/* Multi-model discussion status — show label only, no model count or expand */}
              {task.contributorsDone > 0 && (
                <div className="mt-3">
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-white/[0.06] bg-white/[0.03] px-2.5 py-1 text-[13px] text-zinc-400 select-none">
                    <Users size={12} />
                    <span>{t("pages.home.discussionBadge")}</span>
                  </span>
                </div>
              )}

              {/* Clarification */}
              {task.clarification && (
                <div className={cn("mt-2 text-xs", STATUS_PANEL_WARNING)}>
                  <p className="text-zinc-300 mb-2">{task.clarification.message}</p>
                  {task.clarification.suggested_questions.length > 0 && (
                    <div className="flex flex-col gap-1 mt-1">
                      {task.clarification.suggested_questions.map((q: string, i: number) => (
                        <button key={i} onClick={() => handleAsk(q)}
                          className={cn(ACTION_CHIP_SECONDARY, "w-full justify-start text-left")}>
                          {q}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* v5.1: Silent route meta label — shows how Auto resolved when route was silent */}
              {!isStreaming && task.companionRoute?.is_silent && task.companionRoute.resolved_mode && (
                <div className="mt-2 flex items-center gap-1.5 text-[13px] text-zinc-300 select-none">
                  <span>⚡</span>
                  <span>
                    {t("pages.home.autoResolved.route", {
                      mode: modeLabels[task.companionRoute.resolved_mode] ?? task.companionRoute.resolved_mode,
                    })}
                  </span>
                  {(task.companionRoute.contributor_count ?? 0) > 0 && (
                    <span>{t("pages.home.autoResolved.modeCount", { count: task.companionRoute.contributor_count })}</span>
                  )}
                  {task.response?.latency_ms != null && (
                    <span>· {(task.response.latency_ms / 1000).toFixed(1)}s</span>
                  )}
                </div>
              )}

              {task.response && !isStreaming && (
                <PostAnswerGuidance
                  response={task.response}
                  onCompanionAction={handleCompanionGuideAction}
                />
              )}
            </div>
          )}
        </div>
      </div>

      {/* ===== Bottom fixed: input + mode selector ===== */}
      <div
        ref={bottomDockRef}
        className="sticky bottom-0 z-20 shrink-0 border-t border-white/[0.04] bg-surface-0/90 backdrop-blur-sm px-2 sm:px-4 pt-2"
        style={bottomDockStyle}
      >
        <div className="max-w-3xl mx-auto">
          <QueryInput
            onSubmit={handleAsk}
            loading={isStreaming}
            mode={mode}
            onCancel={isStreaming && task ? () => cancelTask(task.id) : undefined}
            initialValue={editingQuestion}
            placeholder={modePlaceholders[mode] ?? modePlaceholders.auto}
            submitRequest={submitRequest}
            footerRight={
              <ModeSelector
                selected={mode}
                onChange={setMode}
                disabled={false}
                webSearch={webSearch}
                onWebSearchChange={setWebSearch}
              />
            }
          />
        </div>
      </div>
    </div>
  );
}
