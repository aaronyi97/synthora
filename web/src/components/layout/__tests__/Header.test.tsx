import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Header from "@/components/layout/Header";
import { createZhI18nMock, translateZh } from "@/test/i18nMock";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    health: vi.fn(),
    getSavedUser: vi.fn(),
  },
}));

vi.mock("@/api/client", () => ({
  api: apiMock,
}));

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

describe("Header", () => {
  beforeEach(() => {
    apiMock.health.mockReset();
    apiMock.getSavedUser.mockReset();
    apiMock.getSavedUser.mockReturnValue(null);
  });

  it("在 conversation_store 降级时提示历史未保存", async () => {
    apiMock.health.mockResolvedValue({
      status: "degraded",
      conversation_store: "degraded",
    });

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    expect(await screen.findByText(translateZh("components.header.status.historyNotSaved"))).toBeInTheDocument();
  });
});
