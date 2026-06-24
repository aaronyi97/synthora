import { useState, useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import EnhancedMarkdown from "./EnhancedMarkdown";
import ActionBar from "./ActionBar";
import {
  ChevronDown,
  ChevronUp,
  RotateCcw,
} from "lucide-react";
import type { AskResponse, LowConfidenceAction } from "@/types";
import { cn } from "@/lib/utils";
import { api } from "@/api/client";
import { SUPPORT_CONTACT } from "@/lib/constants";
import {
  ACTION_CHIP_SECONDARY,
  ACTION_ICON_BUTTON,
  OUTPUT_META_BAR,
  OUTPUT_META_PILL,
  OUTPUT_SECTION_HEADER,
  OUTPUT_SURFACE,
  OUTPUT_SURFACE_SOFT,
  STATUS_BADGE_WARNING,
  STATUS_BADGE_ACCENT,
  STATUS_PANEL_ACCENT,
  STATUS_PANEL_WARNING,
} from "@/lib/outputStyle";

type Translate = (key: string, options?: Record<string, unknown>) => string;

interface DivergencePosition {
  stance: string;
  summary: string;
  models?: string[];
}

interface DivergencePointUI {
  topic: string;
  description: string;
  positions: DivergencePosition[];
  consensus_ratio: number;
  difficulty: string;
}

function buildDifficultyStyle(t: Translate): Record<string, { label: string; color: string }> {
  return {
    easy: { label: t("components.responseDisplay.difficulty.easy"), color: "text-emerald-400/80 bg-emerald-500/[0.08] border border-emerald-500/15" },
    medium: { label: t("components.responseDisplay.difficulty.medium"), color: "text-amber-400/80 bg-amber-500/[0.08] border border-amber-500/15" },
    hard: { label: t("components.responseDisplay.difficulty.hard"), color: "text-rose-400/80 bg-rose-500/[0.08] border border-rose-500/15" },
  };
}

const STANCE_BORDER = ["border-l-sky-400", "border-l-violet-400", "border-l-rose-400", "border-l-amber-400"];
const DIVERGENCE_DOTS = ["bg-sky-400", "bg-violet-400", "bg-rose-400", "bg-amber-400"];

function stripReasoningArtifacts(text: string): string {
  if (!text) return "";
  return text
    .replace(/<think>[\s\S]*?<\/think>/gi, "")
    .replace(/<\/?think>/gi, "")
    .trim();
}

function triggerDownload(content: string, filename: string, mime = "text/plain;charset=utf-8") {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function buildMarkdown(response: AskResponse, content: string): string {
  return `# ${response.question}\n\n${content}\n`;
}

function DivergenceItemFull({ point, index }: { point: DivergencePointUI; index: number }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`mt-0.5 size-1.5 shrink-0 rounded-full ${DIVERGENCE_DOTS[index % DIVERGENCE_DOTS.length]}`} />
        <span className="text-[15px] font-semibold text-zinc-50 leading-snug">{point.topic}</span>
      </div>
      {point.description && (
        <p className="pl-3.5 text-[14px] leading-7 text-zinc-300">{point.description}</p>
      )}
      {point.positions && point.positions.length > 0 && (
        <div className="space-y-1.5 pl-3.5">
          {point.positions.slice(0, 3).map((pos, pi) => (
            <div key={pi} className={`border-l-2 pl-3 py-0.5 ${STANCE_BORDER[pi % STANCE_BORDER.length]}`}>
              <span className="text-[14px] font-medium text-zinc-200">{pos.stance}</span>
              {pos.summary && <p className="mt-1 text-[13px] leading-7 text-zinc-300">{pos.summary}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface Props {
  response: AskResponse;
  onAction?: (action: string, question: string) => void;
  onRetry?: (question: string, mode: string) => void;
}

function buildGateLabels(t: Translate): Record<string, { label: string; color: string }> {
  return {
    synthesized: { label: t("components.responseDisplay.gates.synthesized"), color: "border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-300" },
    best_single: { label: t("components.responseDisplay.gates.bestSingle"), color: "border-oracle-500/20 bg-oracle-500/[0.08] text-oracle-300" },
    low_confidence: { label: t("components.responseDisplay.gates.lowConfidence"), color: "border-amber-500/20 bg-amber-500/[0.08] text-amber-300" },
  };
}

const INSIGHT_DOTS = ["bg-emerald-400", "bg-sky-400", "bg-amber-400", "bg-rose-400", "bg-violet-400"];

function CollapsibleSection({
  label,
  labelColor = "text-zinc-400",
  defaultOpen = false,
  children,
}: {
  label: string;
  labelColor?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={cn("mt-2", OUTPUT_SURFACE_SOFT)}>
      <button
        onClick={() => setOpen(!open)}
        className={cn(OUTPUT_SECTION_HEADER, "py-3.5 text-[14px]", labelColor)}
      >
        <span>{label}</span>
        {open
          ? <ChevronUp size={15} className="text-zinc-400" />
          : <ChevronDown size={15} className="text-zinc-400" />}
      </button>
      {open && <div className="border-t border-white/[0.08] px-4 pb-4">{children}</div>}
    </div>
  );
}

export default function ResponseDisplay({ response, onAction, onRetry }: Props) {
  const { t } = useTranslation();
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const difficultyStyle = buildDifficultyStyle(t);
  const gateLabels = buildGateLabels(t);

  const cleanAnswer = stripReasoningArtifacts(response.final_answer.replace(/\r\n/g, "\n").replace(/\r/g, "\n"));
  const isEmpty = cleanAnswer.trim().length === 0;
  const safeAnswer = isEmpty
    ? t("components.responseDisplay.emptyResponse", { question: response.question, supportContact: SUPPORT_CONTACT })
    : cleanAnswer;

  const submitFeedback = useCallback((vote: "up" | "down") => {
    if (feedback) return;
    setFeedback(vote);
    const record = {
      query_id: response.query_id,
      vote,
      mode: response.mode,
      quality_gate: response.quality_gate,
    };
    try {
      const existing = JSON.parse(localStorage.getItem("synthora_feedback") ?? "[]");
      const next = [...existing, record].slice(-100);
      localStorage.setItem("synthora_feedback", JSON.stringify(next));
    } catch {}
    api.submitFeedback({
      query_id: response.query_id,
      vote,
      mode: response.mode,
      quality_gate: response.quality_gate,
    }).catch(() => {});
  }, [feedback, response.query_id, response.mode, response.quality_gate]);

  const gate = gateLabels[response.quality_gate] ?? { label: response.quality_gate, color: "text-zinc-400" };
  const reasonCode = typeof response.reason_code === "string" ? response.reason_code : "";
  const isDegraded = reasonCode === "low_confidence" || (!reasonCode && response.quality_gate === "low_confidence");
  const hasPostAnswerGuidance = Boolean(
    (response.guidance && response.guidance.source !== "none")
    || response.companion_guide
  );
  const consensusType = typeof response.consensus_type === "string" && response.consensus_type.trim() !== "" && response.consensus_type.toLowerCase() !== "unknown"
    ? response.consensus_type
    : null;
  const isResearchBestSingle = response.mode === "research" && response.quality_gate === "best_single";

  return (
    <div className="w-full space-y-0">
      <div className={cn(OUTPUT_SURFACE, "border-white/[0.12] bg-[linear-gradient(180deg,rgba(255,255,255,0.03),rgba(255,255,255,0.015))] shadow-[0_20px_56px_rgba(0,0,0,0.28)]")}>
        {response.mode === "research" && (
          <div className="px-4 pt-3 pb-0">
            <span className={STATUS_BADGE_WARNING}>
              {isResearchBestSingle ? t("components.responseDisplay.research.directResult") : t("components.responseDisplay.research.report")}
            </span>
          </div>
        )}

        {isDegraded && (
          <div className={cn("mx-4 mt-4 text-[14px] leading-7 text-amber-100", STATUS_PANEL_WARNING)}>
            {t("components.responseDisplay.degradedWarning")}
          </div>
        )}

        {response.quality_gate === "best_single" && response.mode !== "light" && (
          <div className={cn("mx-4 mt-4 flex items-center gap-2 text-[14px] leading-7 text-zinc-100", STATUS_PANEL_ACCENT)}>
            <span className="text-oracle-300">✦</span>
            <span>{isResearchBestSingle ? t("components.responseDisplay.bestSingle.research") : t("components.responseDisplay.bestSingle.default")}</span>
          </div>
        )}

        <div className="px-4 pt-4 pb-3">
          <EnhancedMarkdown content={safeAnswer} citations={response.search_citations as unknown as import("./EnhancedMarkdown").Citation[] | undefined} />
        </div>

        <div className={OUTPUT_META_BAR}>
          <div className="flex flex-wrap items-center gap-2.5 text-[13px] text-zinc-100">
            {response.mode !== "light" && (
              <span className={cn("inline-flex items-center rounded-full border px-2.5 py-1.5 text-[13px] font-semibold", gate.color)}>
                {gate.label}
              </span>
            )}
            {consensusType && (
              <span className={cn(OUTPUT_META_PILL, "border-white/[0.12] bg-white/[0.06] px-2.5 py-1.5 text-[13px] text-zinc-200")}>
                {consensusType}
              </span>
            )}
            <span className={cn(OUTPUT_META_PILL, "border-white/[0.12] bg-white/[0.06] px-2.5 py-1.5 text-[13px] text-zinc-200")}>
              {response.mode}
            </span>
            {response.fast_path && <span className="font-medium text-emerald-300">{t("components.responseDisplay.meta.fastPath")}</span>}
            {response.mode !== "light" && <span className="text-zinc-200">{t("components.responseDisplay.meta.expertCount", { count: response.contributor_count })}</span>}
            {response.mode !== "light" && <span className="text-zinc-200">{t("components.responseDisplay.meta.consensus", { percent: (response.confidence * 100).toFixed(0) })}</span>}
            <span className="text-zinc-300">{(response.latency_ms / 1000).toFixed(1)}s</span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 sm:justify-end sm:gap-1">
            <ActionBar
              content={safeAnswer}
              title={response.question}
              queryId={response.query_id}
              showFeedback
              onFeedback={submitFeedback}
              feedbackState={feedback}
            />
            <button
              onClick={() => onRetry?.(response.question, response.mode)}
              title={t("common.actions.retry")}
              className={ACTION_ICON_BUTTON}
            >
              <RotateCcw size={14} />
            </button>
          </div>
        </div>
      </div>

      {response.has_divergence && (() => {
        const pts = (response.divergence_points as unknown as DivergencePointUI[] | undefined);
        const consensusPts = (response as unknown as { consensus_points?: string[] }).consensus_points;
        const hasPts = pts && pts.length > 0;
        const hasConsensus = consensusPts && consensusPts.length > 0;
        const divergenceCount = hasPts ? Math.min(pts!.length, 3) : 0;
        const label = hasPts
          ? t("components.responseDisplay.divergence.labelWithCount", { count: divergenceCount })
          : t("components.responseDisplay.divergence.label");
        return (
          <CollapsibleSection label={label} labelColor="text-amber-400" defaultOpen>
            <div className="space-y-3 pt-2.5">
              {hasConsensus && (
              <div>
                  <p className="mb-1.5 text-[13px] font-medium uppercase tracking-wide text-emerald-300">{t("components.responseDisplay.divergence.coreConsensus")}</p>
                  <div className="space-y-1">
                    {consensusPts!.slice(0, 3).map((pt, i) => (
                      <div key={i} className="flex items-start gap-2">
                        <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-emerald-400" />
                        <span className="text-[14px] leading-7 text-zinc-100">{pt}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {hasPts ? (
                <div>
                  {hasConsensus && <div className="border-t border-white/[0.04] my-2" />}
                  <p className="mb-1.5 text-[13px] font-medium uppercase tracking-wide text-amber-300">{t("components.responseDisplay.divergence.mainDifferences")}</p>
                  <div className="space-y-3">
                    {pts!.slice(0, 3).map((pt, i) => (
                      <DivergenceItemFull key={i} point={pt} index={i} />
                    ))}
                  </div>
                </div>
              ) : response.divergence_summary ? (
                <div className="rounded-xl border-l-2 border-amber-500/55 bg-amber-500/[0.08] py-2 pl-3.5 pr-3">
                  <p className="text-[14px] leading-7 text-amber-100">{response.divergence_summary}</p>
                </div>
              ) : null}
            </div>
          </CollapsibleSection>
        );
      })()}

      {response.fact_warnings && response.fact_warnings.length > 0 && (
        <CollapsibleSection
          label={t("components.responseDisplay.factWarnings.label", { count: response.fact_warnings.length })}
          labelColor="text-amber-400/80"
          defaultOpen={response.fact_warnings.length > 0}
        >
          <div className="space-y-1.5 pt-2.5">
            <p className="text-[14px] leading-7 text-zinc-200">
              {t("components.responseDisplay.factWarnings.body")}
            </p>
            {response.fact_warnings.map((w, i) => (
              <div key={i} className="flex items-start gap-2 rounded-xl border border-amber-500/25 bg-amber-500/[0.1] px-3 py-2">
                <span className="text-[14px] leading-7 text-amber-100">{w}</span>
              </div>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {response.key_insights && response.key_insights.length > 0 && (
        <CollapsibleSection label={t("components.responseDisplay.keyInsights.label", { count: response.key_insights.length })} labelColor="text-oracle-400" defaultOpen>
          <div className="space-y-2 pt-2.5">
            {response.key_insights.map((insight, i) => (
              <div key={`insight-${i}`} className="flex items-start gap-2.5">
                <span className={`mt-1.5 size-1.5 shrink-0 rounded-full ${INSIGHT_DOTS[i % INSIGHT_DOTS.length]}`} />
                <span className="text-[15px] leading-8 text-zinc-100">{insight}</span>
              </div>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {!hasPostAnswerGuidance && response.low_confidence_actions && response.low_confidence_actions.length > 0 && (
        <div className={cn("mt-2 space-y-2 p-3", OUTPUT_SURFACE_SOFT, "border-white/[0.12] bg-zinc-900/75")}>
          <p className="text-[14px] font-medium text-amber-300">{t("components.responseDisplay.lowConfidence.title")}</p>
          <div className="flex flex-wrap gap-1.5">
            {response.low_confidence_actions.map((act: LowConfidenceAction) => (
              <button
                key={act.action}
                onClick={() => onAction?.(act.action, response.question)}
                className={ACTION_CHIP_SECONDARY}
              >
                {act.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="mt-3 border-t border-white/[0.08] pt-3">
        <p className="text-[14px] leading-7 text-zinc-300">
          {(response as unknown as { ai_disclosure?: string }).ai_disclosure ?? t("common.app.aiDisclosure")}
        </p>
      </div>
    </div>
  );
}
