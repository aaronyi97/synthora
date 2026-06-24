import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import { createZhI18nMock, translateZh } from "@/test/i18nMock";
import RoundtablePage from "../RoundtablePage";

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

vi.mock("@/i18n", async () => {
  const { createZhI18nMock } = await import("@/test/i18nMock");
  return { default: createZhI18nMock() };
});

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  const realRoundtableCheck = actual.api.roundtableCheck.bind(actual.api);
  return {
    ...actual,
    api: Object.assign(actual.api, {
      roundtableResume: vi.fn(),
      roundtableCheck: vi.fn(realRoundtableCheck),
      roundtableStream: vi.fn(),
      roundtableChoice: vi.fn(),
    }),
  };
});

const disputeMap = {
  synthesized_dimensions: ["风险", "收益"],
  dimension_sources: { risk: ["专家A"] },
  contention_points: [{
    topic: "火星移民",
    severity: "high",
    dispute_type: ["value"],
    factual_aspect: "成本",
    value_aspect: "长期收益",
    dimension_id: "risk",
    dimension_label: "风险",
    dimension_aliases: [],
    adjudication_note: "",
    sides: [
      { position: "继续投入", supporting_claims: ["长期价值"], lead_expert: "专家A", main_argument: "值得押注" },
      { position: "先观望", supporting_claims: ["短期风险高"], lead_expert: "专家B", main_argument: "现在不该继续投" },
    ],
    why_it_matters: "这决定了投入节奏",
    suggested_focus: true,
  }],
  consensus_points: [],
  suggested_focus: "风险",
  echo_chamber_warning: "",
  clarifying_questions: [],
};

const ACTIVE_SESSION_QUESTION = "Is Musk's Mars plan realistic?";
const LONG_TERM_MOAT = "Long-term moat";
const RECOMMENDED_ACTION = "Proceed conservatively";

const ROUNDTABLE_INTERACTIVE = true;
const describeInteractive = ROUNDTABLE_INTERACTIVE ? describe : describe.skip;

