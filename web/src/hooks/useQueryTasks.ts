/**
 * Multi-query concurrent task manager.
 *
 * Manages an array of QueryTask objects, each representing a concurrent question.
 * Supports: starting new queries, tracking streaming state per query,
 * recording individual model responses, and timing.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import i18n from "@/i18n";
import type { AskResponse, ClarificationNeeded, DraftAnswerItem, SearchCitation } from "@/types";

export type WaitIssueCode =
  | "timeout"
  | "all_models_failed"
  | "quota_limited"
  | "user_cancelled"
  | null;

export interface ModelContribution {
  model_id: string;
  content: string;
  latency_ms?: number;
}

export interface OptimizationPoint {
  type: string;        // 补充范围 | 明确角度 | 收窄焦点 | 修正表述 | 增加约束
  original: string;   // 原问题片段
  revised: string;    // 改后表述
  reason: string;     // 一句话说明
}

export interface QuestionConfirmation {
  original_question: string;
  optimized_question: string;
  changes_summary: string;
  needs_confirmation: boolean;
  optimization_points?: OptimizationPoint[];  // v4.17: per-change breakdown
}

export interface StageHistoryItem {
  stage: string;        // raw stage key
  label: string;        // friendly label
  detail: string;       // e.g. "5 子问题, 4 搜索词"
  startedAt: number;    // ms since task start
  completedAt?: number; // undefined while in-progress
}

export interface QueryTask {
  id: string;
  conversationId?: string | null;
  question: string;
  mode: string;
  status: "streaming" | "done" | "error";
  startTime: number;
  elapsed: number; // seconds, updated by timer
  stage: string;
  stageDetail: string;
  stageStartedAt: number;
  stageHistory: StageHistoryItem[];  // v4.24: research timeline
  contributorsDone: number;
  streamPreview: string;
  streamTokens: string;
  response: AskResponse | null;
  error: string | null;
  modelResponses: ModelContribution[];
  clarification: ClarificationNeeded | null;
  questionConfirmation: QuestionConfirmation | null;  // v3.3
  abortController: AbortController;
  fileIds?: string[];
  draftAnswers: DraftAnswerItem[];  // v3.3: intermediate versions
  streamCitations: SearchCitation[];  // v4.20: early citations from search, before complete event
  companionSkeleton: boolean;  // v5.1: Auto mode skeleton bubble (<200ms instant feedback)
  companionRoute: { message: string; actions: { label: string; capability_label?: string; model_label?: string; action_type: string; action_payload?: Record<string, unknown>; estimated_seconds?: number }[]; more_actions?: { label: string; capability_label?: string; model_label?: string; action_type: string; action_payload?: Record<string, unknown>; estimated_seconds?: number }[]; route_reason: string; auto_execute_seconds: number; is_silent: boolean; resolved_mode?: string; contributor_count?: number } | null;
  waitIssueCode: WaitIssueCode;
}

let _nextId = 1;
const MAX_CONCURRENT = 5;
const LEGACY_CANCELLED = "\u5DF2\u53D6\u6D88";
const LEGACY_QUOTA_TERMS = ["\u914D\u989D", "\u989D\u5EA6", "\u9650\u989D"];
const LEGACY_ALL_MODELS_FAILED_TERMS = [
  "\u6240\u6709\u6A21\u578B\u6682\u65F6\u4E0D\u53EF\u7528",
  "\u6240\u6709\u6A21\u578B\u5747\u65E0\u6CD5\u751F\u6210\u56DE\u7B54",
];
const LEGACY_TIMEOUT_TERMS = ["\u8D85\u65F6", "\u8FDE\u63A5\u4E2D\u65AD"];
const LEGACY_PREPARE_LABEL = "\u51C6\u5907";
const LEGACY_ANALYZE_QUESTION_LABEL = "\u5206\u6790\u95EE\u9898";
const LEGACY_SEARCH_LABEL = "\u641C\u7D22\u8D44\u6599";
const LEGACY_FAN_OUT_LABEL = "\u4E13\u5BB6\u601D\u8003";
const LEGACY_GAP_SEARCH_LABEL = "\u8865\u5145\u641C\u7D22";
const LEGACY_MOA_LABEL = "\u4EA4\u53C9\u9A8C\u8BC1";
const LEGACY_SYNTHESIS_LABEL = "\u7EFC\u5408\u7B54\u6848";
const LEGACY_REFINEMENT_LABEL = "\u6253\u78E8\u7B54\u6848";
const LEGACY_API_QUOTA_TEXT = "API \u989D\u5EA6\u6682\u65F6\u4E0D\u8DB3";

function sanitizeDisplayContent(text: string): string {
  if (!text) return "";
  return text
    .replace(/<think>[\s\S]*?<\/think>/gi, "")
    .replace(/<think>[\s\S]*$/gi, "")
    .replace(/<\/?think>/gi, "");
}

function hasCjk(text: string): boolean {
  return /[\u3400-\u9fff]/.test(text);
}

function detectWaitIssueFromError(rawError: string): WaitIssueCode {
  const raw = (rawError || "").trim();
  const lower = raw.toLowerCase();
  if (!raw) return null;
  if (raw.includes(LEGACY_CANCELLED) || lower.includes("cancelled") || lower.includes("canceled")) return "user_cancelled";
  if (
    LEGACY_QUOTA_TERMS.some((term) => raw.includes(term))
    || lower.includes("quota")
    || lower.includes("insufficient")
    || lower.includes("429")
  ) return "quota_limited";
  if (LEGACY_ALL_MODELS_FAILED_TERMS.some((term) => raw.includes(term))) return "all_models_failed";
  if (
    LEGACY_TIMEOUT_TERMS.some((term) => raw.includes(term))
    || lower.includes("timeout")
    || lower.includes("pipeline_timeout")
  ) return "timeout";
  return null;
}

function detectWaitIssueFromResponse(resp: AskResponse): WaitIssueCode {
  const answer = (resp.final_answer || "").trim();
  if (!answer) return null;
  if (answer.includes(LEGACY_API_QUOTA_TEXT)) return "quota_limited";
  if (LEGACY_ALL_MODELS_FAILED_TERMS.some((term) => answer.includes(term))) {
    return "all_models_failed";
  }
  return null;
}

function normalizeErrorMessage(rawError: string, code: WaitIssueCode): string {
  if (code === "timeout") return i18n.t("hooks.useQueryTasks.errors.timeout");
  if (code === "all_models_failed") return i18n.t("hooks.useQueryTasks.errors.allModelsFailed");
  if (code === "quota_limited") return i18n.t("hooks.useQueryTasks.errors.quotaLimited");
  if (code === "user_cancelled") return i18n.t("hooks.useQueryTasks.errors.userCancelled");
  return rawError || i18n.t("hooks.useQueryTasks.errors.requestFailed");
}

function fallbackStageDetail(mode: string, rawStage: string, label: string): string {
  const lower = (rawStage || "").toLowerCase();
  if (lower.includes("pipeline") || label.includes(LEGACY_PREPARE_LABEL)) {
    return mode === "research"
      ? i18n.t("hooks.useQueryTasks.stageDetail.pipeline.research")
      : i18n.t("hooks.useQueryTasks.stageDetail.pipeline.deep");
  }
  if (lower.includes("planner") || label.includes(LEGACY_ANALYZE_QUESTION_LABEL)) {
    return mode === "research"
      ? i18n.t("hooks.useQueryTasks.stageDetail.planner.research")
      : i18n.t("hooks.useQueryTasks.stageDetail.planner.deep");
  }
  if (lower.includes("search") || label.includes(LEGACY_SEARCH_LABEL)) {
    return i18n.t("hooks.useQueryTasks.stageDetail.search");
  }
  if (lower.includes("fan_out") || label.includes(LEGACY_FAN_OUT_LABEL)) {
    return i18n.t("hooks.useQueryTasks.stageDetail.fanOut");
  }
  if (lower.includes("gap_search") || label.includes(LEGACY_GAP_SEARCH_LABEL)) {
    return i18n.t("hooks.useQueryTasks.stageDetail.gapSearch");
  }
  if (lower.includes("moa") || label.includes(LEGACY_MOA_LABEL)) {
    return i18n.t("hooks.useQueryTasks.stageDetail.moa");
  }
  if (lower.includes("synthesis") || label.includes(LEGACY_SYNTHESIS_LABEL)) {
    return i18n.t("hooks.useQueryTasks.stageDetail.synthesis");
  }
  if (lower.includes("refinement") || label.includes(LEGACY_REFINEMENT_LABEL)) {
    return i18n.t("hooks.useQueryTasks.stageDetail.refinement");
  }
  return mode === "research"
    ? i18n.t("hooks.useQueryTasks.stageDetail.defaultResearch")
    : i18n.t("hooks.useQueryTasks.stageDetail.defaultDeep");
}

export function useQueryTasks() {
  const [tasks, setTasks] = useState<QueryTask[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const streamTokensMap = useRef<Record<string, string>>({});
  const tasksRef = useRef<QueryTask[]>([]);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  // Update elapsed time for streaming tasks
  const startTimer = useCallback(() => {
    if (timerRef.current) return;
    timerRef.current = setInterval(() => {
      setTasks((prev) =>
        prev.map((t) =>
          t.status === "streaming"
            ? { ...t, elapsed: Math.round((Date.now() - t.startTime) / 1000) }
            : t
        )
      );
    }, 1000);
  }, []);

  const stopTimerIfDone = useCallback(() => {
    setTasks((prev) => {
      const anyStreaming = prev.some((t) => t.status === "streaming");
      if (!anyStreaming && timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      return prev;
    });
  }, []);

  const updateTask = useCallback((id: string, patch: Partial<QueryTask>) => {
    setTasks((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  }, []);

  const applyTaskError = useCallback((id: string, rawError: string) => {
    setTasks((prev) =>
      prev.map((t) => {
        if (t.id !== id) return t;
        const waitIssue = detectWaitIssueFromError(rawError);
        return {
          ...t,
          error: normalizeErrorMessage(rawError, waitIssue),
          waitIssueCode: waitIssue,
          stage: waitIssue === "user_cancelled" ? i18n.t("hooks.useQueryTasks.waitIssue.cancelledStage") : t.stage,
          stageDetail: waitIssue === "user_cancelled"
            ? i18n.t("hooks.useQueryTasks.waitIssue.cancelledDetail")
            : t.stageDetail,
          status: "error",
          elapsed: Math.round((Date.now() - t.startTime) / 1000),
        };
      })
    );
  }, []);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      for (const t of tasksRef.current) {
        if (t.status === "streaming") {
          t.abortController.abort();
        }
      }
      streamTokensMap.current = {};
    };
  }, []);

  const startQuery = useCallback(
    (opts: {
      question: string;
      mode: string;
      conversationId?: string | null;
      webSearch: boolean;
      sessionId?: string;
      fileIds?: string[];
      skipPreflight?: boolean;
      singleModelId?: string;  // v5.1: CompanionBubble query_single override
      onCompleted?: (response: AskResponse) => void;  // BUG-4: immediate persist hook
    }) => {
      const id = `q-${_nextId++}-${Date.now()}`;
      const ac = new AbortController();
      streamTokensMap.current[id] = "";

      const task: QueryTask = {
        id,
        conversationId: opts.conversationId ?? null,
        question: opts.question,
        mode: opts.mode,
        status: "streaming",
        startTime: Date.now(),
        elapsed: 0,
        stage: i18n.t("hooks.useQueryTasks.initial.stage"),
        stageDetail: opts.mode === "research"
          ? i18n.t("hooks.useQueryTasks.initial.research")
          : opts.mode === "deep"
          ? i18n.t("hooks.useQueryTasks.initial.deep")
          : i18n.t("hooks.useQueryTasks.initial.default"),
        stageStartedAt: Date.now(),
        stageHistory: [],
        contributorsDone: 0,
        streamPreview: "",
        streamTokens: "",
        response: null,
        error: null,
        modelResponses: [],
        clarification: null,
        questionConfirmation: null,
        abortController: ac,
        fileIds: opts.fileIds,
        draftAnswers: [],
        streamCitations: [],
        companionSkeleton: opts.mode === "auto",
        companionRoute: null,
        waitIssueCode: null,
      };

      setTasks((prev) => {
        // AUDIT-002: limit concurrent queries
        // C4: mark evicted tasks as error (not just abort) to prevent zombie streaming
        const streaming = prev.filter((t) => t.status === "streaming");
        let updated = prev;
        if (streaming.length >= MAX_CONCURRENT) {
          const victim = streaming[streaming.length - 1];
          victim.abortController.abort();
          updated = prev.map((t) =>
            t.id === victim.id ? { ...t, status: "error" as const, error: i18n.t("hooks.useQueryTasks.errors.replacedByNewQuery") } : t
          );
        }
        return [task, ...updated];
      });
      setSelectedId(id);
      startTimer();

      // Stage labels
      const STAGE_LABELS: Record<string, string> = {
        pipeline:    i18n.t("hooks.useQueryTasks.stageLabels.pipeline"),
        planner:     i18n.t("hooks.useQueryTasks.stageLabels.planner"),
        search:      i18n.t("hooks.useQueryTasks.stageLabels.search"),
        fan_out:     i18n.t("hooks.useQueryTasks.stageLabels.fanOut"),
        gap_search:  i18n.t("hooks.useQueryTasks.stageLabels.gapSearch"),
        extraction:  i18n.t("hooks.useQueryTasks.stageLabels.extraction"),
        moa_layer2:  i18n.t("hooks.useQueryTasks.stageLabels.moaLayer2"),
        synthesis:   i18n.t("hooks.useQueryTasks.stageLabels.synthesis"),
        refinement:  i18n.t("hooks.useQueryTasks.stageLabels.refinement"),
      };
      const friendlyStage = (raw: string): string => {
        const lower = raw.toLowerCase();
        for (const [key, label] of Object.entries(STAGE_LABELS)) {
          if (lower.startsWith(key) || lower.includes(key)) return label;
        }
        if (/\d+\s*contributors?/i.test(raw)) return STAGE_LABELS.fan_out;
        if (/judge/i.test(raw)) return STAGE_LABELS.synthesis;
        if (/moa/i.test(raw)) return STAGE_LABELS.moa_layer2;
        return i18n.t("hooks.useQueryTasks.stageLabels.analyzing");
      };

      api
        .askStream(
          {
            question: opts.question,
            mode: opts.mode,
            web_search: opts.webSearch,
            skip_preflight: opts.skipPreflight ?? false,
            session_id: opts.sessionId || undefined,
            file_ids: opts.fileIds || [],
            single_model_id: opts.singleModelId || undefined,
          },
          {
            onStageStart: (s, detail) => {
              if (ac.signal.aborted) return;
              const label = friendlyStage(s);
              updateTask(id, {
                stage: label,
                stageDetail: detail?.trim() || fallbackStageDetail(opts.mode, s, label),
                stageStartedAt: Date.now(),
                companionSkeleton: false,
              });
              // Research timeline: record stage start
              if (opts.mode === "research") {
                setTasks((prev) => prev.map((t) => {
                  if (t.id !== id) return t;
                  const elapsed = Date.now() - t.startTime;
                  return {
                    ...t,
                    stageHistory: [
                      ...t.stageHistory,
                      { stage: s, label, detail: detail || "", startedAt: elapsed },
                    ],
                  };
                }));
              }
            },
            onContributor: () => {
              if (ac.signal.aborted) return;
              setTasks((prev) =>
                prev.map((t) =>
                  t.id === id ? { ...t, contributorsDone: t.contributorsDone + 1 } : t
                )
              );
            },
            onPreview: (_m, c) => {
              if (ac.signal.aborted) return;
              // Ignore empty/whitespace preview chunks to avoid blank streaming cards.
              const sanitized = sanitizeDisplayContent(c || "");
              if (!sanitized || sanitized.trim().length === 0) return;
              updateTask(id, { streamPreview: sanitized });
            },
            onToken: (t) => {
              if (ac.signal.aborted) return;
              const nextTokens = (streamTokensMap.current[id] || "") + t;
              streamTokensMap.current[id] = nextTokens;
              const visibleTokens = sanitizeDisplayContent(nextTokens);
              if (visibleTokens.trim().length === 0) return;
              // v4.20: do NOT clear streamPreview — preview card must persist independently
              updateTask(id, { streamTokens: visibleTokens });
            },
            onStageComplete: (s, detail?: string) => {
              if (ac.signal.aborted) return;
              // Research timeline: mark last matching stage as complete
              if (opts.mode === "research") {
                setTasks((prev) => prev.map((t) => {
                  if (t.id !== id) return t;
                  const elapsed = Date.now() - t.startTime;
                  // Find last entry with matching stage key that has no completedAt
                  const history = [...t.stageHistory];
                  for (let i = history.length - 1; i >= 0; i--) {
                    if (history[i].stage === s && !history[i].completedAt) {
                      history[i] = { ...history[i], completedAt: elapsed, detail: detail || history[i].detail };
                      break;
                    }
                  }
                  return { ...t, stageHistory: history };
                }));
              }
            },
            onDraftAnswer: (stage, _modelId, content) => {
              if (ac.signal.aborted) return;
              const item: DraftAnswerItem = {
                stage: stage as DraftAnswerItem["stage"],
                model_id: _modelId,
                content: sanitizeDisplayContent(content),
              };
              setTasks((prev) => prev.map((t) =>
                t.id === id ? { ...t, draftAnswers: [...t.draftAnswers, item] } : t
              ));
            },
            onCitationsReady: (citations) => {
              if (!ac.signal.aborted) updateTask(id, { streamCitations: citations });
            },
            onCompanionRoute: (data) => {
              if (!ac.signal.aborted) updateTask(id, { companionSkeleton: false, companionRoute: data });
            },
            onClarificationNeeded: (data) => {
              if (!ac.signal.aborted)
                updateTask(id, { clarification: data, status: "done" });
            },
            onQuestionConfirmation: (data) => {
              if (!ac.signal.aborted) {
                const original = sanitizeDisplayContent(data.original_question || "").trim();
                const optimizedRaw = sanitizeDisplayContent(data.optimized_question || "").trim();
                let optimized = optimizedRaw || original;
                if (optimized && !hasCjk(optimized) && hasCjk(original)) {
                  optimized = original;
                }
                updateTask(id, {
                  questionConfirmation: {
                    ...data,
                    original_question: original || data.original_question,
                    optimized_question: optimized || data.optimized_question,
                  },
                  status: "done",
                  stage: i18n.t("hooks.useQueryTasks.questionConfirmation.doneStage"),
                });
              }
            },
            onComplete: (r) => {
              if (ac.signal.aborted) return;
              const liveTask = tasksRef.current.find((t) => t.id === id);
              const normalizedFinal = (r.final_answer || "").trim()
                || sanitizeDisplayContent(liveTask?.streamTokens || "").trim()
                || sanitizeDisplayContent(liveTask?.streamPreview || "").trim();
              const normalizedResponse = normalizedFinal && !(r.final_answer || "").trim()
                ? { ...r, final_answer: normalizedFinal }
                : r;
              const waitIssue = detectWaitIssueFromResponse(normalizedResponse);
              // BUG-4 fix: invoke persist hook immediately before state update,
              // so assistant message is written to localStorage even if user refreshes mid-stream.
              opts.onCompleted?.(normalizedResponse);
              updateTask(id, {
                response: normalizedResponse,
                status: "done",
                elapsed: Math.round((Date.now() - task.startTime) / 1000),
                streamTokens: "",   // clear streaming card atomically with final answer
                streamPreview: "",  // prevent flash of both cards in same render
                waitIssueCode: waitIssue,
              });
              delete streamTokensMap.current[id]; // AUDIT-006: free memory
              stopTimerIfDone();
            },
            onError: (e) => {
              if (ac.signal.aborted) return;
              applyTaskError(id, e);
              delete streamTokensMap.current[id]; // AUDIT-006: free memory
              stopTimerIfDone();
            },
          },
          ac.signal
        )
        .then(() => {
          if (!ac.signal.aborted) {
            setTasks((prev) =>
              prev.map((t) =>
                t.id === id && t.status === "streaming" && !t.response
                  ? { ...t, status: "done", elapsed: Math.round((Date.now() - task.startTime) / 1000) }
                  : t
              )
            );
            stopTimerIfDone();
          }
        })
        .catch((e) => {
          if (ac.signal.aborted) return;
          const rawError = (e as { detail?: string }).detail || i18n.t("hooks.useQueryTasks.errors.requestFailed");
          applyTaskError(id, rawError);
          stopTimerIfDone();
        });

      return id;
    },
    [applyTaskError, startTimer, stopTimerIfDone, updateTask]
  );

  const cancelTask = useCallback((id: string) => {
    // AUDIT-007: abort outside setTasks to avoid side-effects in updater.
    const target = tasksRef.current.find((t) => t.id === id);
    if (target) target.abortController.abort();
    setTasks((prev) => {
      return prev.map((t) =>
        t.id === id
          ? {
              ...t,
              status: "error" as const,
              error: i18n.t("hooks.useQueryTasks.errors.userCancelled"),
              stage: i18n.t("hooks.useQueryTasks.waitIssue.cancelledStage"),
              stageDetail: i18n.t("hooks.useQueryTasks.waitIssue.cancelledDetail"),
              waitIssueCode: "user_cancelled" as const,
            }
          : t
      );
    });
    delete streamTokensMap.current[id];
    stopTimerIfDone();
  }, [stopTimerIfDone]);

  const removeTask = useCallback((id: string) => {
    const t = tasksRef.current.find((x) => x.id === id);
    if (t && t.status === "streaming") t.abortController.abort();
    setTasks((prev) => {
      return prev.filter((x) => x.id !== id);
    });
    setSelectedId((prev) => (prev === id ? null : prev));
    delete streamTokensMap.current[id];
  }, []);

  const selected = tasks.find((t) => t.id === selectedId) || null;
  const hasStreaming = tasks.some((t) => t.status === "streaming");

  return {
    tasks,
    selected,
    selectedId,
    setSelectedId,
    startQuery,
    cancelTask,
    removeTask,
    hasStreaming,
  };
}
