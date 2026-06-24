import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createZhI18nMock, translateZh } from "@/test/i18nMock";

vi.mock("@/api/client", () => ({
  api: {
    modes: vi.fn(() => new Promise(() => {})),
    submitFeedback: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn(),
  },
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

import VisibleSurfacePage from "@/pages/VisibleSurfacePage";

describe("VisibleSurfacePage", () => {
  it("renders all required offline review states", () => {
    render(<VisibleSurfacePage />);

    expect(screen.getByText(translateZh("pages.visibleSurface.title"))).toBeInTheDocument();
    expect(screen.getByText(translateZh("pages.visibleSurface.cards.light.title"))).toBeInTheDocument();
    expect(screen.getByText(translateZh("pages.visibleSurface.cards.deep.title"))).toBeInTheDocument();
    expect(screen.getByText(translateZh("pages.visibleSurface.cards.dispatcher.title"))).toBeInTheDocument();
    expect(screen.getByText(translateZh("pages.visibleSurface.subtitle"))).toBeInTheDocument();
  });
});
