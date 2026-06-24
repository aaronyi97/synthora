import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createZhI18nMock, translateZh } from "@/test/i18nMock";

vi.mock("react-i18next", async () => {
  const actual = await vi.importActual<typeof import("react-i18next")>("react-i18next");

  return {
    ...actual,
    useTranslation: () => ({
      t: translateZh,
      i18n: createZhI18nMock(),
    }),
  };
});

import ResponseDisplay from "@/components/query/ResponseDisplay";
import type { AskResponse } from "@/types";

const baseResponse: AskResponse = {
  query_id: "qid-response",
  question: "为什么会这样？",
  mode: "deep",
  final_answer: "这是最终回答。",
  confidence: 0.72,
  quality_gate: "synthesized",
  has_divergence: false,
  divergence_summary: null,
  key_insights: [],
  latency_ms: 1500,
  estimated_cost_usd: 0.02,
  contributor_count: 3,
  individual_responses: null,
  companion_hint: null,
  preflight: null,
  pipeline_started: true,
  fast_path: false,
  low_confidence_actions: [],
  session_id: null,
  context_compressed: false,
  search_citations: [],
  draft_answers: [],
  divergence_points: [],
  consensus_points: [],
  fact_warnings: [],
  companion_guide: null,
  consensus_type: "unknown",
  guidance: null,
  reason_code: "standard",
  ai_disclosure: "AI",
};

describe("ResponseDisplay", () => {
  it("single_model_fast_path 不再显示 degraded warning", () => {
    render(
      <ResponseDisplay
        response={{
          ...baseResponse,
          quality_gate: "best_single",
          fast_path: true,
          reason_code: "single_model_fast_path",
        }}
        onAction={vi.fn()}
        onRetry={vi.fn()}
      />
    );

    expect(screen.queryByText(translateZh("components.responseDisplay.degradedWarning"))).not.toBeInTheDocument();
  });

  it("low_confidence 仍显示 warning，且 guidance 存在时不重复渲染 low_confidence_actions", () => {
    render(
      <ResponseDisplay
        response={{
          ...baseResponse,
          quality_gate: "low_confidence",
          reason_code: "low_confidence",
          low_confidence_actions: [{ action: "retry_deep", label: "Retry Deep" }],
          guidance: {
            source: "dispatcher",
            confidence_statement: "",
            confidence_level: "low",
            message: "Try a different angle.",
            suggestions: [{
              id: "g-step",
              label: "Ask from another angle",
              action_type: "query_followup",
              action_payload: { question: "Ask from another angle" },
              rationale: "",
              estimated_seconds: 30,
              estimated_cost_usd: 0,
              requires_confirm: false,
            }],
            intensity: "light",
            is_folded: false,
            show_dismiss: true,
            route_reason: "There are still disagreements worth exploring.",
            trigger: "fold",
          },
        }}
        onAction={vi.fn()}
        onRetry={vi.fn()}
      />
    );

    expect(screen.getByText(translateZh("components.responseDisplay.degradedWarning"))).toBeInTheDocument();
    expect(screen.queryByText("Retry Deep")).not.toBeInTheDocument();
  });

  it("research best_single 使用不误导的直采文案", () => {
    render(
      <ResponseDisplay
        response={{
          ...baseResponse,
          mode: "research",
          quality_gate: "best_single",
        }}
        onAction={vi.fn()}
        onRetry={vi.fn()}
      />
    );

    expect(screen.getByText(translateZh("components.responseDisplay.research.directResult"))).toBeInTheDocument();
    expect(screen.getByText(translateZh("components.responseDisplay.bestSingle.research"))).toBeInTheDocument();
    expect(screen.queryByText(translateZh("components.responseDisplay.research.report"))).not.toBeInTheDocument();
  });
});
