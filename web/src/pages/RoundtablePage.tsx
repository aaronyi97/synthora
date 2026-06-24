/**
 * RoundtablePage — Dispute-driven multi-model decision engine (v2.2.2).
 *
 * Batch 2B+3: S1 my_dimensions + blind_spot_warning, S2 synthesized dims +
 *   echo_chamber_warning + clarifying_questions + dispute_type[] tags,
 *   S3 debate cards (main_debater/reviewer), Decision Point B,
 *   S4 conclusion_type color coding + value_disputes_to_user + stance_evolution.
 */
import { useState, useRef, useEffect, useCallback } from "react";
import { useNavigate, useLocation, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Users, ArrowLeft, Loader2, CheckCircle, AlertCircle,
  ChevronDown, ChevronUp, Zap, Shield, MessageSquarePlus,
  Swords, Eye,
} from "lucide-react";
import { api } from "@/api/client";
import i18n from "@/i18n";

// ── Types ──────────────────────────────────────────────────

interface Claim { point: string; evidence: string; dimension: string; }

interface ExpertOpinionResult {
  model_id: string;
  label: string;
  stance: string;
  confidence: number;
  my_dimensions: string[];
  claims: Claim[];
  risk_warning: string;
  blind_spot_warning: string;
  challenge_to_others: string;
  raw_response: string;
  structured: boolean;
  success: boolean;
  error: string;
  latency_ms: number;
}

interface ExpertOpinionData extends ExpertOpinionResult {
  done_count: number;
  total_count: number;
}

interface RebuttalData {
  model_id: string; label: string; role: string;
  target_dispute: string; response_type: string;
  response: string; new_evidence: string; revised_stance: string;
  stance_changed: boolean; confidence: number;
  structured: boolean; success: boolean; latency_ms: number;
  done_count: number; total_count: number;
}

interface ContentionSide {
  position: string; supporting_claims: string[];
  lead_expert: string; main_argument: string;
}
interface ContentionPoint {
  topic: string; severity: string; dispute_type: string[];
  factual_aspect: string; value_aspect: string;
  dimension_id: string; dimension_label: string; dimension_aliases: string[];
  adjudication_note: string;
  sides: ContentionSide[]; why_it_matters: string; suggested_focus: boolean;
}
interface ConsensusPoint { point: string; strength: string; agreed_by: string[]; }
interface DisputeMapData {
  synthesized_dimensions: string[];
  dimension_sources: Record<string, string[]>;
  contention_points: ContentionPoint[];
  consensus_points: ConsensusPoint[];
  suggested_focus: string;
  echo_chamber_warning: string;
  clarifying_questions: string[];
}

interface DecisionOption {
  choice: string; pros: string[]; cons: string[];
  best_when: string; risk: string; mitigation: string;
}
interface UnresolvedItem { point: string; reason: string; how_to_resolve: string; }
interface ValueDisputeItem { point: string; dimension_id: string; ask_user: string; }
interface StanceEvoItem { expert: string; r1_stance: string; final_stance: string; changed: boolean; changed_reason: string; }
interface DecisionPacketData {
  conclusion_type: string;   // "recommendation" | "conditional" | "draft"
  confidence_basis: string;
  final_summary: string;
  stance_evolution: StanceEvoItem[];
  options: DecisionOption[];
  unresolved: UnresolvedItem[];
  what_changes_my_mind: string;
  recommended_action: string;
  value_disputes_to_user: ValueDisputeItem[];
  echo_chamber_flag: boolean;
  degraded: boolean;
  degradation_reason: string;
  total_latency_ms: number;
  estimated_cost_usd: number;
}

interface RoundtableResultData {
  session_id: string;
  question: string;
  rounds_completed: number;
  experts: ExpertOpinionResult[];
  dispute_map: DisputeMapData;
  decision_packet: DecisionPacketData;
}

interface ResumeStateSnapshot {
  question?: string;
  expert_count?: number;
  experts?: ExpertOpinionResult[];
  dispute_map?: DisputeMapData | null;
  rebuttals?: RebuttalData[];
  debate_round?: number;
  choice_point?: string | null;
}

type Phase =
  | "idle" | "s1_experts" | "s2_disputes"
  | "awaiting_choice_a" | "s3_debate" | "awaiting_choice_b"
  | "s4_decision" | "done" | "error";

const ROUNDTABLE_SESSION_KEY = "roundtable_session_id";
const ROUNDTABLE_QUESTION_KEY = "roundtable_question";
const ROUNDTABLE_INTERACTIVE = import.meta.env.MODE === "test";

function disputeTypeLabel(type: string): string {
  switch (type) {
    case "factual":
      return i18n.t("pages.roundtable.types.disputeType.factual");
    case "assumption":
      return i18n.t("pages.roundtable.types.disputeType.assumption");
    case "value":
      return i18n.t("pages.roundtable.types.disputeType.value");
    case "priority":
      return i18n.t("pages.roundtable.types.disputeType.priority");
    default:
      return type;
  }
}

function consensusStrengthLabel(strength: string): string {
  switch (strength) {
    case "strong":
      return i18n.t("pages.roundtable.types.consensusStrength.strong");
    case "moderate":
      return i18n.t("pages.roundtable.types.consensusStrength.moderate");
    default:
      return i18n.t("pages.roundtable.types.consensusStrength.weak");
  }
}

function isRoundtableDisconnectMessage(message: string): boolean {
  const normalized = message.toLowerCase();
  return message.includes(i18n.getFixedT("zh-CN")("api.client.errors.roundtable.connectionInterrupted"))
    || normalized.includes(i18n.getFixedT("en-US")("api.client.errors.roundtable.connectionInterrupted").toLowerCase());
}

function phaseFromSessionState(state: string | null | undefined): Phase {
  switch (state) {
    case "collecting":
    case "initializing":
      return "s1_experts";
    case "mapping":
      return "s2_disputes";
    case "awaiting_A":
      return "awaiting_choice_a";
    case "debating":
      return "s3_debate";
    case "awaiting_B":
      return "awaiting_choice_b";
    case "drafting":
      return "s4_decision";
    default:
      return "idle";
  }
}

function getChoiceErrorMessage(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) {
      if (detail === "session_ended") return i18n.t("pages.roundtable.errors.sessionEnded");
      return detail;
    }
    if (detail && typeof detail === "object") {
      const mismatch = detail as { error?: string; current_state?: string };
      if (mismatch.error === "choice_point_mismatch") {
        return i18n.t("pages.roundtable.errors.phaseChanged", {
          state: mismatch.current_state || "unknown",
        });
      }
    }
  }
  return i18n.t("pages.roundtable.errors.submitChoiceFailed");
}

function isSessionResumeExpiredError(error: unknown): boolean {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    return detail === "forbidden" || detail === "session_ended";
  }
  return false;
}

function emptyDisputeMap(): DisputeMapData {
  return {
    synthesized_dimensions: [],
    dimension_sources: {},
    contention_points: [],
    consensus_points: [],
    suggested_focus: "",
    echo_chamber_warning: "",
    clarifying_questions: [],
  };
}

