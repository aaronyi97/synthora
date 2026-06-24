import { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useLocation, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";

function sanitizeHref(url: string | undefined): string {
  if (!url) return "#";
  const trimmed = url.trim().toLowerCase();
  if (trimmed.startsWith("javascript:") || trimmed.startsWith("data:") || trimmed.startsWith("vbscript:")) {
    return "#";
  }
  return url;
}

const safeMarkdownComponents = {
  a: ({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { children?: React.ReactNode }) => (
    <a href={sanitizeHref(href)} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>
  ),
};
import {
  Send,
  Loader2,
  Eye,
  ArrowLeft,
  MessageCircle,
  GraduationCap,
  Brain,
  Sparkles,
  Target,
  Users,
  CheckCircle2,
  Search,
  Zap,
} from "lucide-react";
import { api } from "@/api/client";
import type {
  SocraticStartResponse,
  SocraticRevealResponse,
  ChatTurn,
  ApiError,
} from "@/types";
import clsx from "clsx";

// Phase 1 streaming progress state
interface Phase1Progress {
  stage: string;
  detail: string;
  contributors: { model_id: string; success: boolean; latency_ms: number }[];
  totalContributors: number;
  divergenceReady: boolean;
  consensusPoints: string[];
  divergenceCount: number;
  overallConsensus: number;
}

const INITIAL_PHASE1: Phase1Progress = {
  stage: "",
  detail: "",
  contributors: [],
  totalContributors: 0,
  divergenceReady: false,
  consensusPoints: [],
  divergenceCount: 0,
  overallConsensus: 0,
};
const MAX_SOCRATIC_CHECKPOINTS = 5;

function isRevealCommand(message: string): boolean {
  const trimmed = message.trim();
  const normalized = trimmed.toLowerCase();
  return trimmed === i18n.getFixedT("zh-CN")("pages.socratic.actions.revealAnswer")
    || normalized === i18n.getFixedT("en-US")("pages.socratic.actions.revealAnswer").toLowerCase()
    || normalized === "reveal";
}

export default function SocraticPage() {
  const { t } = useTranslation();
  const { sessionId: urlSessionId } = useParams<{ sessionId: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const text = {
    modeLabel: t("pages.socratic.modeLabel"),
    prepare: t("pages.socratic.phase1.prepare"),
    thinking: t("pages.socratic.phase1.thinking"),
    analyzing: t("pages.socratic.phase1.analyzing"),
    guiding: t("pages.socratic.phase1.guiding"),
    contributorProgress: t("pages.socratic.phase1.contributorProgress"),
    expertUnit: t("pages.socratic.phase1.expertUnit"),
    expertLabel: (index: number) => t("pages.socratic.phase1.expertLabel", { index }),
    consensus: t("pages.socratic.phase1.consensus"),
    divergenceCount: (count: number) => t("pages.socratic.phase1.divergenceCount", { count }),
    cancel: t("common.actions.cancel"),
    connectionError: t("pages.socratic.errors.connectionFailed"),
    recoveredSession: t("pages.socratic.recoveredSession"),
    sendError: t("pages.socratic.errors.sendFailed"),
    revealError: t("pages.socratic.errors.revealFailed"),
    sessionExpired: t("pages.socratic.errors.sessionExpired"),
    sessionExpiredHint: t("pages.socratic.errors.sessionExpiredHint"),
    backHome: t("pages.socratic.actions.backHome"),
    roundsInfo: (round: number, maxRounds: number) => t("pages.socratic.progress.roundsInfo", { round, maxRounds }),
    phaseName: (round: number, maxRounds: number) => {
      if (round === 0) return t("pages.socratic.progress.phaseNames.explore");
      if (round <= maxRounds * 0.4) return t("pages.socratic.progress.phaseNames.diverge");
      if (round <= maxRounds * 0.7) return t("pages.socratic.progress.phaseNames.converge");
      return t("pages.socratic.progress.phaseNames.deepen");
    },
    phaseSuffix: t("pages.socratic.progress.phaseSuffix"),
    roundNumber: (round: number) => t("pages.socratic.progress.roundNumber", { round }),
    readyToStart: t("pages.socratic.progress.readyToStart"),
    continueQuestion: t("pages.socratic.continueQuestion"),
    revealAnswer: t("pages.socratic.actions.revealAnswer"),
    revealTitle: t("pages.socratic.reveal.title"),
    divergenceMap: t("pages.socratic.reveal.divergenceMap"),
    divergencePointCount: (count: number) => t("pages.socratic.reveal.divergencePointCount", { count }),
    expertCount: (count: number) => t("pages.socratic.reveal.expertCount", { count }),
    cognitiveSnapshot: t("pages.socratic.reveal.cognitiveSnapshot"),
    reasoningDepth: t("pages.socratic.reveal.reasoningDepth"),
    nuanceRecognition: t("pages.socratic.reveal.nuanceRecognition"),
    anchoringDetected: t("pages.socratic.reveal.anchoringDetected"),
    confirmationBiasDetected: t("pages.socratic.reveal.confirmationBiasDetected"),
    blindSpots: t("pages.socratic.reveal.blindSpots"),
    guidedRounds: (count: number) => t("pages.socratic.progress.guidedRounds", { count }),
    waitingToExplore: (count: number) => t("pages.socratic.progress.waitingToExplore", { count }),
    thinkingShort: t("pages.socratic.thinkingShort"),
    inputPlaceholder: t("pages.socratic.inputPlaceholder"),
    continueAsking: t("pages.socratic.actions.continueAsking"),
  };

  const locState = location.state as {
    startRes?: SocraticStartResponse;
    q?: string;
    question?: string;
  } | null;
  const queryQuestion = new URLSearchParams(location.search).get("q") ?? "";
  const effectiveQuestion = (locState?.q ?? locState?.question ?? queryQuestion).trim();

  // B4+C5: sessionStorage checkpoint helpers (supports both Phase1 temp key and Phase2 session key)
  const phase1TempKey = effectiveQuestion ? `socratic_p1_${effectiveQuestion.slice(0, 60)}` : null;
  // G3: Ref to track latest phase1TempKey — prevents stale closure in startStreaming
  const phase1TempKeyRef = useRef(phase1TempKey);
  phase1TempKeyRef.current = phase1TempKey;
  const ssKey = urlSessionId && urlSessionId !== "new" ? `socratic_${urlSessionId}` : phase1TempKey;
  const loadCheckpoint = () => {
    // Try session key first, then Phase1 temp key
    for (const k of [urlSessionId && urlSessionId !== "new" ? `socratic_${urlSessionId}` : null, phase1TempKey].filter(Boolean) as string[]) {
      try { const raw = sessionStorage.getItem(k); if (raw) return JSON.parse(raw); } catch { /* */ }
    }
    return null;
  };
  const checkpoint = loadCheckpoint();

  // Core state — restore from sessionStorage if available
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    checkpoint?.activeSessionId ?? (urlSessionId && urlSessionId !== "new" ? urlSessionId : null)
  );
  const [maxRounds, setMaxRounds] = useState(checkpoint?.maxRounds ?? locState?.startRes?.max_guide_rounds ?? 5);
  const [divergenceInfo, setDivergenceInfo] = useState(checkpoint?.divergenceInfo ?? locState?.startRes?.divergence_map ?? null);
  const [round, setRound] = useState(checkpoint?.round ?? 0);
  const [turns, setTurns] = useState<ChatTurn[]>(() => {
    if (checkpoint?.turns?.length) return checkpoint.turns;
    const initial = locState?.startRes?.initial_guide;
    return initial ? [{ role: "guide", content: initial }] : [];
  });
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [revealing, setRevealing] = useState(false);
  const [revealData, setRevealData] = useState<SocraticRevealResponse | null>(checkpoint?.revealData ?? null);
  const [error, setError] = useState<string | null>(null);

  // C5: Phase 1 streaming state — declared before persist effect so closure order is clear
  const [isPhase1, setIsPhase1] = useState(checkpoint?.isPhase1 ?? false);
  const [phase1Progress, setPhase1Progress] = useState<Phase1Progress>(checkpoint?.phase1Progress ?? INITIAL_PHASE1);

  // B4+C5: Persist state to sessionStorage on meaningful changes (Phase1 + Phase2)
  useEffect(() => {
    const key = activeSessionId ? `socratic_${activeSessionId}` : phase1TempKey;
    if (!key) return;
    try {
      const savedAt = Date.now();
      sessionStorage.setItem(key, JSON.stringify({
        activeSessionId, maxRounds, divergenceInfo, round, turns, revealData,
        isPhase1, phase1Progress, effectiveQuestion, savedAt,
      }));
      const checkpointKeys = Array.from({ length: sessionStorage.length }, (_, idx) => sessionStorage.key(idx))
        .filter((item): item is string => !!item && item.startsWith("socratic_"));
      if (checkpointKeys.length > MAX_SOCRATIC_CHECKPOINTS) {
        const staleKeys = checkpointKeys
          .map((checkpointKey) => {
            try {
              const raw = sessionStorage.getItem(checkpointKey);
              const parsed = raw ? JSON.parse(raw) : null;
              return {
                checkpointKey,
                savedAt: typeof parsed?.savedAt === "number" ? parsed.savedAt : 0,
              };
            } catch {
              return { checkpointKey, savedAt: 0 };
            }
          })
          .sort((a, b) => a.savedAt - b.savedAt)
          .slice(0, checkpointKeys.length - MAX_SOCRATIC_CHECKPOINTS);
        for (const stale of staleKeys) {
          if (stale.checkpointKey !== key) {
            sessionStorage.removeItem(stale.checkpointKey);
          }
        }
      }
    } catch { /* quota exceeded — ignore */ }
  }, [activeSessionId, maxRounds, divergenceInfo, round, turns, revealData, isPhase1, phase1Progress, effectiveQuestion, phase1TempKey]);
  const abortRef = useRef<AbortController | null>(null);
  const streamingStartedRef = useRef(false);
  const routeKeyRef = useRef("");

  const chatEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to bottom
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, revealData, phase1Progress]);

  // Focus input after response
  useEffect(() => {
    if (!sending && !revealing && !revealData && !isPhase1) {
      inputRef.current?.focus();
    }
  }, [sending, revealing, revealData, isPhase1]);

  // 方案C: Start SSE streaming on mount if this is a new session
  const startStreaming = useCallback(async (q: string) => {
    const ac = new AbortController();
    abortRef.current = ac;
    setIsPhase1(true);
    setError(null);

    try {
      await api.socraticStartStream(q, {
        onStage: (stage, detail) => {
          setPhase1Progress(prev => ({ ...prev, stage, detail }));
        },
        onContributor: (data) => {
          setPhase1Progress(prev => ({
            ...prev,
            contributors: [...prev.contributors, { model_id: data.model_id, success: data.success, latency_ms: data.latency_ms }],
            totalContributors: data.total_count,
          }));
        },
        onDivergence: (data) => {
          setPhase1Progress(prev => ({
            ...prev,
            divergenceReady: true,
            consensusPoints: data.consensus_points,
            divergenceCount: data.divergence_count,
            overallConsensus: data.overall_consensus,
          }));
        },
        onReady: (data) => {
          // Phase 1 complete — transition to chat
          setActiveSessionId(data.session_id);
          setMaxRounds(data.max_guide_rounds);
          setDivergenceInfo(data.divergence_map);
          setTurns([{ role: "guide", content: data.initial_guide }]);
          setIsPhase1(false);
          // C5: Clean up Phase1 temp checkpoint — permanent key will be written by persist effect
          // G3: Use ref to get latest key (startStreaming has [] deps, phase1TempKey would be stale)
          if (phase1TempKeyRef.current) { try { sessionStorage.removeItem(phase1TempKeyRef.current); } catch { /* */ } }
          // Update URL without full navigation
          window.history.replaceState(null, "", `/socratic/${data.session_id}`);
        },
        onError: (err) => {
          setError(err);
          setIsPhase1(false);
        },
      }, ac.signal);
    } catch (e) {
      if (!ac.signal.aborted) {
        setError((e as Error).message || text.connectionError);
        setIsPhase1(false);
      }
    }
  }, [text.connectionError]);

  useEffect(() => {
    // Reset state when route/question changes to prevent stale session blocking new starts.
    const routeKey = `${urlSessionId ?? ""}|${effectiveQuestion}`;
    if (routeKeyRef.current === routeKey) return;
    routeKeyRef.current = routeKey;
    streamingStartedRef.current = false;
    abortRef.current?.abort();

    if (urlSessionId === "new") {
      // D3: Skip reset if checkpoint was restored (prevents overwriting recovered Phase1 state)
      if (checkpoint) return;
      const initial = locState?.startRes?.initial_guide;
      setActiveSessionId(null);
      setMaxRounds(locState?.startRes?.max_guide_rounds ?? 5);
      setDivergenceInfo(locState?.startRes?.divergence_map ?? null);
      setRound(0);
      setRevealData(null);
      setError(null);
      setIsPhase1(false);
      setPhase1Progress(INITIAL_PHASE1);
      setTurns(initial ? [{ role: "guide", content: initial }] : []);
    } else if (urlSessionId && urlSessionId !== "new") {
      setActiveSessionId(urlSessionId);
      setError(null);
    }
  }, [urlSessionId, effectiveQuestion, locState?.startRes]);

  useEffect(() => {
    // If navigated to /socratic/new with a question, start streaming.
    if (
      urlSessionId === "new" &&
      effectiveQuestion &&
      !streamingStartedRef.current &&
      !activeSessionId &&
      turns.length === 0 &&
      !isPhase1
    ) {
      streamingStartedRef.current = true;
      startStreaming(effectiveQuestion);
    }
  }, [urlSessionId, effectiveQuestion, activeSessionId, turns.length, isPhase1, startStreaming]);

  useEffect(() => {
    return () => { abortRef.current?.abort(); };
  }, []);

  useEffect(() => {
    // Allow direct URL recovery: if session id exists but no in-memory turns,
    // show a recovery guide message instead of hard-failing as "expired".
    if (activeSessionId && turns.length === 0 && !revealData && !isPhase1) {
      setTurns([{ role: "guide", content: text.recoveredSession }]);
    }
  }, [activeSessionId, turns.length, revealData, isPhase1, text.recoveredSession]);

  const handleSend = async () => {
    const msg = input.trim();
    if (!msg || sending || !activeSessionId) return;

    if (isRevealCommand(msg)) {
      handleReveal();
      return;
    }

    setInput("");
    setSending(true);
    setError(null);
    setTurns((prev) => [...prev, { role: "user", content: msg }]);

    try {
      const res = await api.socraticRespond(activeSessionId, msg);
      setRound(res.round);
      setTurns((prev) => [...prev, { role: "guide", content: res.guide_message }]);
    } catch (e) {
      const err = e as ApiError;
      setError(err.detail || text.sendError);
    } finally {
      setSending(false);
    }
  };

  const handleReveal = async () => {
    if (!activeSessionId || revealing) return;
    setRevealing(true);
    setError(null);
    try {
      const data = await api.socraticReveal(activeSessionId);
      setRevealData(data);
    } catch (e) {
      const err = e as ApiError;
      setError(err.detail || text.revealError);
    } finally {
      setRevealing(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  // 方案C: Phase 1 progressive loading UI
  if (isPhase1) {
    const p = phase1Progress;
    const doneCount = p.contributors.length;
    const successCount = p.contributors.filter(c => c.success).length;
    const stageIcon = p.stage === "searching" ? <Search size={16} /> :
      p.stage === "thinking" ? <Users size={16} /> :
      p.stage === "analyzing" ? <Zap size={16} /> :
      p.stage === "guiding" ? <GraduationCap size={16} /> :
      <Loader2 size={16} className="animate-spin" />;

    return (
      <div className="max-w-2xl mx-auto px-4 sm:px-6 flex flex-col items-center justify-center h-full min-h-0">
        <div className="w-full space-y-6">
          {/* Header */}
          <div className="text-center">
            <div className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-oracle-500/10 border border-oracle-500/20 mb-4">
              <GraduationCap size={16} className="text-oracle-400" />
              <span className="text-sm font-medium text-oracle-400">{text.modeLabel}</span>
            </div>
            <p className="text-sm text-zinc-500 line-clamp-2 mt-1">{effectiveQuestion}</p>
          </div>

          {/* Current stage */}
          <div className="glass rounded-2xl p-6 space-y-4">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-oracle-500/15 flex items-center justify-center text-oracle-400">
                {stageIcon}
              </div>
              <div>
                <p className="text-sm font-medium text-zinc-200">{p.detail || text.prepare}</p>
                <p className="text-xs text-zinc-600 mt-0.5">
                  {p.stage === "thinking" && doneCount > 0 ? text.thinking :
                   p.stage === "analyzing" ? text.analyzing :
                   p.stage === "guiding" ? text.guiding : ""}
                </p>
              </div>
            </div>

            {/* Contributor progress */}
            {p.totalContributors > 0 && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-zinc-500">
                  <span>{text.contributorProgress}</span>
                  <span>{doneCount}/{p.totalContributors}{text.expertUnit && ` ${text.expertUnit}`}</span>
                </div>
                <div className="flex gap-1.5">
                  {Array.from({ length: p.totalContributors }).map((_, i) => {
                    const c = p.contributors[i];
                    // BUG-1 fix: when divergence analysis has started (divergenceReady),
                    // remaining empty slots are "skipped" (grey-dash), not still pending.
                    const skipped = !c && p.divergenceReady;
                    return (
                      <div
                        key={i}
                        className={clsx(
                          "flex-1 h-2 rounded-full transition-all duration-500",
                          c ? (c.success ? "bg-oracle-500" : "bg-zinc-600/50")
                            : skipped ? "bg-zinc-700/40"
                            : "bg-surface-3"
                        )}
                      />
                    );
                  })}
                </div>
                {/* Contributor names */}
                <div className="flex flex-wrap gap-1.5">
                  {p.contributors.map((c, i) => (
                    <span key={i} className={clsx(
                      "inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs",
                      c.success ? "bg-oracle-500/10 text-oracle-400" : "bg-zinc-700/50 text-zinc-500"
                    )}>
                      <CheckCircle2 size={10} />
                      {text.expertLabel(i + 1)}
                      <span className="text-zinc-600">{(c.latency_ms / 1000).toFixed(1)}s</span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Divergence ready indicator */}
            {p.divergenceReady && (
              <div className="flex items-center gap-2 pt-2 border-t border-surface-4/30">
                <Target size={12} className="text-oracle-400" />
                <span className="text-xs text-zinc-400">
                  {text.consensus} {(p.overallConsensus * 100).toFixed(0)}%
                  {p.divergenceCount > 0 && ` · ${text.divergenceCount(p.divergenceCount)}`}
                </span>
              </div>
            )}
          </div>

          {/* Cancel button */}
          <div className="text-center">
            <button
              onClick={() => { abortRef.current?.abort(); navigate("/"); }}
              className="text-sm text-zinc-600 hover:text-zinc-400 transition-colors"
            >
              {text.cancel}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // No session data and not loading — missing question/session
  if (!activeSessionId && turns.length === 0 && !revealData && !isPhase1) {
    return (
      <div className="flex flex-col items-center justify-center h-[60vh] gap-4">
        <GraduationCap size={32} className="text-zinc-600" />
        <p className="text-zinc-500 text-sm text-center max-w-xs">
          {text.sessionExpired}<br />
          {text.sessionExpiredHint}
        </p>
        <button
          onClick={() => navigate("/")}
          className="px-5 py-2.5 rounded-xl text-sm font-medium text-surface-0 bg-oracle-500 hover:bg-oracle-400 transition-colors"
        >
          {text.backHome}
        </button>
      </div>
    );
  }

  const roundsInfo = text.roundsInfo(round, maxRounds);
  const isFinished = !!revealData;
  const progress = Math.min(round / Math.max(maxRounds, 1), 1);
  const phase = text.phaseName(round, maxRounds);
  const phaseColor = round === 0 ? "text-zinc-500" : round <= maxRounds * 0.4 ? "text-sky-400" : round <= maxRounds * 0.7 ? "text-oracle-400" : "text-emerald-400";
  const displayQuestion = effectiveQuestion || text.continueQuestion;

  return (
    <div className="max-w-3xl mx-auto px-3 sm:px-6 flex flex-col h-full min-h-0">
      {/* Top bar */}
      <div className="py-3 sm:py-4 border-b border-surface-4/30">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 sm:gap-3 min-w-0">
            <button
              onClick={() => navigate("/")}
              className="p-1.5 rounded-lg text-zinc-500 hover:text-zinc-300 hover:bg-surface-3 transition-colors flex-shrink-0"
            >
              <ArrowLeft size={18} />
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <GraduationCap size={16} className="text-oracle-400 flex-shrink-0" />
                <span className="text-sm font-medium text-oracle-400">{text.modeLabel}</span>
              </div>
              <p className="text-xs sm:text-sm text-zinc-600 mt-0.5 line-clamp-1">
                {displayQuestion}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 sm:gap-3 flex-shrink-0">
            <span className="text-xs text-zinc-600 hidden sm:inline">{roundsInfo}</span>
            {!isFinished && (
              <button
                onClick={handleReveal}
                disabled={revealing}
                className="flex items-center gap-1.5 px-3 sm:px-4 py-2 rounded-full text-sm font-medium bg-oracle-500/15 text-oracle-300 border border-oracle-500/30 hover:bg-oracle-500/25 transition-colors disabled:opacity-50"
              >
                {revealing ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <Eye size={14} />
                )}
                {text.revealAnswer}
              </button>
            )}
          </div>
        </div>

        {/* Thinking progress indicator */}
        {!isFinished && (
          <div className="mt-3 space-y-1.5">
            <div className="flex items-center justify-between text-xs">
              <span className={`flex items-center gap-1 font-medium ${phaseColor}`}>
                <Sparkles size={11} />
                {phase}{text.phaseSuffix}
              </span>
              <span className="text-zinc-600">{round > 0 ? text.roundNumber(round) : text.readyToStart}</span>
            </div>
            <div className="h-1.5 bg-surface-2 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full bg-gradient-to-r from-sky-500 via-oracle-500 to-emerald-500 transition-all duration-700"
                style={{ width: `${Math.max(progress * 100, 5)}%` }}
              />
            </div>
          </div>
        )}

        {/* Consensus info from phase 1 */}
        {divergenceInfo && !isFinished && round === 0 && (
          <div className="mt-2 flex items-center gap-2 text-xs text-zinc-600">
            <Target size={11} className="text-oracle-400" />
            <span>{text.consensus} {((divergenceInfo.overall_consensus ?? 0) * 100).toFixed(0)}%</span>
            {divergenceInfo.divergence_count > 0 && (
              <span>· {text.waitingToExplore(divergenceInfo.divergence_count)}</span>
            )}
          </div>
        )}
      </div>

      {/* Chat messages */}
      <div className="flex-1 overflow-y-auto py-6 space-y-5">
        {turns.map((turn, i) => (
          <ChatBubble key={`${turn.role}-${i}-${turn.content.slice(0, 16)}`} turn={turn} />
        ))}

        {sending && (
          <div className="flex items-center gap-2 text-zinc-500 text-sm pl-11 animate-fade-in">
            <Loader2 size={14} className="animate-spin text-oracle-500" />
            <span>{text.thinkingShort}</span>
          </div>
        )}

        {error && (
          <div className="ml-11 p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm animate-slide-up">
            {error}
          </div>
        )}

        {revealData && <RevealSection data={revealData} />}

        <div ref={chatEndRef} />
      </div>

      {/* Input area */}
      {!isFinished && (
        <div className="py-4 border-t border-surface-4/30">
          <div className="relative glass rounded-xl">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={text.inputPlaceholder}
              maxLength={5000}
              disabled={sending || revealing}
              rows={1}
              className="w-full bg-transparent px-5 pt-4 pb-12 text-base text-zinc-200 placeholder-zinc-600 resize-none focus:outline-none disabled:opacity-50"
            />
            <div className="absolute bottom-3 right-3">
              <button
                onClick={handleSend}
                disabled={!input.trim() || sending}
                className="p-2.5 rounded-lg bg-oracle-500 text-surface-0 hover:bg-oracle-400 disabled:opacity-30 transition-all active:scale-95"
              >
                {sending ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <Send size={16} />
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {isFinished && (
        <div className="py-4 border-t border-surface-4/30 text-center">
          <button
            onClick={() => navigate("/")}
            className="px-6 py-3 rounded-xl text-base font-medium text-surface-0 bg-oracle-500 hover:bg-oracle-400 transition-colors"
          >
            {text.continueAsking}
          </button>
        </div>
      )}
    </div>
  );
}

/* ---------- Sub-components ---------- */

function ChatBubble({ turn }: { turn: ChatTurn }) {
  const isUser = turn.role === "user";
  return (
    <div
      className={clsx(
        "flex gap-3 animate-slide-up",
        isUser ? "flex-row-reverse" : "flex-row",
      )}
    >
      <div
        className={clsx(
          "w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center",
          isUser
            ? "bg-surface-4 text-zinc-400"
            : "bg-oracle-500/20 text-oracle-400",
        )}
      >
        {isUser ? <MessageCircle size={15} /> : <GraduationCap size={15} />}
      </div>

      <div
        className={clsx(
          "max-w-[80%] rounded-2xl px-5 py-4 text-base leading-relaxed",
          isUser
            ? "bg-surface-3 text-zinc-200 rounded-tr-md"
            : "bg-surface-2 border border-surface-4/50 text-zinc-300 rounded-tl-md",
        )}
      >
        <div className="prose-oracle">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={safeMarkdownComponents}>{turn.content}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}

function RevealSection({ data }: { data: SocraticRevealResponse }) {
  const { t } = useTranslation();
  const [showMap, setShowMap] = useState(false);
  const [showCognitive, setShowCognitive] = useState(false);
  const cs = data.cognitive_snapshot;
  const text = {
    revealTitle: t("pages.socratic.reveal.title"),
    divergenceMap: t("pages.socratic.reveal.divergenceMap"),
    divergencePointCount: (count: number) => t("pages.socratic.reveal.divergencePointCount", { count }),
    expertCount: (count: number) => t("pages.socratic.reveal.expertCount", { count }),
    cognitiveSnapshot: t("pages.socratic.reveal.cognitiveSnapshot"),
    reasoningDepth: t("pages.socratic.reveal.reasoningDepth"),
    nuanceRecognition: t("pages.socratic.reveal.nuanceRecognition"),
    anchoringDetected: t("pages.socratic.reveal.anchoringDetected"),
    confirmationBiasDetected: t("pages.socratic.reveal.confirmationBiasDetected"),
    blindSpots: t("pages.socratic.reveal.blindSpots"),
    guidedRounds: (count: number) => t("pages.socratic.progress.guidedRounds", { count }),
  };

  return (
    <div className="mt-6 animate-slide-up">
      {/* Divider */}
      <div className="flex items-center gap-3 mb-6">
        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-oracle-500/30 to-transparent" />
        <span className="text-sm font-medium text-oracle-400 flex items-center gap-1.5">
          <Eye size={14} />
          {text.revealTitle}
        </span>
        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-oracle-500/30 to-transparent" />
      </div>

      {/* Full answer */}
      <div className="glass rounded-2xl p-6 sm:p-8 glow-oracle">
        <div className="prose-oracle text-base leading-relaxed">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={safeMarkdownComponents}>{data.full_answer}</ReactMarkdown>
        </div>
      </div>

      {/* Divergence map */}
      {data.divergence_map?.divergence_points?.length > 0 && (
        <div className="mt-4">
          <button
            onClick={() => setShowMap(!showMap)}
            className="w-full text-left px-5 py-3 rounded-xl glass-hover text-sm text-zinc-400"
          >
            <span className="text-oracle-400 font-medium">{text.divergenceMap}</span>
            {" · "}
            {text.divergencePointCount(data.divergence_map.divergence_points.length)}
          </button>

          {showMap && (
            <div className="mt-2 space-y-2">
              {data.divergence_map.divergence_points.map((point, i) => (
                <div key={i} className="p-5 rounded-xl bg-surface-1 border border-surface-4/30">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-sm font-medium text-zinc-200">{point.topic}</span>
                    <span className="text-xs text-oracle-400/80">{point.difficulty}</span>
                  </div>
                  <p className="text-sm text-zinc-300 mb-3">{point.description}</p>
                  {point.positions.map((pos, j) => (
                    <div key={j} className="ml-3 mb-2 pl-3 border-l border-surface-5 text-sm">
                      <span className="text-zinc-200 font-medium">{pos.stance}</span>
                      {pos.models?.length > 0 && (
                        <span className="text-zinc-400"> ({text.expertCount(pos.models.length)})</span>
                      )}
                      <p className="text-zinc-300 mt-0.5">{pos.summary}</p>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Cognitive snapshot */}
      {cs && (
        <div className="mt-4">
          <button
            onClick={() => setShowCognitive(!showCognitive)}
            className="w-full text-left px-5 py-3 rounded-xl glass-hover text-sm text-zinc-400"
          >
            <span className="text-oracle-400 font-medium flex items-center gap-1.5">
              <Brain size={14} />
              {text.cognitiveSnapshot}
            </span>
          </button>

          {showCognitive && (
            <div className="mt-2 p-5 rounded-xl bg-surface-1 border border-surface-4/30 space-y-3">
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-zinc-400">{text.reasoningDepth}</span>
                  <div className="text-zinc-200 font-medium">{(cs.reasoning_depth * 100).toFixed(0)}%</div>
                </div>
                <div>
                  <span className="text-zinc-400">{text.nuanceRecognition}</span>
                  <div className="text-zinc-200 font-medium">{(cs.nuance_recognition * 100).toFixed(0)}%</div>
                </div>
              </div>
              {cs.anchoring_detected && (
                <p className="text-sm text-amber-400/80">{text.anchoringDetected}</p>
              )}
              {cs.confirmation_bias && (
                <p className="text-sm text-amber-400/80">{text.confirmationBiasDetected}</p>
              )}
              {cs.blind_spots.length > 0 && (
                <div>
                  <span className="text-sm text-zinc-300">{text.blindSpots}</span>
                  <ul className="mt-1 space-y-1">
                    {cs.blind_spots.map((bs, i) => (
                      <li key={i} className="text-sm text-zinc-200 pl-3 border-l-2 border-amber-500/30">{bs}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <div className="mt-4 text-center text-sm text-zinc-600">
        {text.guidedRounds(data.guide_rounds_used)}
      </div>
    </div>
  );
}
