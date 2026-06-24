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

import ResearchTimeline from "../ResearchTimeline";

const stageHistory = [
  {
    stage: "search",
    label: "搜索资料...",
    detail: "2 个来源",
    startedAt: 0,
    completedAt: 1200,
  },
];

describe("ResearchTimeline", () => {
  it("taskStatus='error' 时显示'发生错误'而非'全部完成'", () => {
    render(
      <ResearchTimeline
        stageHistory={stageHistory}
        isStreaming={false}
        taskStatus="error"
      />
    );

    expect(screen.getByText(translateZh("components.researchTimeline.status.error"))).toBeInTheDocument();
    expect(screen.queryByText(translateZh("components.researchTimeline.status.done"))).not.toBeInTheDocument();
  });

  it("taskStatus='user_cancelled' 时显示'已取消'", () => {
    render(
      <ResearchTimeline
        stageHistory={stageHistory}
        isStreaming={false}
        taskStatus="user_cancelled"
      />
    );

    expect(screen.getByText(translateZh("components.researchTimeline.status.cancelled"))).toBeInTheDocument();
    expect(screen.queryByText(translateZh("components.researchTimeline.status.done"))).not.toBeInTheDocument();
  });
});
