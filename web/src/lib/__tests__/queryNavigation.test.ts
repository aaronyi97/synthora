import { describe, expect, it } from "vitest";
import { buildRoundtablePath, buildSocraticPath } from "@/lib/queryNavigation";

describe("buildSocraticPath", () => {
  it("为 socratic 入口返回纯路径", () => {
    expect(buildSocraticPath("为什么学习需要反思")).toBe("/socratic/new");
  });

  it("空问题时返回纯 socratic 路径", () => {
    expect(buildSocraticPath("   ")).toBe("/socratic/new");
  });
});

describe("buildRoundtablePath", () => {
  it("为 roundtable 入口返回纯路径", () => {
    expect(buildRoundtablePath("马斯克的火星计划到底靠不靠谱")).toBe("/roundtable");
  });

  it("空问题时返回纯 roundtable 路径", () => {
    expect(buildRoundtablePath("   ")).toBe("/roundtable");
  });
});
