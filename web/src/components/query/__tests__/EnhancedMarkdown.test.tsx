import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import EnhancedMarkdown from "../EnhancedMarkdown";

describe("EnhancedMarkdown", () => {
  it("sanitizes dangerous markdown href protocols", () => {
    render(<EnhancedMarkdown content="[bad](javascript:alert(1))" />);

    const link = screen.getByRole("link", { name: "bad" });
    expect(link.getAttribute("href")).toBe("#");
  });

  it("sanitizes dangerous citation href protocols", () => {
    render(
      <EnhancedMarkdown
        content={"引用来源 [1]"}
        citations={[{ url: "javascript:alert(1)", title: "bad-source" }]}
      />,
    );

    const links = screen.getAllByRole("link");
    expect(links.every((link) => link.getAttribute("href") === "#")).toBe(true);
  });
});
