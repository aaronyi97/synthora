/**
 * ResearchTimeline — v4.24
 *
 * Shows the Research mode pipeline stages as a live vertical timeline.
 * Appears during streaming; collapses to a one-line summary when done.
 *
 * Stages: planner → search → fan_out → gap_search → moa_layer2 → synthesis → refinement
 */

import { useState } from "react";
import { CheckCircle2, Loader2, ChevronDown, ChevronUp, FlaskConical } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { StageHistoryItem } from "@/hooks/useQueryTasks";
import { cn } from "@/lib/utils";
import {
  OUTPUT_SURFACE_SOFT,
  STATUS_BADGE_ACCENT,
  STATUS_BADGE_SUCCESS,
} from "@/lib/outputStyle";

interface Props {
  stageHistory: StageHistoryItem[];
  isStreaming: boolean;
  taskStatus?: string;
}

const STAGE_ICON: Record<string, string> = {
  planner:    "🗺️",
  search:     "🔍",
  fan_out:    "🤖",
  gap_search: "🔬",
  moa_layer2: "🔀",
  synthesis:  "⚗️",
  refinement: "✨",
};

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatElapsed(ms: number): string {
  const seconds = Math.max(1, Math.round(ms / 1000));
  if (seconds >= 60) {
    const minutes = Math.floor(seconds / 60);
    const remain = seconds % 60;
    return remain > 0 ? `${minutes}m ${remain}s` : `${minutes}m`;
  }
  return `${seconds}s`;
}

function getActiveStage(stageHistory: StageHistoryItem[]): StageHistoryItem | null {
  for (let i = stageHistory.length - 1; i >= 0; i--) {
    if (stageHistory[i].completedAt === undefined) return stageHistory[i];
  }
  return stageHistory[stageHistory.length - 1] ?? null;
}

function normalizeTerminalStatus(taskStatus?: string): "done" | "error" | "cancelled" {
  if (taskStatus === "cancelled" || taskStatus === "user_cancelled") return "cancelled";
  if (taskStatus === "error") return "error";
  return "done";
}

