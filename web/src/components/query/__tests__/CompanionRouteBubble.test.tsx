import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CompanionRouteBubble from "../CompanionRouteBubble";

describe("CompanionRouteBubble", () => {
  it("有 actions + onAction 时，action 按钮可见且点击触发 onAction", () => {
    const onAction = vi.fn();
    const route = {
      message: "The current route is slower, and you can switch directly.",
      actions: [
        {
          label: "Switch to Light",
          action_type: "query_light",
          action_payload: { mode: "light" },
        },
      ],
      route_reason: "The wait is relatively long.",
      auto_execute_seconds: 15,
      is_silent: false,
      resolved_mode: "research",
      contributor_count: 3,
    };

    render(<CompanionRouteBubble route={route} onAction={onAction} />);

    const button = screen.getByRole("button", { name: "Switch to Light" });
    expect(button).toBeVisible();

    fireEvent.click(button);

    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction).toHaveBeenCalledWith(route.actions[0]);
  });
});
