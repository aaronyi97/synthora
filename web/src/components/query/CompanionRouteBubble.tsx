/**
 * CompanionRouteBubble — Dispatcher route status indicator (v5.1)
 *
 * Two modes:
 * 1. Normal (auto_execute_seconds=0, no actions): purely informational status.
 *    Pipeline is already running — no controls.
 * 2. Timeout/failure (actions present, auto_execute_seconds>0): actionable.
 *    Shows action buttons + countdown to auto-execute first action.
 *
 * Data source: companion_route SSE event from Dispatcher pre-route or UX-1 timeout
 */
import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  ACTION_CHIP_GHOST,
  ACTION_CHIP_PRIMARY,
  ACTION_CHIP_SECONDARY,
  OUTPUT_META_PILL,
  OUTPUT_SURFACE_SOFT,
  STATUS_BADGE_ACCENT,
} from "@/lib/outputStyle";

interface RouteAction {
  label: string;
  capability_label?: string;
  model_label?: string;
  action_type: string;
  action_payload?: Record<string, unknown>;
  estimated_seconds?: number;
}

interface CompanionRouteData {
  message: string;
  actions: RouteAction[];
  more_actions?: RouteAction[];
  route_reason: string;
  auto_execute_seconds: number;
  is_silent: boolean;
  resolved_mode?: string;
  contributor_count?: number;
}

interface Props {
  route: CompanionRouteData;
  onAction?: (action: RouteAction) => void;
}

function actionClass(actionType: string): string {
  const lower = actionType.toLowerCase();
  if (lower.includes("light")) return ACTION_CHIP_PRIMARY;
  if (lower.includes("cancel")) return `${ACTION_CHIP_GHOST} text-red-300 hover:text-red-200`;
  return ACTION_CHIP_SECONDARY;
}

function actionSubtitle(action: RouteAction): string | null {
  const parts = [action.capability_label, action.model_label].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : null;
}

function translateResolvedMode(mode: string | undefined, t: (key: string) => string): string {
  switch (mode) {
    case "auto":
      return "Auto";
    case "light":
      return "Light";
    case "deep":
      return "Deep";
    case "research":
      return "Research";
    case "socratic":
      return t("common.modes.socraticName");
    case "roundtable":
      return t("common.modes.roundtableName");
    default:
      return mode || t("components.companionRouteBubble.routeStatus.analyzing");
  }
}

function describeAction(action: RouteAction, t: (key: string) => string): string | null {
  const payload = action.action_payload || {};
  const mode = typeof payload.mode === "string" ? payload.mode : "";
  if (action.action_type === "query_light" || mode === "light") return t("components.companionRouteBubble.actionDescription.queryLight");
  if (action.action_type === "query_deep" || mode === "deep") return t("components.companionRouteBubble.actionDescription.queryDeep");
  if (action.action_type === "query_single") return t("components.companionRouteBubble.actionDescription.querySingle");
  if (action.action_type === "cancel") return t("components.companionRouteBubble.actionDescription.cancel");
  return null;
}