function renderRoundtablePage(url: string, state?: Record<string, unknown>) {
  window.history.pushState(state ?? {}, "", url);
  const parsed = new URL(url, "http://localhost");
  return render(
    <MemoryRouter initialEntries={[{ pathname: parsed.pathname, search: parsed.search, state }]}>
      <Routes>
        <Route path="/roundtable" element={<RoundtablePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RoundtablePage", () => {
  beforeEach(() => {
    cleanup();
    vi.resetAllMocks();
    vi.unstubAllGlobals();
    const storage = new Map<string, string>([["synthora_language", "zh-CN"]]);
    const localStorageMock = {
      getItem: vi.fn((key: string) => storage.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => {
        storage.set(key, value);
      }),
      removeItem: vi.fn((key: string) => {
        storage.delete(key);
      }),
      clear: vi.fn(() => {
        storage.clear();
      }),
      key: vi.fn((index: number) => Array.from(storage.keys())[index] ?? null),
      get length() {
        return storage.size;
      },
    };
    vi.stubGlobal("localStorage", localStorageMock);
    sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  describeInteractive("interactive resume flows", () => {
    it("session_active 恢复时会带回问题文本和 choice UI", async () => {
      vi.mocked(api.roundtableStream).mockResolvedValueOnce(undefined as never);
      vi.mocked(api.roundtableResume).mockResolvedValueOnce({
        status: "session_active",
        session_id: "sid-active",
        state: "awaiting_A",
        choice_point: "A",
        state_snapshot: {
          question: ACTIVE_SESSION_QUESTION,
          expert_count: 2,
          experts: [{
            model_id: "claude_opus_thinking",
            label: "专家A",
            stance: "支持",
            confidence: 0.7,
            my_dimensions: [LONG_TERM_MOAT, "投入节奏"],
            claims: [],
            risk_warning: "",
            blind_spot_warning: "",
            challenge_to_others: "",
            raw_response: "",
            structured: true,
            success: true,
            error: "",
            latency_ms: 1200,
          }],
          dispute_map: disputeMap,
          rebuttals: [],
          debate_round: 0,
          choice_point: "A",
        },
      } as never);

      renderRoundtablePage("/roundtable?session_id=sid-active");

      expect(await screen.findByText(ACTIVE_SESSION_QUESTION)).toBeInTheDocument();
      expect(await screen.findByText(translateZh("pages.roundtable.spotlight.moderatorSuggestion"))).toBeInTheDocument();
      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.primaryA.title"))).toBeInTheDocument();
      const valueGuide = await screen.findByText(translateZh("pages.roundtable.spotlight.dependsOnYou"));
      expect(valueGuide).toBeInTheDocument();
      expect(valueGuide.parentElement?.textContent).toContain("长期收益");
      await waitFor(() => {
        expect(api.roundtableStream).toHaveBeenCalledWith(
          {
            question: ACTIVE_SESSION_QUESTION,
            session_id: "sid-active",
          },
          expect.any(Object),
          expect.any(AbortSignal),
        );
      });
    });

    it("choice 失败时显式报错并回滚到恢复后的 phase", async () => {
      vi.mocked(api.roundtableResume)
        .mockResolvedValueOnce({
          status: "session_active",
          session_id: "sid-active",
          state: "awaiting_A",
          choice_point: "A",
          state_snapshot: {
            question: "火星计划值得继续投吗？",
            expert_count: 2,
            experts: [],
            dispute_map: disputeMap,
            rebuttals: [],
            debate_round: 0,
            choice_point: "A",
          },
        } as never)
        .mockResolvedValueOnce({
          status: "session_active",
          session_id: "sid-active",
          state: "awaiting_B",
          choice_point: "B",
          state_snapshot: {
            question: "火星计划值得继续投吗？",
            expert_count: 2,
            experts: [],
            dispute_map: disputeMap,
            rebuttals: [],
            debate_round: 1,
            choice_point: "B",
          },
        } as never);
      vi.mocked(api.roundtableChoice).mockRejectedValueOnce({
        detail: { error: "choice_point_mismatch", current_state: "awaiting_B" },
      });

      renderRoundtablePage("/roundtable?session_id=sid-active");

      fireEvent.click(await screen.findByText(translateZh("pages.roundtable.userActionPanel.secondaryA.title")));

      await waitFor(() => {
        expect(api.roundtableChoice).toHaveBeenCalled();
      });
      expect(await screen.findByText(translateZh("pages.roundtable.errors.phaseChanged", { state: "awaiting_B" }))).toBeInTheDocument();
      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.promptB"))).toBeInTheDocument();
      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.primaryB.title"))).toBeInTheDocument();
      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.secondaryB.title"))).toBeInTheDocument();
    });
  });

  describeInteractive("interactive rendering", () => {
    it("进入 choice-point A 时会渲染 UserActionPanel", async () => {
      vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
      vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
      vi.mocked(api.roundtableStream).mockImplementationOnce(async (_request, handlers) => {
        handlers.onStarted?.({
          session_id: "sid-active",
          expert_count: 2,
          question: "火星计划值得继续投吗？",
        });
        handlers.onDisputesMapped?.(disputeMap);
        handlers.onAwaitingUserChoice?.({
          choice_point: "A",
          timeout_s: 60,
          default_action: "conclude",
        });
      });

      renderRoundtablePage("/roundtable?q=%E7%81%AB%E6%98%9F%E8%AE%A1%E5%88%92%E5%80%BC%E5%BE%97%E7%BB%A7%E7%BB%AD%E6%8A%95%E5%90%97%EF%BC%9F");

      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.promptA"))).toBeInTheDocument();
      expect(screen.getByText(translateZh("pages.roundtable.userActionPanel.primaryA.title"))).toBeInTheDocument();
      expect(screen.getByText(translateZh("pages.roundtable.userActionPanel.secondaryA.title"))).toBeInTheDocument();
      expect(screen.getByText(translateZh("pages.roundtable.userActionPanel.tertiaryTitleA"))).toBeInTheDocument();
    });
  });

  it("带 location.state q 时自动调用 roundtableCheck 和 roundtableStream", async () => {
    vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
    vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
    vi.mocked(api.roundtableStream).mockResolvedValueOnce(undefined as never);

    renderRoundtablePage("/roundtable", { q: "测试问题", autoStart: true });

    await waitFor(() => {
      expect(api.roundtableCheck).toHaveBeenCalledWith("测试问题");
    });
    await waitFor(() => {
      expect(api.roundtableStream).toHaveBeenCalled();
    });
    expect(screen.queryByText(translateZh("pages.roundtable.dispute.viewDetailedCollapsed"))).not.toBeInTheDocument();
  });

  it("URL q 参数优先启动新问题，不复用 sessionStorage 里的旧会话", async () => {
    sessionStorage.setItem("roundtable_session_id", "sid-stale");
    sessionStorage.setItem("roundtable_question", "旧问题");
    vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
    vi.mocked(api.roundtableStream).mockResolvedValueOnce(undefined as never);

    renderRoundtablePage("/roundtable?q=%E6%96%B0%E9%97%AE%E9%A2%98");

    expect(api.roundtableResume).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(api.roundtableCheck).toHaveBeenCalledWith("新问题");
    });
    await waitFor(() => {
      expect(api.roundtableStream).toHaveBeenCalledWith(
        { question: "新问题", session_id: undefined },
        expect.any(Object),
        expect.any(AbortSignal),
      );
    });
  });

  it("兼容旧链接时仍会读取 URL q 参数", async () => {
    vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
    vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
    vi.mocked(api.roundtableStream).mockResolvedValueOnce(undefined as never);

    renderRoundtablePage("/roundtable?q=%E5%85%BC%E5%AE%B9%E6%97%A7%E9%93%BE%E6%8E%A5");

    await waitFor(() => {
      expect(api.roundtableCheck).toHaveBeenCalledWith("兼容旧链接");
    });
  });

  it("roundtableCheck 返回 401 时不触发 force-logout 且圆桌继续启动", async () => {
    const forceLogoutSpy = vi.fn();
    window.addEventListener("synthora:force-logout", forceLogoutSpy);
    vi.spyOn(api, "isLoggedIn").mockReturnValue(true);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/roundtable/check")) {
        return { ok: false, status: 401 } as Response;
      }
      throw new Error(`unexpected fetch: ${String(input)}`);
    }));
    vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
    vi.mocked(api.roundtableStream).mockResolvedValueOnce(undefined as never);

    renderRoundtablePage("/roundtable?q=%E6%B5%8B%E8%AF%95%E9%97%AE%E9%A2%98");

    await waitFor(() => {
      expect(api.roundtableStream).toHaveBeenCalledWith(
        { question: "测试问题", session_id: undefined },
        expect.any(Object),
        expect.any(AbortSignal),
      );
    });
    expect(forceLogoutSpy).not.toHaveBeenCalled();
    window.removeEventListener("synthora:force-logout", forceLogoutSpy);
  });

  it("resume 返回 403 时会用已保存问题自动重新发起并显示提示", async () => {
    sessionStorage.setItem("roundtable_question", "火星计划值得继续投吗？");
    vi.mocked(api.roundtableResume).mockRejectedValueOnce({ detail: "forbidden" } as never);
    vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
    vi.mocked(api.roundtableStream).mockResolvedValueOnce(undefined as never);

    renderRoundtablePage("/roundtable?session_id=sid-expired");

    expect(await screen.findByText(translateZh("pages.roundtable.errors.sessionExpiredRestarted"))).toBeInTheDocument();
    await waitFor(() => {
      expect(api.roundtableCheck).toHaveBeenCalledWith("火星计划值得继续投吗？");
    });
    await waitFor(() => {
      expect(api.roundtableStream).toHaveBeenCalledWith(
        { question: "火星计划值得继续投吗？", session_id: undefined },
        expect.any(Object),
        expect.any(AbortSignal),
      );
    });
  });

  it("非交互模式：S1→S2→S4→done，且不出现 UserActionPanel", async () => {
    vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
    vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
    vi.mocked(api.roundtableStream).mockImplementationOnce(async (_request, handlers) => {
      handlers.onStarted?.({
        session_id: "sid-active",
        expert_count: 2,
        question: "火星计划值得继续投吗？",
      });
      handlers.onExpertDone?.({
        model_id: "claude_opus_thinking",
        label: "专家A",
        stance: "支持",
        confidence: 0.7,
        my_dimensions: ["长期壁垒"],
        claims: [],
        risk_warning: "",
        blind_spot_warning: "",
        challenge_to_others: "",
        raw_response: "",
        structured: true,
        success: true,
        error: "",
        latency_ms: 1200,
        done_count: 1,
        total_count: 2,
      });
      handlers.onDisputesMapped?.(disputeMap);
      handlers.onModeratorStarted?.();
      handlers.onComplete?.({
        session_id: "sid-active",
        question: "火星计划值得继续投吗？",
        rounds_completed: 1,
        experts: [],
        dispute_map: disputeMap,
        decision_packet: {
          conclusion_type: "recommendation",
          confidence_basis: "信息已经足够",
          final_summary: "当前信息已足够形成结论。",
          stance_evolution: [],
          options: [],
          unresolved: [],
          what_changes_my_mind: "",
          recommended_action: RECOMMENDED_ACTION,
          value_disputes_to_user: [],
          echo_chamber_flag: false,
          degraded: false,
          degradation_reason: "",
          total_latency_ms: 1200,
          estimated_cost_usd: 0.12,
        },
      });
    });

    renderRoundtablePage("/roundtable?q=%E7%81%AB%E6%98%9F%E8%AE%A1%E5%88%92%E5%80%BC%E5%BE%97%E7%BB%A7%E7%BB%AD%E6%8A%95%E5%90%97%EF%BC%9F");

    expect(await screen.findByText(RECOMMENDED_ACTION)).toBeInTheDocument();
    expect(screen.queryByText(translateZh("pages.roundtable.userActionPanel.primaryA.title"))).not.toBeInTheDocument();
    expect(screen.queryByText(translateZh("pages.roundtable.userActionPanel.secondaryA.title"))).not.toBeInTheDocument();
    expect(screen.queryByText(translateZh("pages.roundtable.userActionPanel.tertiaryTitleA"))).not.toBeInTheDocument();
    expect(screen.queryByText(translateZh("pages.roundtable.spotlight.dependsOnYou"))).not.toBeInTheDocument();
    expect(api.roundtableChoice).not.toHaveBeenCalled();
  });

  describeInteractive("interactive choice flows", () => {
    it("决策点B允许追加第二轮深入辩论", async () => {
      vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
      vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
      vi.mocked(api.roundtableStream).mockImplementationOnce(async (_request, handlers) => {
        handlers.onStarted?.({
          session_id: "sid-active",
          expert_count: 2,
          question: "火星计划值得继续投吗？",
        });
        handlers.onDisputesMapped?.(disputeMap);
        handlers.onAwaitingUserChoice?.({
          choice_point: "B",
          timeout_s: 60,
          default_action: "conclude",
        });
      });
      vi.mocked(api.roundtableChoice).mockResolvedValueOnce({ ok: true } as never);

      renderRoundtablePage("/roundtable?q=%E7%81%AB%E6%98%9F%E8%AE%A1%E5%88%92%E5%80%BC%E5%BE%97%E7%BB%A7%E7%BB%AD%E6%8A%95%E5%90%97%EF%BC%9F");

      await waitFor(() => {
        expect(api.roundtableStream).toHaveBeenCalled();
      });
      fireEvent.click(await screen.findByRole("button", { name: new RegExp(translateZh("pages.roundtable.userActionPanel.secondaryB.title"), "i") }));

      await waitFor(() => {
        expect(api.roundtableChoice).toHaveBeenCalledWith(
          "sid-active",
          "B",
          "deepen",
          undefined,
          expect.any(String),
        );
      });
    });

    it("等待选择阶段流中断时会自动 resume 并重建 SSE", async () => {
      vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
      vi.mocked(api.roundtableResume).mockResolvedValueOnce({
        status: "session_active",
        session_id: "sid-active",
        state: "awaiting_B",
        choice_point: "B",
        state_snapshot: {
          question: "火星计划值得继续投吗？",
          expert_count: 2,
          experts: [],
          dispute_map: disputeMap,
          rebuttals: [],
          debate_round: 1,
          choice_point: "B",
        },
      } as never);
      vi.mocked(api.roundtableStream)
        .mockImplementationOnce(async (_request, handlers) => {
          handlers.onStarted?.({
            session_id: "sid-active",
            expert_count: 2,
            question: "火星计划值得继续投吗？",
          });
          handlers.onDisputesMapped?.(disputeMap);
          handlers.onAwaitingUserChoice?.({
            choice_point: "B",
            timeout_s: 60,
            default_action: "conclude",
          });
          handlers.onError?.("圆桌连接中断，请重试");
        })
        .mockResolvedValueOnce(undefined as never);

      renderRoundtablePage("/roundtable?q=%E7%81%AB%E6%98%9F%E8%AE%A1%E5%88%92%E5%80%BC%E5%BE%97%E7%BB%A7%E7%BB%AD%E6%8A%95%E5%90%97%EF%BC%9F");

      await waitFor(() => {
        expect(api.roundtableResume).toHaveBeenCalledWith("sid-active");
      });
      await waitFor(() => {
        expect(api.roundtableStream).toHaveBeenNthCalledWith(
          2,
          {
            question: "火星计划值得继续投吗？",
            session_id: "sid-active",
          },
          expect.any(Object),
          expect.any(AbortSignal),
        );
      });
      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.primaryB.title"))).toBeInTheDocument();
      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.secondaryB.title"))).toBeInTheDocument();
    });
  });

  it("证据层默认折叠，展开后不暴露 model_id 和 latency", async () => {
    vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
    vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
    vi.mocked(api.roundtableStream).mockImplementationOnce(async (_request, handlers) => {
      handlers.onStarted?.({
        session_id: "sid-active",
        expert_count: 1,
        question: "火星计划值得继续投吗？",
      });
      handlers.onExpertDone?.({
        model_id: "claude_opus_thinking",
        label: "专家A",
        stance: "支持",
        confidence: 0.7,
        my_dimensions: [LONG_TERM_MOAT, "投入节奏"],
        claims: [{ point: "长期价值高", evidence: "市场窗口", dimension: LONG_TERM_MOAT }],
        risk_warning: "现金流压力",
        blind_spot_warning: "可能低估执行难度",
        challenge_to_others: "短期风险不等于长期不值得",
        raw_response: "",
        structured: true,
        success: true,
        error: "",
        latency_ms: 1200,
        done_count: 1,
        total_count: 1,
      });
      handlers.onDisputesMapped?.(disputeMap);
      handlers.onAwaitingUserChoice?.({
        choice_point: "A",
        timeout_s: 60,
        default_action: "conclude",
      });
    });

    renderRoundtablePage("/roundtable?q=%E7%81%AB%E6%98%9F%E8%AE%A1%E5%88%92%E5%80%BC%E5%BE%97%E7%BB%A7%E7%BB%AD%E6%8A%95%E5%90%97%EF%BC%9F");

    expect(await screen.findByText(translateZh("pages.roundtable.spotlight.moderatorSuggestion"))).toBeInTheDocument();
    const opener = await screen.findByText(translateZh("pages.roundtable.dispute.viewDetailedCollapsed"));
    fireEvent.click(opener);
    expect(await screen.findByText(translateZh("pages.roundtable.dispute.viewDetailedExpanded"))).toBeInTheDocument();
    expect(await screen.findByText(LONG_TERM_MOAT)).toBeInTheDocument();
    expect(screen.queryByText("claude_opus_thinking")).not.toBeInTheDocument();
    expect(screen.queryByText("1.2s")).not.toBeInTheDocument();
  });

  describeInteractive("interactive inject flows", () => {
    it("A 点补充信息成功后保持当前 phase 且清空输入框", async () => {
      vi.mocked(api.roundtableResume).mockRejectedValueOnce(new Error("no session"));
      vi.mocked(api.roundtableCheck).mockResolvedValueOnce({ suitability: "high", reason: "" } as never);
      vi.mocked(api.roundtableStream).mockImplementationOnce(async (_request, handlers) => {
        handlers.onStarted?.({
          session_id: "sid-active",
          expert_count: 2,
          question: "火星计划值得继续投吗？",
        });
        handlers.onDisputesMapped?.(disputeMap);
        handlers.onAwaitingUserChoice?.({
          choice_point: "A",
          timeout_s: 60,
          default_action: "conclude",
        });
      });
      vi.mocked(api.roundtableChoice).mockResolvedValueOnce({ ok: true } as never);

      renderRoundtablePage("/roundtable?q=%E7%81%AB%E6%98%9F%E8%AE%A1%E5%88%92%E5%80%BC%E5%BE%97%E7%BB%A7%E7%BB%AD%E6%8A%95%E5%90%97%EF%BC%9F");

      expect(await screen.findByText(translateZh("pages.roundtable.spotlight.moderatorSuggestion"))).toBeInTheDocument();
      fireEvent.click(await screen.findByText(translateZh("pages.roundtable.userActionPanel.tertiaryTitleA")));

      const input = await screen.findByPlaceholderText(translateZh("pages.roundtable.userActionPanel.inputPlaceholder"));
      fireEvent.change(input, { target: { value: "请优先考虑现金流安全" } });
      fireEvent.click(await screen.findByText(translateZh("pages.roundtable.actions.send")));

      await waitFor(() => {
        expect(api.roundtableChoice).toHaveBeenCalledWith(
          "sid-active",
          "A",
          "inject",
          "请优先考虑现金流安全",
          expect.any(String),
        );
      });
      expect(await screen.findByText(translateZh("pages.roundtable.userActionPanel.promptA"))).toBeInTheDocument();
      await waitFor(() => {
        expect((input as HTMLInputElement).value).toBe("");
      });
    });
  });
});
