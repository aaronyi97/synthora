import CompanionBubble, { type CompanionAction, type CompanionGuideData } from "@/components/query/CompanionBubble";
import type { AskResponse, GuidanceOutput } from "@/types";

interface Props {
  response: AskResponse;
  onCompanionAction: (action: CompanionAction) => void;
  isHistory?: boolean;
}

function normalizeTrigger(trigger: string | undefined): CompanionGuideData["trigger"] {
  if (trigger === "divergence" || trigger === "low_confidence") return trigger;
  return "fold";
}

function legacyCompanionToGuidance(response: AskResponse): GuidanceOutput | null {
  const guide = response.companion_guide;
  if (!guide) return null;
  const actions = Array.isArray(guide.actions) ? guide.actions : [];
  return {
    source: "dispatcher",
    confidence_statement: "",
    confidence_level: "medium",
    message: guide.message ?? "",
    suggestions: actions.map((action, index) => ({
      id: `${response.query_id || "legacy-companion"}-${index}`,
      label: action.label,
      action_type: action.action_type,
      action_payload: action.action_payload ?? {},
      rationale: "",
      estimated_seconds: action.estimated_seconds ?? 0,
      estimated_cost_usd: 0,
      requires_confirm: "requires_confirm" in action ? Boolean((action as { requires_confirm?: boolean }).requires_confirm) : false,
    })),
    intensity: actions.length > 0 ? "rich" : "light",
    is_folded: normalizeTrigger(guide.trigger) === "fold",
    show_dismiss: actions.length > 0,
    route_reason: guide.route_reason ?? "",
    trigger: normalizeTrigger(guide.trigger),
  };
}

export function resolveResponseGuidance(response: AskResponse): GuidanceOutput | null {
  const canonical = response.guidance;
  if (canonical?.source === "dispatcher") {
    return canonical;
  }
  return legacyCompanionToGuidance(response);
}

function toCompanionGuide(guidance: GuidanceOutput): CompanionGuideData {
  return {
    message: guidance.message ?? "",
    actions: (guidance.suggestions ?? []).map((suggestion) => ({
      label: suggestion.label,
      action_type: suggestion.action_type,
      action_payload: suggestion.action_payload ?? {},
      estimated_seconds: suggestion.estimated_seconds ?? 0,
      requires_confirm: suggestion.requires_confirm ?? false,
    })),
    trigger: normalizeTrigger(guidance.trigger),
    is_silent: false,
    route_reason: guidance.route_reason ?? "",
  };
}

export default function PostAnswerGuidance({
  response,
  onCompanionAction,
  isHistory,
}: Props) {
  const guidance = resolveResponseGuidance(response);
  if (!guidance || guidance.source === "none") return null;

  return (
    <CompanionBubble
      guide={toCompanionGuide(guidance)}
      onAction={onCompanionAction}
      isHistory={isHistory}
    />
  );
}