export default function CompanionRouteBubble({ route, onAction }: Props) {
  const { t } = useTranslation();
  const hasActions = Array.isArray(route.actions) && route.actions.length > 0;
  const hasMoreActions = Array.isArray(route.more_actions) && route.more_actions.length > 0;
  const [countdown, setCountdown] = useState(() => Math.max(0, Math.floor(route.auto_execute_seconds)));
  const [showMoreActions, setShowMoreActions] = useState(false);
  const autoExecutedRef = useRef<string | null>(null);

  const routeKey = [
    route.message,
    route.route_reason,
    route.resolved_mode,
    route.auto_execute_seconds,
    route.actions.map((action) => `${action.action_type}:${action.label}`).join("|"),
    (route.more_actions ?? []).map((action) => `${action.action_type}:${action.label}`).join("|"),
  ].join("::");

  useEffect(() => {
    setCountdown(Math.max(0, Math.floor(route.auto_execute_seconds)));
    setShowMoreActions(false);
    autoExecutedRef.current = null;
  }, [routeKey, route.auto_execute_seconds]);

  useEffect(() => {
    if (!hasActions || !onAction || route.auto_execute_seconds <= 0) return;
    if (countdown <= 0) {
      if (autoExecutedRef.current !== routeKey) {
        autoExecutedRef.current = routeKey;
        onAction(route.actions[0]);
      }
      return;
    }

    const intervalId = window.setInterval(() => {
      setCountdown((current) => Math.max(0, current - 1));
    }, 1000);

    return () => window.clearInterval(intervalId);
  }, [countdown, hasActions, onAction, route.actions, route.auto_execute_seconds, routeKey]);

  const handleAction = (action: RouteAction) => {
    autoExecutedRef.current = routeKey;
    setCountdown(0);
    onAction?.(action);
  };

  // Silent route — don't render
  if (route.is_silent) return null;

  // Nothing meaningful to show
  if (!route.message && !route.route_reason && !route.resolved_mode) return null;

  return (
    <div className="mt-2 animate-fade-in">
      <div className={`${OUTPUT_SURFACE_SOFT} px-3.5 py-3`}>
        <div className="flex items-start gap-2.5">
        {/* Avatar */}
          <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-oracle-500/15 text-oracle-400">
            <Sparkles size={13} className="animate-pulse" />
          </div>

          {/* Content */}
          <div className="min-w-0 max-w-[90%]">
            {/* Route status line */}
            <div className="flex flex-wrap items-center gap-1.5">
              <span className={`${STATUS_BADGE_ACCENT} text-[13px] text-zinc-100`}>
                <span>{t("components.companionRouteBubble.routeStatus.label")}</span>
                <span className="text-oracle-400/60">→</span>
                <span>{translateResolvedMode(route.resolved_mode, t)}</span>
              </span>
              {(route.contributor_count ?? 0) > 0 && (
                <span className="text-[13px] text-zinc-300">· {t("components.companionRouteBubble.routeStatus.models", { count: route.contributor_count })}</span>
              )}
              <span className="inline-block h-1 w-1 rounded-full bg-oracle-400/60 animate-pulse" />
            </div>

            {route.route_reason && (
              <p className="mt-1 text-[13px] italic text-zinc-300">
                {route.route_reason}
              </p>
            )}
            {route.message && (
              <p className="mt-1.5 text-[13px] leading-relaxed text-zinc-200">
                {route.message}
              </p>
            )}
            <p className="mt-1 text-[12px] leading-relaxed text-zinc-400">
              {t("components.companionRouteBubble.routeStatus.runningHint")}
            </p>

            {hasActions && onAction && (
              <div className="mt-2.5 space-y-2">
                <p className="text-[13px] text-zinc-300">
                  {t("components.companionRouteBubble.routeStatus.switchHint")}
                </p>
                <div className="flex flex-wrap gap-2">
                  {route.actions.map((action, index) => {
                    const subtitle = actionSubtitle(action);
                    const description = describeAction(action, t);
                    return (
                      <button
                        key={`${action.action_type}-${index}`}
                        type="button"
                        aria-label={action.label}
                        onClick={() => handleAction(action)}
                        className={`${actionClass(action.action_type)} flex flex-col items-start text-left`}
                      >
                        <span>{action.label}</span>
                        {description && (
                          <span className="text-[12px] font-normal text-zinc-400">
                            {description}
                          </span>
                        )}
                        {subtitle && (
                          <span className="text-[13px] font-normal text-zinc-300">
                            {subtitle}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
                {hasMoreActions && (
                  <div className="space-y-2">
                    <button
                      type="button"
                      onClick={() => setShowMoreActions((current) => !current)}
                      className={`${OUTPUT_META_PILL} inline-flex items-center gap-1.5 border-white/[0.12] bg-white/[0.05] text-[13px] normal-case tracking-normal text-zinc-200 hover:border-white/[0.18] hover:text-zinc-50`}
                    >
                      {showMoreActions ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      <span>{t("components.companionRouteBubble.moreOptions", { count: route.more_actions!.length })}</span>
                    </button>
                    {showMoreActions && (
                      <div className="flex flex-wrap gap-2">
                        {route.more_actions!.map((action, index) => {
                          const subtitle = actionSubtitle(action);
                          const description = describeAction(action, t);
                          return (
                            <button
                              key={`more-${action.action_type}-${index}`}
                              type="button"
                              aria-label={action.label}
                              onClick={() => handleAction(action)}
                              className={`${actionClass(action.action_type)} flex flex-col items-start text-left`}
                            >
                              <span>{action.label}</span>
                              {description && (
                                <span className="text-[12px] font-normal text-zinc-400">
                                  {description}
                                </span>
                              )}
                              {subtitle && (
                                <span className="text-[13px] font-normal text-zinc-300">
                                  {subtitle}
                                </span>
                              )}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}
                {countdown > 0 && route.auto_execute_seconds > 0 && (
                  <p className="text-[13px] text-zinc-300">
                    {t("components.companionRouteBubble.autoContinue", { count: countdown })}
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
