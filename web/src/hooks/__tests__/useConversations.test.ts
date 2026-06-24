import { describe, expect, it } from "vitest";

import { __testing, type Conversation } from "@/hooks/useConversations";
import type { HistoryItem } from "@/types";

function makeHistoryItem(overrides: Partial<HistoryItem> = {}): HistoryItem {
  return {
    query_id: "q-1",
    session_id: "sess-1",
    question: "What is MCP?",
    mode: "research",
    final_answer: "MCP is a model context protocol.",
    confidence: 0.82,
    quality_gate: "synthesized",
    contributor_count: 4,
    latency_ms: 1234,
    estimated_cost_usd: 0.12,
    created_at: new Date(1_000).toISOString(),
    has_divergence: false,
    divergence_summary: "",
    key_insights: [],
    divergence_points: [],
    best_single_answer: "",
    ...overrides,
  };
}

describe("mergeHistoryItemsIntoConversations", () => {
  it("fills the assistant answer back into a local question-only conversation", () => {
    const localConversation: Conversation = {
      id: "local-1",
      title: "What is MCP?",
      messages: [
        { role: "user", content: "What is MCP?", timestamp: 1_000 },
      ],
      sessionId: null,
      turnCount: 0,
      createdAt: 1_000,
      updatedAt: 1_000,
    };

    const merged = __testing.mergeHistoryItemsIntoConversations(
      [localConversation],
      [makeHistoryItem()],
    ).conversations;

    expect(merged).toHaveLength(1);
    expect(merged[0].id).toBe("sess-sess-1");
    expect(merged[0].sessionId).toBe("sess-1");
    expect(merged[0].messages).toHaveLength(2);
    expect(merged[0].messages[1].role).toBe("assistant");
    expect(merged[0].messages[1].content).toBe("MCP is a model context protocol.");
    expect(merged[0].messages[1].response?.query_id).toBe("q-1");
  });

  it("updates an existing assistant turn instead of appending duplicates", () => {
    const existingHistory = makeHistoryItem();
    const existingConversation = __testing.createConversationFromHistoryItem(existingHistory);

    const merged = __testing.mergeHistoryItemsIntoConversations(
      [existingConversation],
      [makeHistoryItem({ final_answer: "Updated answer." })],
    ).conversations;

    expect(merged).toHaveLength(1);
    expect(merged[0].messages).toHaveLength(2);
    expect(merged[0].messages[1].content).toBe("Updated answer.");
  });
});
