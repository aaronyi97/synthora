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

import PostAnswerGuidance from "@/components/query/PostAnswerGuidance";
import type { AskResponse } from "@/types";

const baseResponse: AskResponse = {
  query_id: "qid-guidance",
  question: "这个问题接下来怎么办？",
  mode: "deep",
  final_answer: "这是回答",
  confidence: 0.82,
  quality_gate: "synthesized",
  has_divergence: false,
  divergence_summary: null,
  key_insights: [],
  latency_ms: 1200,
  estimated_cost_usd: 0.01,
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

describe("PostAnswerGuidance", () => {
  it("优先渲染 canonical dispatcher guidance，不回落到 legacy companion_guide", () => {
    render(
      <PostAnswerGuidance
        response={{
          ...baseResponse,
          guidance: {
            source: "dispatcher",
            confidence_statement: "",
            confidence_level: "medium",
            message: "",
            suggestions: [],
            intensity: "none",
            is_folded: true,
            show_dismiss: false,
            route_reason: "",
            trigger: "fold",
          },
          companion_guide: {
            message: "legacy guidance",
            actions: [],
            trigger: "fold",
            is_silent: false,
          },
        }}
        onCompanionAction={vi.fn()}
      />
    );

    expect(screen.getByText(translateZh("components.companionBubble.foldLabel"))).toBeInTheDocument();
    expect(screen.queryByText("legacy guidance")).not.toBeInTheDocument();
  });

  it("canonical guidance 缺失时回落到 legacy companion bubble", () => {
    render(
      <PostAnswerGuidance
        response={{
          ...baseResponse,
          companion_guide: {
            message: "legacy guidance",
            actions: [],
            trigger: "divergence",
            is_silent: false,
          },
        }}
        onCompanionAction={vi.fn()}
      />
    );

    expect(screen.getByText(translateZh("components.companionBubble.header"))).toBeInTheDocument();
    expect(screen.getByText("legacy guidance")).toBeInTheDocument();
  });
});
