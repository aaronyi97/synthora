// ════════════════════════════════════════════════════════
// Synthora Frontend Types
//
// API types: auto-generated from backend Pydantic models.
//   Source: web/src/types/generated.ts (via scripts/generate-types.sh)
//   Pipeline: Pydantic → OpenAPI JSON → openapi-typescript → TS types
//   禁令 #9: 禁止手写双份契约。修改 API 类型请改 app.py 然后重新生成。
//
// Manual types: frontend-only types not in API contract.
// ════════════════════════════════════════════════════════

import type { components } from "./generated";

// ── Auto-generated API types (from Pydantic models in app.py) ──

export type AskRequest = components["schemas"]["AskRequest"] & {
  file_ids?: string[];  // v3.2: multimodal attachments
};
export type DraftAnswerItem = components["schemas"]["DraftAnswerModel"];

export type SearchCitation = components["schemas"]["SearchCitationModel"];

export type AskResponse = components["schemas"]["AskResponse"] & {
  guidance?: GuidanceOutput | null;  // v5.2: canonical guidance protocol (single source of truth)
};
export type ModelResponse = components["schemas"]["ModelResponseItem"];
export type CompanionHint = components["schemas"]["CompanionHintModel"];
export type ClarificationNeeded = components["schemas"]["ClarificationNeededModel"];
export type LowConfidenceAction = components["schemas"]["LowConfidenceActionModel"];

// ── v5.2: Canonical Guidance Protocol ──
export type GuidanceOutput = components["schemas"]["GuidanceModel"];

// ── Mixed types: generated contracts + frontend-only types ──

export type Mode = "auto" | "light" | "deep" | "research" | "socratic" | "roundtable";

export interface HealthResponse {
  conversation_store?: string | null;
  [key: string]: unknown;
}

export type SocraticStartResponse = components["schemas"]["SocraticStartResponse"];

export type SocraticRespondResponse = components["schemas"]["SocraticRespondResponse"];

export type SocraticRevealResponse = components["schemas"]["SocraticRevealResponse"];

export type DivergencePoint = components["schemas"]["SocraticRevealDivergencePoint"];

export type DivergencePosition = components["schemas"]["DivergencePosition"];

export type CognitiveSnapshot = components["schemas"]["CognitiveSnapshot"];

export type AuthResponse = components["schemas"]["AuthResponse"];

export type UserProfile = components["schemas"]["AuthMeResponse"];

export type HistoryItem = components["schemas"]["HistoryItem"];

export type CognitiveSummaryResponse = components["schemas"]["CognitiveSummaryResponse"];

export type BehaviorSummaryResponse = components["schemas"]["BehaviorSummaryResponse"];

export interface ApiError {
  error_code?: string;
  status?: number;
  detail: string;
}

export type GrowthDashboardResponse = components["schemas"]["GrowthResponse"];

// ── Capability Map & Improvement Plans (ProactiveCoachService) ──

export type CapabilityTopicItem = components["schemas"]["CapabilityTopicItem"];

export type ImprovementPlanSummary = components["schemas"]["ImprovementPlanSummary"];

export type ImprovementPlanRecord = components["schemas"]["ImprovementPlanRecord"];

export type ImprovementPlanActionResponse = components["schemas"]["ImprovementPlanActionResponse"];

export type CapabilityMapResponse = components["schemas"]["CapabilityMapResponse"];

// ── Mode Metadata (from /modes endpoint — single source of truth) ──

export type ModeInfo = components["schemas"]["ModeInfo"];

export type ModesResponse = components["schemas"]["ModesResponse"];

export type UsageResponse = components["schemas"]["UsageResponse"];

export type QuotaResponse = components["schemas"]["QuotaResponse"];

export type RoundtableResumeResponse = components["schemas"]["RoundtableResumeResponse"];

export type CognitiveConsentResponse = components["schemas"]["CognitiveConsentResponse"];

export type DeleteCognitiveResponse = components["schemas"]["DeleteCognitiveResponse"];

export type RecentTurnsResponse = components["schemas"]["RecentTurnsResponse"];

// ── Frontend-only types (not in API) ──

export interface ChatTurn {
  role: "guide" | "user";
  content: string;
}