// ── Main page ──────────────────────────────────────────────

export default function RoundtablePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const text = {
    unsuitablePrefix: t("pages.roundtable.errors.unsuitablePrefix"),
    autoDraftRecovered: t("pages.roundtable.errors.autoDraftRecovered"),
    sessionExpiredRestarted: t("pages.roundtable.errors.sessionExpiredRestarted"),
    sessionRestoreFailedRestarted: t("pages.roundtable.errors.sessionRestoreFailedRestarted"),
    offlineNotice: t("pages.roundtable.errors.offlineNotice"),
    startFailed: t("pages.roundtable.errors.startFailed"),
    actionFailed: t("pages.roundtable.errors.actionFailed"),
    retry: t("common.actions.retry"),
  };

  const qParam = searchParams.get("q") || "";
  const sessionIdParam = searchParams.get("session_id") || "";
  const stateQuestion = (location.state as { q?: string; question?: string } | null)?.q
    || (location.state as { q?: string; question?: string } | null)?.question
    || "";
  const stateAutoStart = (location.state as { autoStart?: boolean } | null)?.autoStart === true;
  const bootQuestion = stateQuestion || qParam;
  const bootSignature = `${sessionIdParam}|${bootQuestion}|${stateAutoStart ? "1" : "0"}`;

  const [question, setQuestion] = useState(bootQuestion);
  const bootConsumedRef = useRef<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [experts, setExperts] = useState<ExpertOpinionData[]>([]);
  const [totalExperts, setTotalExperts] = useState(0);
  const [disputeMap, setDisputeMap] = useState<DisputeMapData | null>(null);
  const [rebuttals, setRebuttals] = useState<RebuttalData[]>([]);
  const [debateRound, setDebateRound] = useState(0);
  const [choicePoint, setChoicePoint] = useState<"A" | "B">("A");
  const [result, setResult] = useState<RoundtableResultData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [choiceSubmitting, setChoiceSubmitting] = useState(false);
  const [injectResetToken, setInjectResetToken] = useState(0);
  const [isOnline, setIsOnline] = useState(
    () => (typeof navigator === "undefined" ? true : navigator.onLine),
  );
  const abortRef = useRef<AbortController | null>(null);
  const sessionIdRef = useRef<string>("");
  const phaseRef = useRef<Phase>("idle");
  const reconnectingRef = useRef(false);
  const resumeRoundtableRef = useRef<(sessionId: string) => Promise<boolean>>(async () => false);
  phaseRef.current = phase;

  function getStoredQuestion() {
    return sessionStorage.getItem(ROUNDTABLE_QUESTION_KEY)?.trim() || "";
  }

  function saveStoredQuestion(nextQuestion: string) {
    const normalized = nextQuestion.trim();
    if (normalized) {
      sessionStorage.setItem(ROUNDTABLE_QUESTION_KEY, normalized);
    }
  }

  function clearStoredSessionId(options?: { keepQuestion?: boolean }) {
    sessionIdRef.current = "";
    sessionStorage.removeItem(ROUNDTABLE_SESSION_KEY);
    if (!options?.keepQuestion) {
      sessionStorage.removeItem(ROUNDTABLE_QUESTION_KEY);
    }
  }

  function saveSessionId(sessionId: string, nextQuestion?: string) {
    sessionIdRef.current = sessionId;
    sessionStorage.setItem(ROUNDTABLE_SESSION_KEY, sessionId);
    if (nextQuestion) {
      saveStoredQuestion(nextQuestion);
    }
  }

  const buildRoundtableResult = useCallback((
    sessionId: string,
    restoredQuestion: string,
    decisionPacket: DecisionPacketData,
  ): RoundtableResultData => ({
      session_id: sessionId,
      question: restoredQuestion,
      rounds_completed: debateRound,
      experts,
      dispute_map: disputeMap ?? emptyDisputeMap(),
      decision_packet: decisionPacket,
    }),
  [debateRound, disputeMap, experts]);

  const connectRoundtableStream = useCallback(async (
    streamQuestion: string,
    options?: { sessionId?: string; preserveState?: boolean },
  ) => {
    const { sessionId, preserveState = false } = options ?? {};
    const ac = new AbortController();
    abortRef.current?.abort();
    abortRef.current = ac;

    const maybeReconnectAfterDisconnect = async (msg: string): Promise<boolean> => {
      const activePhase = phaseRef.current;
      const activeSessionId = sessionIdRef.current || sessionId || "";
      const shouldReconnect = isRoundtableDisconnectMessage(msg)
        && (activePhase === "awaiting_choice_a" || activePhase === "awaiting_choice_b")
        && !!activeSessionId
        && !reconnectingRef.current;
      if (!shouldReconnect) {
        return false;
      }
      reconnectingRef.current = true;
      try {
        setError(null);
        return await resumeRoundtableRef.current(activeSessionId);
      } finally {
        reconnectingRef.current = false;
      }
    };

    try {
      await api.roundtableStream(
        {
          question: streamQuestion,
          session_id: sessionId,
        },
        {
          onStarted: (d: { session_id: string; expert_count: number; question: string }) => {
            saveSessionId(d.session_id, d.question || streamQuestion);
            setTotalExperts(d.expert_count);
            if (!preserveState) {
              phaseRef.current = "s1_experts";
              setPhase("s1_experts");
            }
          },
          onExpertDone: (d: ExpertOpinionData) =>
            setExperts((prev) => [...prev, d]),
          onDisputesMapped: (d: DisputeMapData) => {
            setDisputeMap(d);
            phaseRef.current = "s2_disputes";
            setPhase("s2_disputes");
          },
          onAwaitingUserChoice: (d: { choice_point: string; timeout_s: number }) => {
            const nextPhase = d.choice_point === "B" ? "awaiting_choice_b" : "awaiting_choice_a";
            setChoicePoint(d.choice_point === "B" ? "B" : "A");
            phaseRef.current = nextPhase;
            setPhase(nextPhase);
          },
          onDebateStarted: (d: { round: number }) => {
            setDebateRound(d.round);
            phaseRef.current = "s3_debate";
            setPhase("s3_debate");
          },
          onRebuttalDone: (d: RebuttalData) =>
            setRebuttals((prev) => [...prev, d]),
          onDebateComplete: () => { /* phase will switch via onAwaitingUserChoice */ },
          onModeratorStarted: () => {
            phaseRef.current = "s4_decision";
            setPhase("s4_decision");
          },
          onComplete: (d: RoundtableResultData) => {
            clearStoredSessionId();
            setResult(d);
            setNotice(null);
            phaseRef.current = "done";
            setPhase("done");
          },
          onAutoDraft: (d: { decision_packet: DecisionPacketData; message: string }) => {
            const activeSessionId = sessionIdRef.current || sessionId || "";
            clearStoredSessionId();
            setError(null);
            setNotice(d.message);
            setResult(buildRoundtableResult(activeSessionId, streamQuestion, d.decision_packet));
            phaseRef.current = "done";
            setPhase("done");
          },
          onError: (msg: string) => {
            void (async () => {
              if (await maybeReconnectAfterDisconnect(msg)) {
                return;
              }
              setError(msg);
              if (!preserveState) {
                phaseRef.current = "error";
                setPhase("error");
              }
            })();
          },
        },
        ac.signal,
      );
    } catch (e) {
      if (!ac.signal.aborted) {
        const errorMessage = e instanceof Error ? e.message : String(e);
        if (await maybeReconnectAfterDisconnect(errorMessage)) {
          return;
        }
        setError(errorMessage);
        if (!preserveState) {
          phaseRef.current = "error";
          setPhase("error");
        }
      }
    }
  }, [buildRoundtableResult]);

  const applyResumedState = useCallback((
    sessionId: string,
    state: string | null | undefined,
    snapshot?: ResumeStateSnapshot | null,
  ) => {
    const restoredQuestion = snapshot?.question?.trim() || qParam;
    saveSessionId(sessionId, restoredQuestion);
    const restoredExperts = snapshot?.experts ?? [];
    const restoredTotalExperts = Math.max(snapshot?.expert_count ?? 0, restoredExperts.length);
    const restoredRebuttals = snapshot?.rebuttals ?? [];
    const restoredChoicePoint = (snapshot?.choice_point ?? (state === "awaiting_B" ? "B" : "A")) === "B" ? "B" : "A";

    setQuestion(restoredQuestion);
    setExperts(
      restoredExperts.map((expert, index) => ({
        ...expert,
        done_count: Math.min(index + 1, restoredTotalExperts || index + 1),
        total_count: restoredTotalExperts || restoredExperts.length || 0,
      })),
    );
    setTotalExperts(restoredTotalExperts);
    setDisputeMap(snapshot?.dispute_map ?? null);
    setRebuttals(
      restoredRebuttals.map((rebuttal, index) => ({
        ...rebuttal,
        done_count: index + 1,
        total_count: restoredRebuttals.length,
      })),
    );
    setDebateRound(snapshot?.debate_round ?? 0);
    setChoicePoint(restoredChoicePoint);
    setResult(null);
    setError(null);
    const nextPhase = phaseFromSessionState(state);
    phaseRef.current = nextPhase;
    setPhase(nextPhase);
  }, [qParam]);

  const startRoundtable = useCallback(async (
    inputQuestion?: string,
    options?: { preserveNotice?: boolean },
  ) => {
    const q = (inputQuestion ?? question).trim();
    if (!q) return;
    setQuestion(q);
    abortRef.current?.abort();
    clearStoredSessionId();
    saveStoredQuestion(q);
    setExperts([]);
    setDisputeMap(null);
    setRebuttals([]);
    setDebateRound(0);
    setResult(null);
    setError(null);
    if (!options?.preserveNotice) {
      setNotice(null);
    }
    setTotalExperts(0);
    phaseRef.current = "s1_experts";
    setPhase("s1_experts");

    // P1-A: Route guard — check suitability before starting stream
    try {
      const { suitability, reason } = await api.roundtableCheck(q);
      if (suitability === "low") {
        setError(`${text.unsuitablePrefix}${reason}`);
        setPhase("error");
        return;
      }
    } catch { /* guard failure is non-fatal — proceed */ }

    await connectRoundtableStream(q);
  }, [connectRoundtableStream, question, text.unsuitablePrefix]);

  const resumeRoundtable = useCallback(async (sessionId: string): Promise<boolean> => {
    try {
      const resumed = await api.roundtableResume(sessionId);
      if (resumed.status === "session_active") {
        const restoredQuestion =
          (resumed as { state_snapshot?: ResumeStateSnapshot | null }).state_snapshot?.question?.trim()
          || qParam;
        applyResumedState(
          resumed.session_id || sessionId,
          resumed.state,
          (resumed as { state_snapshot?: ResumeStateSnapshot | null }).state_snapshot ?? null,
        );
        if (resumed.state && resumed.state !== "idle") {
          void connectRoundtableStream(restoredQuestion, {
            sessionId: resumed.session_id || sessionId,
            preserveState: true,
          });
        }
        return true;
      }
      if (resumed.status === "auto_draft_available" && resumed.decision_packet) {
        const restoredQuestion = getStoredQuestion() || qParam || question;
        clearStoredSessionId();
        setQuestion(restoredQuestion);
        setError(null);
        setNotice(text.autoDraftRecovered);
        setResult(buildRoundtableResult(
          resumed.session_id || sessionId,
          restoredQuestion,
          resumed.decision_packet as unknown as DecisionPacketData,
        ));
        setPhase("done");
        return true;
      }
      clearStoredSessionId({ keepQuestion: true });
    } catch (resumeError) {
      const fallbackQuestion = getStoredQuestion() || qParam || question;
      clearStoredSessionId({ keepQuestion: true });
      if (fallbackQuestion) {
        const noticeMessage = isSessionResumeExpiredError(resumeError)
          ? text.sessionExpiredRestarted
          : text.sessionRestoreFailedRestarted;
        setNotice(noticeMessage);
        await startRoundtable(fallbackQuestion, { preserveNotice: true });
        return true;
      }
    }
    return false;
  }, [
    applyResumedState,
    buildRoundtableResult,
    connectRoundtableStream,
    qParam,
    question,
    startRoundtable,
    text.autoDraftRecovered,
    text.sessionExpiredRestarted,
    text.sessionRestoreFailedRestarted,
  ]);
  resumeRoundtableRef.current = resumeRoundtable;

  useEffect(() => {
    if (bootConsumedRef.current === bootSignature) return;
    const boot = async () => {
      bootConsumedRef.current = bootSignature;
      const shouldStartFreshFromUrl = Boolean(qParam) && !sessionIdParam;
      const existingSessionId = sessionIdParam || (shouldStartFreshFromUrl ? "" : sessionStorage.getItem(ROUNDTABLE_SESSION_KEY)) || "";
      if (existingSessionId) {
        const resumed = await resumeRoundtable(existingSessionId);
        if (resumed) {
          return;
        }
      }
      if (bootQuestion) {
        startRoundtable(bootQuestion);
      }
    };
    void boot();
  }, [bootQuestion, bootSignature, qParam, resumeRoundtable, sessionIdParam, startRoundtable, stateAutoStart]);

  useEffect(() => {
    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  function handleUserChoice(
    action: "deepen" | "conclude" | "inject",
    userInput?: string,
    cp?: "A" | "B",
  ) {
    if (!ROUNDTABLE_INTERACTIVE) return;
    const sid = sessionIdRef.current;
    if (!sid || choiceSubmitting) return;
    const previousPhase = phase;
    const previousChoicePoint = choicePoint;
    setError(null);
    setChoiceSubmitting(true);
    const effectiveCp = cp ?? choicePoint;
    if (action !== "inject") {
      setPhase(
        action === "conclude" || (effectiveCp === "B" && action !== "deepen")
          ? "s4_decision"
          : "s3_debate",
      );
    }
    const idempotencyKey = crypto.randomUUID();
    void (async () => {
      try {
        await api.roundtableChoice(sid, effectiveCp, action, userInput, idempotencyKey);
        if (action === "inject") {
          setInjectResetToken((prev) => prev + 1);
        }
      } catch (choiceError) {
        const choiceErrorMessage = getChoiceErrorMessage(choiceError);
        const resumed = await resumeRoundtable(sid);
        setError(choiceErrorMessage);
        if (!resumed) {
          setPhase(previousPhase);
          setChoicePoint(previousChoicePoint);
        }
      } finally {
        setChoiceSubmitting(false);
      }
    })();
  }

  const isAwaiting = phase === "awaiting_choice_a" || phase === "awaiting_choice_b";
  const showDisputeSpotlight = disputeMap && phase !== "idle" && phase !== "s1_experts" && phase !== "error";
  const hasEvidence = experts.length > 0 && phase !== "idle" && phase !== "s1_experts";

  return (
    <div className="h-full overflow-y-auto max-w-3xl mx-auto px-4 py-8 text-zinc-200">
      {!isOnline && (
        <div className="mb-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-200">
          {text.offlineNotice}
        </div>
      )}
      {notice && (
        <div className="mb-4 rounded-xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-xs text-sky-200">
          {notice}
        </div>
      )}

      {/* ── Header ── */}
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={() => navigate(-1)}
          className="flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-300 transition-colors"
        >
          <ArrowLeft className="w-4 h-4" /> {t("pages.roundtable.actions.back")}
        </button>
        <div className="flex items-center gap-2">
          <Users className="w-5 h-5 text-oracle-400" />
          <h1 className="text-base font-semibold text-zinc-100">{t("pages.roundtable.header.title")}</h1>
        </div>
      </div>

      {/* ── Question input (idle) ── */}
      {phase === "idle" && (
        <div className="mb-6">
          <textarea
            className="w-full rounded-xl border border-zinc-700 bg-zinc-900 px-4 py-3 text-sm text-zinc-200 placeholder-zinc-600 resize-none focus:outline-none focus:border-zinc-500 min-h-[80px]"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={t("pages.roundtable.header.questionPlaceholder")}
          />
          <button
            onClick={() => startRoundtable()}
            disabled={!question.trim()}
            className="mt-2 rounded-xl bg-oracle-500/20 border border-oracle-500/40 px-5 py-2 text-sm text-oracle-300 hover:bg-oracle-500/30 disabled:opacity-40 transition-colors"
          >
            {t("pages.roundtable.actions.start")}
          </button>
        </div>
      )}

      {/* ── Layer 0: Phase Header ── */}
      {phase !== "idle" && (
        <PhaseHeader phase={phase} question={question} disputeMap={disputeMap}
          experts={experts} totalExperts={totalExperts} />
      )}

      {/* ── Error ── */}
      {error && (
        <div className="mb-4 flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <div>
            <p className="font-medium mb-0.5">{phase === "error" ? text.startFailed : text.actionFailed}</p>
            <p className="text-sm opacity-80">{error}</p>
            {phase === "error" && (
              <button onClick={() => startRoundtable()} className="mt-2 text-sm underline opacity-70 hover:opacity-100">{text.retry}</button>
            )}
          </div>
        </div>
      )}

      {/* ── Decision Packet (done, top, most prominent) ── */}
      {phase === "done" && result?.decision_packet && (
        <DecisionPacketPanel
          packet={result.decision_packet}
          onRestart={() => { clearStoredSessionId(); setPhase("idle"); setExperts([]); setResult(null); setDisputeMap(null); setRebuttals([]); }}
        />
      )}

      {/* ── Layer 2: Core Dispute Spotlight ── */}
      {showDisputeSpotlight && (
        <CoreDisputeSpotlight
          disputeMap={disputeMap!}
          showValueGuide={ROUNDTABLE_INTERACTIVE && phase === "awaiting_choice_a"}
        />
      )}

      {/* ── Layer 3: User Action Panel ── */}
      {ROUNDTABLE_INTERACTIVE && isAwaiting && (
        <UserActionPanel
          choicePoint={choicePoint}
          onChoice={handleUserChoice}
          choiceSubmitting={choiceSubmitting}
          injectResetToken={injectResetToken}
        />
      )}

      {/* ── Layer 4: Evidence (collapsed) ── */}
      {hasEvidence && (
        <EvidenceLayer experts={experts} disputeMap={disputeMap} rebuttals={rebuttals} />
      )}

    </div>
  );
}

// ── ExpertStanceCard ───────────────────────────────────────

function confidenceDots(conf: number) {
  const filled = Math.round(conf * 5);
  return Array.from({ length: 5 }, (_, i) => (
    <span key={i} className={i < filled ? "text-oracle-400" : "text-zinc-700"}>●</span>
  ));
}

function pickFocalDispute(disputeMap: DisputeMapData) {
  return (
    disputeMap.contention_points.find((cp) => cp.suggested_focus)
    || disputeMap.contention_points.find((cp) => cp.severity === "high")
    || disputeMap.contention_points[0]
    || null
  );
}

function decisionConfidenceScore(packet: DecisionPacketData) {
  const base =
    packet.conclusion_type === "recommendation"
      ? 5
      : packet.conclusion_type === "conditional"
        ? 3
        : 2;
  return packet.degraded ? Math.max(1, base - 1) : base;
}

function decisionConfidenceDots(score: number) {
  return Array.from({ length: 5 }, (_, i) => (
    <span key={i} className={i < score ? "text-oracle-300" : "text-zinc-600"}>{i < score ? "●" : "○"}</span>
  ));
}

function ExpertStanceCard({ expert, index }: { expert: ExpertOpinionData; index: number }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const hasStructured = expert.structured && expert.stance;

  return (
    <div className={`rounded-xl border transition-colors ${
      expert.success
        ? "border-zinc-700/60 bg-zinc-900/50"
        : "border-red-500/20 bg-red-500/5"
    }`}>
      <button
        onClick={() => setExpanded((v) => !v)}
        aria-label={`${expanded ? t("pages.roundtable.dispute.collapse") : t("pages.roundtable.dispute.expand")} ${expert.label} ${t("pages.roundtable.dispute.stance")}`}
        aria-expanded={expanded}
        className="w-full flex items-center gap-3 px-4 py-3 text-left"
      >
        <div className="w-6 h-6 rounded-full bg-zinc-800 border border-zinc-700 flex items-center justify-center text-xs text-zinc-400 shrink-0 font-mono">
          {index + 1}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-sm font-medium text-zinc-200">{expert.label}</p>
            {hasStructured && (
              <span className="text-xs text-oracle-300 bg-oracle-500/10 border border-oracle-500/20 rounded px-1.5 py-0.5 truncate max-w-[200px]">
                {expert.stance}
              </span>
            )}
          </div>
          {expert.my_dimensions?.length > 0 && (
            <div className="mt-1 flex gap-1 flex-wrap">
              {expert.my_dimensions.slice(0, 4).map((d, i) => (
                <span key={i} className="text-xs border border-zinc-700/60 rounded px-1.5 py-0.5 text-zinc-400">
                  {d}
                </span>
              ))}
            </div>
          )}
        </div>
        {hasStructured && (
          <span className="text-xs flex gap-0.5 shrink-0">{confidenceDots(expert.confidence)}</span>
        )}
        {!expert.success && <span className="text-xs text-red-400 shrink-0">{t("pages.roundtable.dispute.failed")}</span>}
        {expanded ? <ChevronUp className="w-3.5 h-3.5 text-zinc-600 shrink-0" /> : <ChevronDown className="w-3.5 h-3.5 text-zinc-600 shrink-0" />}
      </button>

      {expanded && expert.success && (
        <div className="px-4 pb-4 border-t border-zinc-800">
          {hasStructured && expert.claims.length > 0 && (
            <div className="mb-3 pt-3">
              <p className="text-xs text-zinc-400 mb-1.5">{t("pages.roundtable.dispute.keyArguments")}</p>
              <ul className="space-y-1.5">
                {expert.claims.map((c, i) => (
                  <li key={i} className="text-sm text-zinc-300 leading-relaxed">
                    {c.dimension && (
                      <span className="text-xs text-zinc-500 border border-zinc-700 rounded px-1 mr-1.5">{c.dimension}</span>
                    )}
                    {c.point}
                    {c.evidence && <span className="text-zinc-500 ml-1">— {c.evidence}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {expert.risk_warning && (
            <p className="text-sm text-amber-400/80 flex gap-1.5 items-start mb-2 pt-2">
              <span className="shrink-0">⚠️</span>{expert.risk_warning}
            </p>
          )}
          {expert.blind_spot_warning && (
            <p className="text-sm text-purple-400/80 flex gap-1.5 items-start mb-2">
              <Eye className="w-3.5 h-3.5 shrink-0 mt-0.5" />{expert.blind_spot_warning}
            </p>
          )}
          {expert.challenge_to_others && (
            <p className="text-sm text-zinc-400 flex gap-1.5 items-start mb-2">
              <span className="shrink-0">🎯</span>{expert.challenge_to_others}
            </p>
          )}
          {!hasStructured && expert.raw_response && (
            <p className="pt-3 text-sm text-zinc-300 leading-relaxed whitespace-pre-wrap">{expert.raw_response}</p>
          )}
        </div>
      )}
      {expanded && !expert.success && (
        <div className="px-4 pb-3 text-sm text-red-400/70 border-t border-zinc-800 pt-3">
          {t("pages.roundtable.dispute.expertTimedOut")}
        </div>
      )}
    </div>
  );
}

// ── RebuttalCard ───────────────────────────────────────────

function RebuttalCard({ rebuttal }: { rebuttal: RebuttalData }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const roleIcon = rebuttal.role === "main_debater" ? "⚔️" : "👁";
  const responseTypeLabel: Record<string, string> = {
    concede: t("pages.roundtable.responseTypes.concede"),
    rebut: t("pages.roundtable.responseTypes.rebut"),
    expand: t("pages.roundtable.responseTypes.expand"),
    maintain: t("pages.roundtable.responseTypes.maintain"),
  };

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/40">
      <button
        onClick={() => setExpanded((v) => !v)}
        aria-label={`${expanded ? t("pages.roundtable.dispute.collapse") : t("pages.roundtable.dispute.expand")} ${rebuttal.label} ${t("pages.roundtable.dispute.debateRecord")}`}
        aria-expanded={expanded}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
      >
        <span className="text-xs shrink-0">{roleIcon}</span>
        <span className="text-sm font-medium text-zinc-300">{rebuttal.label}</span>
        {rebuttal.stance_changed && (
          <span className="text-xs border border-amber-500/30 text-amber-400 rounded px-1 shrink-0">{t("pages.roundtable.badges.stanceChanged")}</span>
        )}
        {rebuttal.response_type && (
          <span className="text-xs border border-zinc-700/60 text-zinc-500 rounded px-1 shrink-0">
            {responseTypeLabel[rebuttal.response_type] ?? rebuttal.response_type}
          </span>
        )}
        <span className="ml-auto" />
        {expanded ? <ChevronUp className="w-3.5 h-3.5 text-zinc-700 shrink-0" /> : <ChevronDown className="w-3.5 h-3.5 text-zinc-700 shrink-0" />}
      </button>
      {expanded && (
        <div className="px-3 pb-3 border-t border-zinc-800 pt-2 space-y-1.5">
          {rebuttal.target_dispute && (
            <p className="text-xs text-zinc-500">{t("pages.roundtable.dispute.topicPrefix")}{rebuttal.target_dispute}</p>
          )}
          {rebuttal.response && (
            <p className="text-sm text-zinc-300 leading-relaxed">{rebuttal.response}</p>
          )}
          {rebuttal.new_evidence && (
            <p className="text-sm text-blue-400/70 flex gap-1.5">
              <span className="shrink-0">📎</span>{rebuttal.new_evidence}
            </p>
          )}
          {rebuttal.revised_stance && (
            <p className="text-sm text-amber-400/70 flex gap-1.5">
              <span className="shrink-0">↪</span>{rebuttal.revised_stance}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ── DisputeMapPanel ────────────────────────────────────────

/* DisputeMapPanel removed — replaced by CoreDisputeSpotlight + UserActionPanel + FullDisputeMap */

// ── DecisionPacketPanel ────────────────────────────────────

function getConclusionStyles(): Record<string, { border: string; badge: string; label: string }> {
  return {
    recommendation: {
      border: "border-oracle-500/40",
      badge: "border-oracle-500/40 text-oracle-300 bg-oracle-500/10",
      label: i18n.t("pages.roundtable.decision.recommendation"),
    },
    conditional: {
      border: "border-amber-500/30",
      badge: "border-amber-500/30 text-amber-300 bg-amber-500/10",
      label: i18n.t("pages.roundtable.decision.conditional"),
    },
    draft: {
      border: "border-zinc-600/40",
      badge: "border-zinc-600/40 text-zinc-400 bg-zinc-800/40",
      label: i18n.t("pages.roundtable.decision.draft"),
    },
  };
}

function DecisionPacketPanel({
  packet, onRestart,
}: {
  packet: DecisionPacketData;
  onRestart: () => void;
}) {
  const { t } = useTranslation();
  const conclusionStyles = getConclusionStyles();
  const style = conclusionStyles[packet.conclusion_type] ?? conclusionStyles.draft;
  const confidenceScore = decisionConfidenceScore(packet);
  const keyReasons = packet.options
    ?.map((opt) => opt.best_when?.trim())
    .filter((reason): reason is string => Boolean(reason))
    .slice(0, 3) ?? [];

  return (
    <div className={`mb-6 rounded-2xl border ${style.border} bg-zinc-900/75 px-6 py-5 shadow-[0_18px_60px_rgba(0,0,0,0.28)]`}>
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <CheckCircle className="w-4 h-4 text-oracle-400" />
        <span className="text-sm font-semibold text-zinc-200">{t("pages.roundtable.decision.title")}</span>
        <span className={`text-xs border rounded px-1.5 py-0.5 ${style.badge}`}>
          {style.label}
        </span>
        {packet.echo_chamber_flag && (
          <span className="text-xs border border-amber-500/30 text-amber-400 bg-amber-500/5 rounded px-1.5 py-0.5">
            {t("pages.roundtable.badges.echoChamberRisk")}
          </span>
        )}
        {packet.degraded && (
          <span className="text-xs border border-red-500/30 text-red-400 bg-red-500/5 rounded px-1.5 py-0.5">
            {t("pages.roundtable.decision.degradedPrefix")}{packet.degradation_reason}
          </span>
        )}
      </div>

      {/* Recommended action */}
      {packet.recommended_action && (
        <div className={`rounded-xl border ${style.border} bg-oracle-500/[0.06] px-4 py-4 mb-4`}>
          <p className="text-xs text-oracle-400 mb-2">{t("pages.roundtable.decision.recommendedAction")}</p>
          <p className="text-lg font-semibold leading-8 text-zinc-50">{packet.recommended_action}</p>
        </div>
      )}

      <div className="mb-4 flex items-center gap-3">
        <p className="text-xs text-zinc-400">{t("pages.roundtable.decision.confidence")}</p>
        <div
          className="flex gap-1 text-sm"
          aria-label={packet.confidence_basis || t("pages.roundtable.decision.confidenceLabel")}
          title={packet.confidence_basis || undefined}
        >
          {decisionConfidenceDots(confidenceScore)}
        </div>
      </div>

      {/* Final summary */}
      {packet.final_summary && (
        <p className="text-[15px] text-zinc-200 leading-7 mb-4">{packet.final_summary}</p>
      )}

      {keyReasons.length > 0 && (
        <div className="mb-4 rounded-xl border border-zinc-700/60 bg-zinc-950/40 px-4 py-3">
          <p className="text-xs text-zinc-400 mb-2">{t("pages.roundtable.decision.keyReasons")}</p>
          <ul className="space-y-1.5">
            {keyReasons.map((reason, index) => (
              <li key={index} className="text-sm leading-6 text-zinc-300">
                • {reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Value disputes to user */}
      {packet.value_disputes_to_user?.length > 0 && (
        <div className="mb-3 p-3 rounded-lg border border-zinc-700/40 bg-zinc-800/30">
          <p className="text-xs text-zinc-500 mb-1.5">{t("pages.roundtable.decision.valuePreferenceChangesConclusion")}</p>
          {packet.value_disputes_to_user.map((vd, i) => (
            <div key={i} className="mb-2">
              <p className="text-sm text-zinc-300">· {vd.point}</p>
              {vd.ask_user && (
                <p className="text-xs text-zinc-500 ml-2 mt-0.5">→ {vd.ask_user}</p>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Stance evolution */}
      {packet.stance_evolution?.some((s) => s.changed) && (
        <div className="mb-3">
          <p className="text-xs text-zinc-500 mb-1.5">{t("pages.roundtable.decision.stanceShifts")}</p>
          {packet.stance_evolution.filter((s) => s.changed).map((s, i) => (
            <div key={i} className="text-sm text-zinc-400 mb-1 flex gap-1.5 items-start">
              <span className="text-zinc-500 shrink-0">{s.expert}:</span>
              <span className="text-zinc-500 line-through">{s.r1_stance}</span>
              <span className="text-zinc-400">→ {s.final_stance}</span>
            </div>
          ))}
        </div>
      )}

      {/* Unresolved */}
      {packet.unresolved?.length > 0 && (
        <div className="mb-3">
          <p className="text-xs text-zinc-500 mb-1.5">{t("pages.roundtable.decision.unresolvedItems")}</p>
          {packet.unresolved.map((u, i) => (
            <div key={i} className="text-sm text-zinc-400 mb-1">
              <span className="text-zinc-300">· {u.point}</span>
              {u.how_to_resolve && (
                <span className="text-zinc-500"> → {u.how_to_resolve}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* What changes mind */}
      {packet.what_changes_my_mind && (
        <div className="mb-4">
          <p className="text-xs text-zinc-500 mb-1">{t("pages.roundtable.decision.whatChangesMind")}</p>
          <p className="text-sm text-zinc-400 italic">"{packet.what_changes_my_mind}"</p>
        </div>
      )}

      <button
        onClick={onRestart}
        className="text-xs text-zinc-500 hover:text-zinc-300 underline transition-colors"
      >
        {t("pages.roundtable.actions.newRoundtable")}
      </button>
    </div>
  );
}

// ── PhaseHeader ────────────────────────────────────────────

function PhaseHeader({
  phase, question, disputeMap, experts, totalExperts,
}: {
  phase: Phase;
  question: string;
  disputeMap: DisputeMapData | null;
  experts: ExpertOpinionData[];
  totalExperts: number;
}) {
  const { t } = useTranslation();
  const expertsDone = experts.length;
  return (
    <div className="mb-5 space-y-3">
      <div className="rounded-xl border border-zinc-700/60 bg-zinc-900/60 px-5 py-4">
        <p className="mb-1 text-xs text-zinc-400">{t("pages.roundtable.dispute.discussionTopic")}</p>
        <p className="text-[15px] leading-7 text-zinc-200">{question}</p>
      </div>
      <div className="flex items-center gap-2 px-1">
        {phase === "s1_experts" && (
          <>
            <Loader2 className="w-4 h-4 animate-spin text-oracle-400" />
            <span className="text-sm text-zinc-300">
              {expertsDone === 0
                ? t("pages.roundtable.phases.gatheringExperts")
                : t("pages.roundtable.phases.progressExpertsDone", {
                    done: expertsDone,
                    total: totalExperts || "?",
                  })}
            </span>
            {totalExperts > 0 && (
              <div className="flex-1 h-1.5 rounded-full bg-zinc-800 overflow-hidden ml-2">
                <div className="h-full rounded-full bg-oracle-500/60 transition-all duration-500"
                  style={{ width: `${Math.round((expertsDone / totalExperts) * 100)}%` }} />
              </div>
            )}
          </>
        )}
        {phase === "s2_disputes" && (
          <>
            <Loader2 className="w-4 h-4 animate-spin text-amber-400" />
            <span className="text-sm text-amber-300/80">{t("pages.roundtable.phases.moderatorExtracting")}</span>
          </>
        )}
        {phase === "awaiting_choice_a" && (
          <>
            <CheckCircle className="w-4 h-4 text-oracle-400" />
            <span className="text-sm text-zinc-200">{t("pages.roundtable.phases.awaitingChoiceA")}</span>
          </>
        )}
        {phase === "s3_debate" && (
          <>
            <Loader2 className="w-4 h-4 animate-spin text-red-400" />
            <span className="text-sm text-red-300/80">{t("pages.roundtable.phases.debating")}</span>
          </>
        )}
        {phase === "awaiting_choice_b" && (
          <>
            <CheckCircle className="w-4 h-4 text-oracle-400" />
            <span className="text-sm text-zinc-200">{t("pages.roundtable.phases.awaitingChoiceB")}</span>
          </>
        )}
        {phase === "s4_decision" && (
          <>
            <Loader2 className="w-4 h-4 animate-spin text-oracle-400" />
            <span className="text-sm text-oracle-300/80">{t("pages.roundtable.phases.recommendationGenerating")}</span>
          </>
        )}
        {phase === "done" && (
          <>
            <CheckCircle className="w-4 h-4 text-green-400" />
            <span className="text-sm text-green-300">{t("pages.roundtable.phases.complete")}</span>
          </>
        )}
      </div>
    </div>
  );
}

// ── CoreDisputeSpotlight ───────────────────────────────────

function CoreDisputeSpotlight({
  disputeMap,
  showValueGuide,
}: {
  disputeMap: DisputeMapData;
  showValueGuide?: boolean;
}) {
  const { t } = useTranslation();
  const focal = pickFocalDispute(disputeMap);
  if (!focal) return null;
  const left = focal.sides[0];
  const right = focal.sides[1];
  const leftLead = left?.lead_expert || t("pages.roundtable.fallback.oneExpert");
  const rightLead = right?.lead_expert || t("pages.roundtable.fallback.anotherExpert");
  const leftClaim = left?.main_argument || left?.position || t("pages.roundtable.fallback.leftSideUnclear");
  const rightClaim = right?.main_argument || right?.position || t("pages.roundtable.fallback.rightSideUnclear");
  const summary = right
    ? t("pages.roundtable.spotlight.summaryVs", {
        topic: focal.topic,
        leftLead,
        leftClaim,
        rightLead,
        rightClaim,
      })
    : t("pages.roundtable.spotlight.summarySingle", {
        topic: focal.topic,
        leftLead,
        leftClaim,
      });
  const whyItMatters = focal.why_it_matters || t("pages.roundtable.fallback.whyItMatters");
  const showValueConflict = showValueGuide && focal.dispute_type?.includes("value") && left && right;
  const preferredValue = focal.value_aspect || focal.dimension_label || focal.topic;
  const alternativeValue = focal.factual_aspect || t("pages.roundtable.fallback.riskAndExecution");

  return (
    <div className="mb-5">
      <div className="rounded-xl border border-amber-500/25 bg-amber-500/[0.04] px-5 py-4">
        <p className="text-sm font-medium text-amber-300 mb-2">{t("pages.roundtable.spotlight.moderatorSuggestion")}</p>
        <p className="text-[15px] leading-7 text-zinc-100">{summary}</p>
        <p className="mt-3 text-sm leading-6 text-zinc-300">
          {t("pages.roundtable.spotlight.mattersBecause")} {whyItMatters}
        </p>
      </div>
      {showValueConflict && (
        <div className="mt-3 rounded-xl border border-zinc-700/40 bg-zinc-900/50 px-5 py-4">
          <p className="text-xs text-zinc-400 mb-2">{t("pages.roundtable.spotlight.dependsOnYou")}</p>
          <p className="text-sm leading-6 text-zinc-300">
            {t("pages.roundtable.spotlight.valueGuide", {
              preferredValue,
              leftPosition: left.position || left.main_argument,
              alternativeValue,
              rightPosition: right.position || right.main_argument,
            })}
          </p>
        </div>
      )}
    </div>
  );
}

// ── UserActionPanel ────────────────────────────────────────

function UserActionPanel({
  choicePoint, onChoice, choiceSubmitting, injectResetToken,
}: {
  choicePoint: "A" | "B";
  onChoice: (action: "deepen" | "conclude" | "inject", userInput?: string, cp?: "A" | "B") => void;
  choiceSubmitting?: boolean;
  injectResetToken?: number;
}) {
  const { t } = useTranslation();
  const [injectText, setInjectText] = useState("");
  const [showInject, setShowInject] = useState(false);

  useEffect(() => {
    if (!injectResetToken) return;
    setInjectText("");
  }, [injectResetToken]);
  const isA = choicePoint === "A";
  const primaryAction = isA
    ? {
        title: t("pages.roundtable.userActionPanel.primaryA.title"),
        description: t("pages.roundtable.userActionPanel.primaryA.description"),
        action: "deepen" as const,
        icon: <Zap className="w-5 h-5 text-oracle-300 shrink-0 mt-0.5" />,
      }
    : {
        title: t("pages.roundtable.userActionPanel.primaryB.title"),
        description: t("pages.roundtable.userActionPanel.primaryB.description"),
        action: "conclude" as const,
        icon: <CheckCircle className="w-5 h-5 text-oracle-300 shrink-0 mt-0.5" />,
      };
  const secondaryAction = isA
    ? {
        title: t("pages.roundtable.userActionPanel.secondaryA.title"),
        description: t("pages.roundtable.userActionPanel.secondaryA.description"),
        action: "conclude" as const,
        icon: <CheckCircle className="w-5 h-5 text-zinc-300 shrink-0 mt-0.5" />,
      }
    : {
        title: t("pages.roundtable.userActionPanel.secondaryB.title"),
        description: t("pages.roundtable.userActionPanel.secondaryB.description"),
        action: "deepen" as const,
        icon: <Swords className="w-5 h-5 text-zinc-300 shrink-0 mt-0.5" />,
      };
  const tertiaryTitle = isA ? t("pages.roundtable.userActionPanel.tertiaryTitleA") : t("pages.roundtable.userActionPanel.tertiaryTitleB");
  const tertiaryDescription = isA
    ? t("pages.roundtable.userActionPanel.tertiaryDescriptionA")
    : t("pages.roundtable.userActionPanel.tertiaryDescriptionB");

  return (
    <div className="mb-5 rounded-xl border border-oracle-500/20 bg-oracle-500/[0.03] px-5 py-4">
      <p className="text-sm font-medium text-zinc-200 mb-3">
        {isA
          ? t("pages.roundtable.userActionPanel.promptA")
          : t("pages.roundtable.userActionPanel.promptB")}
      </p>
      <div className="space-y-2">
        <button onClick={() => onChoice(primaryAction.action, undefined, choicePoint)}
          disabled={!!choiceSubmitting}
          className="w-full flex items-start gap-3 rounded-xl border border-oracle-500/40 bg-oracle-500/10 px-4 py-3 text-left hover:bg-oracle-500/20 disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
          {choiceSubmitting ? <Loader2 className="w-5 h-5 animate-spin text-oracle-300 shrink-0 mt-0.5" /> : primaryAction.icon}
          <div>
            <p className="text-sm font-medium text-oracle-300">{primaryAction.title}</p>
            <p className="text-xs text-zinc-300/80 mt-0.5">{primaryAction.description}</p>
          </div>
        </button>
        <button onClick={() => onChoice(secondaryAction.action, undefined, choicePoint)}
          disabled={!!choiceSubmitting}
          className="w-full flex items-start gap-3 rounded-xl border border-zinc-600/40 bg-zinc-800/30 px-4 py-3 text-left hover:bg-zinc-700/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
          {secondaryAction.icon}
          <div>
            <p className="text-sm font-medium text-zinc-200">{secondaryAction.title}</p>
            <p className="text-xs text-zinc-400 mt-0.5">{secondaryAction.description}</p>
          </div>
        </button>
        <div className="pt-1">
          <button
            onClick={() => setShowInject((v) => !v)}
            disabled={!!choiceSubmitting}
            className="inline-flex items-center gap-2 text-sm text-zinc-400 underline underline-offset-4 hover:text-zinc-200 disabled:opacity-40"
          >
            <MessageSquarePlus className="w-4 h-4" />
            {tertiaryTitle}
          </button>
          <p className="mt-1 text-xs text-zinc-500">{tertiaryDescription}</p>
        </div>
        {showInject && (
          <div className="flex gap-2 mt-1">
            <input className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500 disabled:opacity-40"
              placeholder={t("pages.roundtable.userActionPanel.inputPlaceholder")} value={injectText} disabled={!!choiceSubmitting}
              onChange={(e) => setInjectText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && injectText.trim() && !choiceSubmitting) onChoice("inject", injectText.trim(), choicePoint); }} />
            <button onClick={() => { if (injectText.trim()) onChoice("inject", injectText.trim(), choicePoint); }}
              disabled={!injectText.trim() || !!choiceSubmitting}
              className="rounded-lg border border-oracle-500/40 bg-oracle-500/10 px-4 py-2 text-sm text-oracle-300 hover:bg-oracle-500/20 disabled:opacity-40 transition-colors">
              {choiceSubmitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : t("pages.roundtable.actions.send")}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── EvidenceLayer (collapsed by default) ───────────────────

function EvidenceLayer({
  experts, disputeMap, rebuttals,
}: {
  experts: ExpertOpinionData[];
  disputeMap: DisputeMapData | null;
  rebuttals: RebuttalData[];
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-5 rounded-xl border border-zinc-700/40 bg-zinc-900/30">
      <button onClick={() => setOpen(v => !v)} className="w-full flex items-center gap-2 px-4 py-3 text-left">
        <span className="text-sm text-zinc-400">
          {open
            ? t("pages.roundtable.dispute.viewDetailedExpanded")
            : t("pages.roundtable.dispute.viewDetailedCollapsed")}
        </span>
        <span className="text-xs text-zinc-600 ml-auto">
          {rebuttals.length > 0
            ? t("pages.roundtable.dispute.evidenceSummaryWithDebates", {
                experts: experts.length,
                debates: rebuttals.length,
              })
            : t("pages.roundtable.dispute.evidenceSummary", {
                experts: experts.length,
              })}
        </span>
        {open ? <ChevronUp className="w-4 h-4 text-zinc-600" /> : <ChevronDown className="w-4 h-4 text-zinc-600" />}
      </button>
      {open && (
        <div className="px-4 pb-4 border-t border-zinc-800 space-y-4">
          <div className="pt-3">
            <p className="text-xs text-zinc-400 mb-2">{t("pages.roundtable.dispute.expertStances")}</p>
            <div className="space-y-2">
              {experts.map((ex, i) => (
                <ExpertStanceCard key={`${ex.model_id}-${i}`} expert={ex} index={i} />
              ))}
            </div>
          </div>
          {disputeMap && (
            <div>
              <p className="text-xs text-zinc-400 mb-2">{t("pages.roundtable.dispute.fullDisputeMap")}</p>
              <FullDisputeMap disputeMap={disputeMap} />
            </div>
          )}
          {rebuttals.length > 0 && (
            <div>
              <p className="text-xs text-zinc-400 mb-2">{t("pages.roundtable.dispute.debateRecords")}</p>
              <div className="space-y-2">
                {rebuttals.map((r, i) => (
                  <RebuttalCard key={`${r.model_id}-${i}`} rebuttal={r} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── FullDisputeMap (inside evidence layer) ─────────────────

function FullDisputeMap({ disputeMap }: { disputeMap: DisputeMapData }) {
  const { t } = useTranslation();
  const severityColor = (s: string) =>
    s === "high" ? "text-red-400 border-red-500/30 bg-red-500/5"
    : s === "medium" ? "text-amber-400 border-amber-500/30 bg-amber-500/5"
    : "text-zinc-400 border-zinc-600/30 bg-zinc-800/30";
  return (
    <div className="space-y-3">
      {disputeMap.consensus_points.length > 0 && (
        <div>
          <p className="text-xs text-zinc-400 mb-1.5">{t("pages.roundtable.dispute.consensus")}</p>
          <ul className="space-y-1">
            {disputeMap.consensus_points.map((cp, i) => (
              <li key={i} className="text-sm text-zinc-300 flex gap-1.5 items-start leading-relaxed">
                <span className={`text-xs border rounded px-1 shrink-0 mt-0.5 ${
                  cp.strength === "strong" ? "border-green-500/30 text-green-400"
                  : cp.strength === "moderate" ? "border-zinc-500/30 text-zinc-400"
                  : "border-zinc-700/30 text-zinc-600"
                }`}>{consensusStrengthLabel(cp.strength)}</span>
                {cp.point}
              </li>
            ))}
          </ul>
        </div>
      )}
      {disputeMap.contention_points.length > 0 && (
        <div>
          <p className="text-xs text-zinc-400 mb-1.5">{t("pages.roundtable.dispute.allDisagreements")}</p>
          <div className="space-y-2">
            {disputeMap.contention_points.map((cp, i) => (
              <div key={i} className={`rounded-lg border px-3 py-2.5 ${severityColor(cp.severity)}`}>
                <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                  <span className="text-sm font-medium">{cp.topic}</span>
                  {(Array.isArray(cp.dispute_type) ? cp.dispute_type : [cp.dispute_type]).map((dt, j) => (
                    <span key={j} className="text-xs border rounded px-1.5">
                      {disputeTypeLabel(dt)}
                    </span>
                  ))}
                  {cp.suggested_focus && (
                    <span className="text-xs text-oracle-300 border border-oracle-500/30 bg-oracle-500/10 rounded px-1.5">
                      {t("pages.roundtable.badges.recommendedFocus")}
                    </span>
                  )}
                </div>
                <div className="space-y-1">
                  {cp.sides.map((side, j) => (
                    <div key={j} className="text-sm text-zinc-300 flex gap-1.5">
                      {side.lead_expert && <span className="text-zinc-400 shrink-0">{side.lead_expert}：</span>}
                      <span>{side.main_argument || side.position}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
