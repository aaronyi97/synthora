/**
 * Query Progress Bars — shows all active/recent queries at the top of the page.
 *
 * Each bar displays: truncated question, elapsed time, color-coded status, stage.
 * Click to select/view that query's full response.
 * Streaming queries show a pulsing animation and live timer.
 */

import { X, Loader2, CheckCircle, AlertCircle, ChevronDown, ChevronUp, Clock } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { QueryTask } from "@/hooks/useQueryTasks";
import { cn } from "@/lib/utils";
import { ACTION_ICON_BUTTON, PROGRESS_CARD } from "@/lib/outputStyle";

interface Props {
  tasks: QueryTask[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCancel: (id: string) => void;
  onRemove: (id: string) => void;
  collapsed?: boolean;  // v3.3f: whether the content panel is collapsed
}

// Estimated total time per mode (seconds) — calibrated to actual backend performance
const MODE_ETA: Record<string, number> = {
  auto: 30,
  light: 15,
  deep: 120,
  research: 240,
};

const LONG_WAIT_THRESHOLD_S: Record<string, number> = {
  deep: 60,
  research: 90,
};
const CONNECTION_TERMS = ["connect", "start", "\u8FDE\u63A5", "\u542F\u52A8"];
const SEARCH_TERMS = ["search", "\u641C\u7D22"];
const THINKING_TERMS = ["fan_out", "plan", "think", "\u601D\u8003"];
const VERIFY_TERMS = ["moa", "verify", "judge", "\u9A8C\u8BC1", "\u4EA4\u53C9\u9A8C\u8BC1"];
const SYNTHESIS_TERMS = ["synthesis", "\u7EFC\u5408"];
const POLISH_TERMS = ["refinement", "\u7CBE\u70BC"];

type Translate = (key: string, options?: Record<string, unknown>) => string;

function isWaitMode(task: QueryTask): boolean {
  return task.mode === "deep" || task.mode === "research";
}

function isLongWait(task: QueryTask): boolean {
  if (task.status !== "streaming" || !isWaitMode(task)) return false;
  return task.elapsed >= (LONG_WAIT_THRESHOLD_S[task.mode] ?? 120);
}

function containsAny(value: string, patterns: string[]): boolean {
  const normalized = value.toLowerCase();
  return patterns.some((pattern) => normalized.includes(pattern.toLowerCase()));
}

function patienceHint(task: QueryTask, t: Translate): string | null {
  if (!isLongWait(task)) return null;
  if (task.streamTokens) return t("components.queryProgressBars.patience.hasContent");
  if (containsAny(task.stage, SEARCH_TERMS)) return t("components.queryProgressBars.patience.searching");
  if (containsAny(task.stage, VERIFY_TERMS)) return t("components.queryProgressBars.patience.crossChecking");
  return t("components.queryProgressBars.patience.waiting");
}

function waitSummary(task: QueryTask, t: Translate): string {
  if (!isWaitMode(task)) return "";
  return task.stageDetail?.trim()
    || (task.mode === "research"
      ? t("components.queryProgressBars.waitSummary.research")
      : t("components.queryProgressBars.waitSummary.deep"));
}

function waitActionHint(task: QueryTask, t: Translate): string {
  if (!isWaitMode(task) || task.status !== "streaming") return "";
  if (isLongWait(task)) return t("components.queryProgressBars.waitAction.long");
  return t("components.queryProgressBars.waitAction.short");
}

function estimateRemaining(task: QueryTask, t: Translate): string {
  if (task.status !== "streaming") return "";
  const mode = task.mode?.toLowerCase() || "deep";
  const total = MODE_ETA[mode] || 180;
  const pct = progressPercent(task) / 100;
  // Remaining = total * (1 - progress%), at least 5s
  const remaining = Math.max(5, Math.round(total * (1 - pct)));
  if (remaining >= 60) {
    const m = Math.floor(remaining / 60);
    return t("components.queryProgressBars.eta.minutes", { count: m });
  }
  return t("components.queryProgressBars.eta.seconds", { count: remaining });
}

function statusBorder(status: QueryTask["status"]): string {
  switch (status) {
    case "streaming": return "border-l-sky-400";
    case "done":      return "border-l-emerald-400";
    case "error":     return "border-l-red-400";
  }
}

function statusIcon(status: QueryTask["status"]) {
  switch (status) {
    case "streaming": return <Loader2 size={14} className="animate-spin text-sky-300 shrink-0" />;
    case "done":      return <CheckCircle size={14} className="text-emerald-300 shrink-0" />;
    case "error":     return <AlertCircle size={14} className="text-red-300 shrink-0" />;
  }
}

function progressPercent(task: QueryTask): number {
  if (task.status === "done") return 100;
  if (task.status === "error") return 100;
  // Estimate progress from stage
  const stage = task.stage.toLowerCase();
  if (containsAny(stage, CONNECTION_TERMS)) return 10;
  if (containsAny(stage, SEARCH_TERMS)) return 20;
  if (containsAny(stage, THINKING_TERMS)) return 40;
  if (containsAny(stage, VERIFY_TERMS)) return 60;
  if (containsAny(stage, SYNTHESIS_TERMS)) return 75;
  if (containsAny(stage, POLISH_TERMS)) return 90;
  if (task.streamTokens) return 85;
  return 30;
}

function statusLabel(task: QueryTask, t: Translate): string {
  if (task.status === "done") return t("components.queryProgressBars.status.done");
  if (task.status === "error") return task.waitIssueCode === "user_cancelled" ? t("components.queryProgressBars.status.cancelled") : t("components.queryProgressBars.status.interrupted");
  if (isLongWait(task)) return t("components.queryProgressBars.status.longWait");
  return t("components.queryProgressBars.status.streaming");
}

export default function QueryProgressBars({ tasks, selectedId, onSelect, onCancel, onRemove, collapsed = false }: Props) {
  const { t } = useTranslation();
  if (tasks.length === 0) return null;

  return (
    <div className="space-y-2 px-2 pt-0 pb-0 sm:px-4 max-h-[30vh] overflow-y-auto shrink-0">
      <div className="mx-auto max-w-3xl space-y-2">
        {tasks.map((task) => {
          const isSelected = task.id === selectedId;
          const pct = progressPercent(task);

          return (
            <div
              key={task.id}
              onClick={() => onSelect(task.id)}
              className={cn(
                PROGRESS_CARD,
                "cursor-pointer border-l-2",
                statusBorder(task.status),
                isSelected
                  ? "ring-1 ring-oracle-500/30 shadow-[0_12px_26px_rgba(0,0,0,0.22)]"
                  : "hover:border-white/[0.1] hover:bg-zinc-800/70",
              )}
            >
              {/* Streaming progress fill */}
              {task.status === "streaming" && (
                <div
                  className="absolute inset-y-0 left-0 bg-gradient-to-r from-sky-500/26 via-sky-500/14 to-oracle-500/14 transition-all duration-1000 ease-out"
                  style={{ width: `${pct}%` }}
                />
              )}

              {/* Patience hint — shown after 5 min for deep/research */}
              {patienceHint(task, t) && (
                <div className="relative px-3 pb-1.5 pt-1">
                  <span className="flex items-center gap-1.5 text-[13px] text-amber-200">
                    <span>⏳</span>
                    <span>{patienceHint(task, t)}</span>
                  </span>
                </div>
              )}

              <div className="relative flex min-h-[52px] items-center gap-2.5 px-3 py-2">
                {statusIcon(task.status)}

                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-[14px] font-medium leading-tight text-zinc-100">
                      {task.question}
                    </span>
                    <span className={cn(
                      "shrink-0 rounded-full border px-2 py-0.5 text-[11px]",
                      task.status === "done"
                        ? "border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-300"
                        : task.status === "error"
                        ? "border-red-500/20 bg-red-500/[0.08] text-red-300"
                        : isLongWait(task)
                        ? "border-amber-500/20 bg-amber-500/[0.08] text-amber-200"
                        : "border-sky-500/20 bg-sky-500/[0.08] text-sky-200"
                    )}>
                      {statusLabel(task, t)}
                    </span>
                  </div>
                  {task.status === "streaming" && (
                    <p className="mt-1 truncate text-[12px] text-zinc-400">
                      {waitSummary(task, t) || task.stage}
                    </p>
                  )}
                </div>

                {task.status === "streaming" && (
                  <span className="hidden shrink-0 text-[13px] text-zinc-300 lg:inline">
                    {task.stage}
                  </span>
                )}

                {/* Model count hidden — don't expose contributor count */}

                {/* Time */}
                <span className={`flex shrink-0 items-center gap-1.5 text-[13px] font-medium tabular-nums ${
                  task.status === "streaming" ? "text-sky-200" : "text-zinc-300"
                }`}>
                  <Clock className="h-[12px] w-[12px]" />
                  {task.status === "streaming" ? estimateRemaining(task, t) : `${task.elapsed}s`}
                </span>

                {/* Expand indicator */}
                {isSelected
                  ? (collapsed
                    ? <ChevronDown className="h-[15px] w-[15px] shrink-0 text-oracle-300" />
                    : <ChevronUp className="h-[15px] w-[15px] shrink-0 text-oracle-300" />)
                  : <ChevronDown className="h-[15px] w-[15px] shrink-0 text-zinc-500" />
                }

                {/* Cancel / Remove */}
                <button
                  onClick={(e) => { e.stopPropagation(); task.status === "streaming" ? onCancel(task.id) : onRemove(task.id); }}
                  className={cn(ACTION_ICON_BUTTON, "h-9 w-9 shrink-0 rounded-xl border-white/[0.12] bg-black/10 text-zinc-300")}
                  title={task.status === "streaming" ? t("common.actions.cancel") : t("components.queryProgressBars.remove")}
                >
                  <X className="h-[14px] w-[14px]" />
                </button>
              </div>

              {task.status === "streaming" && isWaitMode(task) && isSelected && (
                <div className="relative border-t border-white/[0.08] px-3 pb-3 pt-2.5">
                  <p className="text-[13px] font-medium text-zinc-200 lg:hidden">{task.stage}</p>
                  <p className="mt-1 text-[14px] leading-7 text-zinc-200">{waitSummary(task, t)}</p>
                  {task.contributorsDone > 0 && (
                    <p className="mt-1 text-[13px] text-zinc-400">
                      {t("components.queryProgressBars.selected.contributorsDone", { count: task.contributorsDone })}
                    </p>
                  )}
                  <p className={cn(
                    "mt-1.5 text-[13px] leading-7",
                    isLongWait(task) ? "text-amber-200" : "text-zinc-300",
                  )}>
                    {waitActionHint(task, t)}
                  </p>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