function StageDot({ item }: { item: StageHistoryItem }) {
  const done = item.completedAt !== undefined;
  const duration = done ? item.completedAt! - item.startedAt : null;
  const icon = STAGE_ICON[item.stage] ?? "⚙️";

  return (
    <div className="flex items-start gap-2.5">
      {/* Timeline spine */}
      <div className="flex flex-col items-center shrink-0 pt-0.5">
        <div className={`flex items-center justify-center size-5 rounded-full text-[10px] transition-all duration-300
          ${done ? "bg-emerald-500/15 border border-emerald-500/30" : "bg-blue-500/10 border border-blue-500/20"}`}
        >
          {done
            ? <CheckCircle2 size={11} className="text-emerald-400" />
            : <Loader2 size={11} className="text-blue-400 animate-spin" />}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 pb-3">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[10px]">{icon}</span>
          <span className={`text-[11px] font-medium leading-tight ${done ? "text-zinc-300" : "text-blue-300"}`}>
            {item.label.replace("...", "")}
          </span>
          {item.detail && (
            <span className="text-[10px] text-zinc-500">{item.detail}</span>
          )}
          {duration !== null && (
            <span className="text-[10px] text-zinc-600 tabular-nums ml-auto">
              {formatMs(duration)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ResearchTimeline({ stageHistory, isStreaming, taskStatus }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(true);

  if (stageHistory.length === 0) return null;

  const doneCount = stageHistory.filter((s) => s.completedAt !== undefined).length;
  const totalCount = stageHistory.length;
  const activeStage = getActiveStage(stageHistory);
  const terminalStatus = normalizeTerminalStatus(taskStatus);
  const totalElapsedMs = stageHistory.reduce((max, item) => {
    const end = item.completedAt ?? item.startedAt;
    return Math.max(max, end);
  }, 0);
  const collapsedSummary = terminalStatus === "done"
    ? t("components.researchTimeline.collapsed.done", { count: totalCount })
    : terminalStatus === "error"
    ? t("components.researchTimeline.status.error")
    : t("components.researchTimeline.status.cancelled");

  // Collapsed summary after done
  if (!isStreaming && !expanded) {
    return (
      <button
        onClick={() => setExpanded(true)}
        className="mb-2 flex items-center gap-1.5 text-[10px] text-zinc-500 transition-colors hover:text-zinc-300"
      >
        <FlaskConical size={10} />
        <span>{t("components.researchTimeline.header.title")} · {collapsedSummary}</span>
        <ChevronDown size={10} />
      </button>
    );
  }

  return (
    <div className={cn("mb-3", OUTPUT_SURFACE_SOFT)}>
      {/* Header */}
      <div className="flex items-center justify-between border-b border-white/[0.05] px-3 py-2">
        <div className="flex items-center gap-1.5">
          <span className={STATUS_BADGE_ACCENT}>
            <FlaskConical size={11} className="text-violet-400" />
            <span>{t("components.researchTimeline.header.title")}</span>
          </span>
          {isStreaming && (
            <span className="text-[10px] text-zinc-500">
              {t("components.researchTimeline.header.progress", { done: doneCount, total: totalCount })}
            </span>
          )}
          {totalElapsedMs > 0 && (
            <span className="text-[10px] text-zinc-500">
              · {t("components.researchTimeline.header.elapsed", { elapsed: formatElapsed(totalElapsedMs) })}
            </span>
          )}
          {!isStreaming && (
            terminalStatus === "done" ? (
              <span className={STATUS_BADGE_SUCCESS}>
                {t("components.researchTimeline.status.done")}
              </span>
            ) : terminalStatus === "error" ? (
              <span className="inline-flex items-center rounded-full border border-red-500/25 bg-red-500/10 px-2 py-0.5 text-[10px] text-red-300">
                {t("components.researchTimeline.status.error")}
              </span>
            ) : (
              <span className="inline-flex items-center rounded-full border border-zinc-700/60 bg-zinc-800/70 px-2 py-0.5 text-[10px] text-zinc-400">
                {t("components.researchTimeline.status.cancelled")}
              </span>
            )
          )}
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="rounded-md p-1 text-zinc-500 transition-colors hover:bg-white/[0.04] hover:text-zinc-300"
        >
          {expanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
        </button>
      </div>

      {activeStage && (
        <div className="border-b border-white/[0.05] px-3 py-2">
          <p className={`text-[11px] font-medium ${isStreaming ? "text-zinc-200" : "text-zinc-300"}`}>
            {t("components.researchTimeline.currentStage", { stage: activeStage.label.replace("...", "") })}
          </p>
          <p className="mt-1 text-[10px] leading-relaxed text-zinc-500">
            {activeStage.detail || (isStreaming
              ? t("components.researchTimeline.detail.streaming")
              : terminalStatus === "done"
              ? t("components.researchTimeline.detail.done")
              : terminalStatus === "error"
              ? t("components.researchTimeline.detail.error")
              : t("components.researchTimeline.detail.cancelled"))}
          </p>
          {isStreaming && (
            <p className="mt-1 text-[10px] leading-relaxed text-zinc-400">
              {t("components.researchTimeline.detail.streamingHint")}
            </p>
          )}
        </div>
      )}

      {/* Stage list */}
      {expanded && (
        <div className="px-3 pt-3 pb-1">
          {/* Vertical connector line behind the dots */}
          <div className="relative">
            {stageHistory.length > 1 && (
              <div
                className="absolute left-[9px] top-5 bottom-3 w-px bg-zinc-800/60"
                aria-hidden
              />
            )}
            {stageHistory.map((item, i) => (
              <StageDot key={`${item.stage}-${i}`} item={item} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
