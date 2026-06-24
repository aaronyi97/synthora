/**
 * CompanionBubble — Dispatcher post-guide UI component (v5.1)
 *
 * Renders the AI companion's contextual guidance after a query completes.
 * Two states:
 *   1. Folded (default): collapsed single-line with expand affordance
 *   2. Expanded: message + action buttons
 *
 * Data source: canonical response.guidance (dispatcher source) or legacy companion_guide fallback
 * Fallback: hidden when there is no renderable dispatcher guidance
 */
import { useState } from "react";
import { Sparkles, ChevronDown, ChevronUp } from "lucide-react";
import { useTranslation } from "react-i18next";

export interface CompanionGuideData {
  message: string;
  actions: CompanionAction[];
  trigger: "fold" | "divergence" | "low_confidence";
  is_silent: boolean;
  route_reason?: string;
}

export interface CompanionAction {
  label: string;
  capability_label?: string;
  model_label?: string;
  action_type: string;
  action_payload?: Record<string, unknown>;
  estimated_seconds?: number;
  requires_confirm?: boolean;
}

interface Props {
  guide: CompanionGuideData;
  onAction: (action: CompanionAction) => void;
  isHistory?: boolean;
}

function formatEta(seconds: number | undefined, t: (key: string, options?: Record<string, unknown>) => string): string | null {
  if (!seconds || seconds <= 0) return null;
  if (seconds >= 60) return t("components.companionBubble.eta.minutes", { count: Math.round(seconds / 60) });
  return t("components.companionBubble.eta.seconds", { count: seconds });
}

function describeAction(action: CompanionAction, t: (key: string) => string): string | null {
  const payload = action.action_payload || {};
  const mode = typeof payload.mode === "string" ? payload.mode : "";
  if (action.action_type === "query_single") return t("components.companionBubble.actionDescription.querySingle");
  if (action.action_type === "roundtable") return t("components.companionBubble.actionDescription.roundtable");
  if (action.action_type === "query_followup") return t("components.companionBubble.actionDescription.queryFollowup");
  if (mode === "deep") return t("components.companionBubble.actionDescription.modeDeep");
  if (mode === "research") return t("components.companionBubble.actionDescription.modeResearch");
  if (mode === "light") return t("components.companionBubble.actionDescription.modeLight");
  return null;
}

export default function CompanionBubble({ guide, onAction, isHistory }: Props) {
  const { t } = useTranslation();
  // Auto-expand for high-value triggers, fold by default
  const isHighValue = guide.trigger === "divergence" || guide.trigger === "low_confidence";
  const [expanded, setExpanded] = useState(isHighValue);

  // Silent routes: completely hidden
  if (guide.is_silent && guide.trigger === "fold" && !guide.message && guide.actions.length === 0) {
    return null;
  }

  // Fold with no content: render collapsed one-liner placeholder
  const isFoldOnly = guide.trigger === "fold" && !guide.message && guide.actions.length === 0;

  return (
    <div className="mt-3 animate-fade-in">
      {isFoldOnly ? (
        /* Collapsed fold placeholder — v3 §6.4 */
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-center gap-2 text-[12px] text-zinc-400 hover:text-zinc-200 transition-colors"
        >
          <span className="flex-1 border-t border-zinc-700/50" />
          <Sparkles size={11} className="text-oracle-300/80" />
          <span className="text-oracle-300/80">{t("components.companionBubble.foldLabel")}</span>
          {expanded
            ? <ChevronUp size={11} />
            : <ChevronDown size={11} />}
          <span className="flex-1 border-t border-zinc-700/50" />
        </button>
      ) : (
        <div className="flex items-start gap-2.5">
          {/* Avatar */}
          <div className="w-7 h-7 rounded-full bg-oracle-500/15 text-oracle-400 flex items-center justify-center shrink-0 mt-0.5">
            <Sparkles size={13} />
          </div>

          {/* Content */}
          <div className="min-w-0 max-w-[90%]">
            {/* Toggle header */}
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="flex items-center gap-1.5 text-[12px] text-oracle-300 hover:text-oracle-200 transition-colors"
            >
              <span>{t("components.companionBubble.header")}</span>
              {expanded
                ? <ChevronUp size={11} className="text-zinc-400" />
                : <ChevronDown size={11} className="text-zinc-400" />}
            </button>

            {/* Expanded content */}
            {expanded && (
              <div className="mt-1.5">
                {guide.route_reason && (
                  <p className="mb-1 text-[12px] italic text-zinc-400">
                    {guide.route_reason}
                  </p>
                )}
                {guide.message && (
                  <p className="text-[13px] text-zinc-200 leading-relaxed">
                    {guide.message}
                  </p>
                )}

                {guide.actions.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {guide.actions.map((action, idx) => {
                      const etaLabel = formatEta(action.estimated_seconds, t);
                      const actionLabel = describeAction(action, t);
                      const metaLabel = [action.capability_label, action.model_label].filter(Boolean).join(" · ");
                      return (
                        <button
                          key={`companion-action-${idx}`}
                          type="button"
                          onClick={() => onAction(action)}
                          className={
                            isHistory
                              ? "flex flex-col items-start gap-0.5 rounded-lg border border-zinc-600/40 bg-zinc-800/60 px-3 py-1.5 text-[13px] text-zinc-300 opacity-60 transition-all hover:border-zinc-500/60 hover:opacity-85"
                              : "flex flex-col items-start gap-0.5 rounded-lg border border-oracle-500/30 bg-oracle-500/12 px-3 py-1.5 text-[13px] text-oracle-200 transition-all hover:bg-oracle-500/22 active:scale-95"
                          }
                        >
                          <span className="flex items-center gap-1.5">
                            <span>{action.label}</span>
                          </span>
                          {actionLabel && (
                            <span className="text-[12px] text-zinc-400">
                              {actionLabel}
                            </span>
                          )}
                          {(etaLabel || metaLabel) && (
                            <span className="text-[12px] text-zinc-500">
                              {[etaLabel, metaLabel].filter(Boolean).join(" · ")}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
