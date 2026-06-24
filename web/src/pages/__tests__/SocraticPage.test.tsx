import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import { createZhI18nMock, translateZh } from "@/test/i18nMock";
import SocraticPage from "../SocraticPage";

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

vi.mock("@/api/client", () => ({
  api: {
    getCurrentLanguage: vi.fn(() => "zh-CN"),
    socraticRespond: vi.fn(),
    socraticReveal: vi.fn(),
    socraticStartStream: vi.fn(),
  },
}));

describe("SocraticPage", () => {
  it("优先读取 location.state.q 启动新问题", async () => {
    const starterQuestion = "Why does this happen?";

    vi.mocked(api.socraticStartStream).mockImplementation(async (_question, callbacks) => {
      callbacks.onStage?.("phase1", "thinking");
    });

    render(
      <MemoryRouter initialEntries={[{ pathname: "/socratic/new", state: { q: starterQuestion } }]}>
        <Routes>
          <Route path="/socratic/:sessionId" element={<SocraticPage />} />
        </Routes>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(api.socraticStartStream).toHaveBeenCalledWith(
        starterQuestion,
        expect.any(Object),
        expect.any(AbortSignal),
      );
    });
  });

  it("socraticRespond reject 时用户消息保留且错误可见", async () => {
    const followupQuestion = "Why does this happen?";

    vi.mocked(api.socraticRespond).mockRejectedValueOnce({ detail: translateZh("pages.socratic.errors.sendFailed") });

    render(
      <MemoryRouter initialEntries={["/socratic/test-session"]}>
        <Routes>
          <Route path="/socratic/:sessionId" element={<SocraticPage />} />
        </Routes>
      </MemoryRouter>
    );

    const textarea = await screen.findByPlaceholderText(translateZh("pages.socratic.inputPlaceholder"));
    fireEvent.change(textarea, { target: { value: followupQuestion } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter", isComposing: false });

    await waitFor(() => {
      expect(api.socraticRespond).toHaveBeenCalledWith("test-session", followupQuestion);
    });

    expect(await screen.findByText(followupQuestion)).toBeInTheDocument();
    expect(await screen.findByText(translateZh("pages.socratic.errors.sendFailed"))).toBeInTheDocument();
  });
});
