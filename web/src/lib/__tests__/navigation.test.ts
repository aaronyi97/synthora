import { describe, expect, it, vi } from "vitest";
import type { NavigateFunction } from "react-router-dom";
import { navigateWithFlushSync } from "@/lib/navigation";

describe("navigateWithFlushSync", () => {
  it("calls navigate with flushSync enabled", () => {
    const navigate = vi.fn();
    const legacyKey = ["unstable", "flushSync"].join("_");

    navigateWithFlushSync(navigate as unknown as NavigateFunction, "/history");

    expect(navigate).toHaveBeenCalledWith("/history", { flushSync: true });
    expect(navigate.mock.calls[0]?.[1]).not.toHaveProperty(legacyKey, true);
  });

  it("preserves existing options while forcing flushSync", () => {
    const navigate = vi.fn();
    const legacyKey = ["unstable", "flushSync"].join("_");

    navigateWithFlushSync(navigate as unknown as NavigateFunction, "/", { replace: true, state: { a: 1 } });

    expect(navigate).toHaveBeenCalledWith("/", {
      replace: true,
      state: { a: 1 },
      flushSync: true,
    });
    expect(navigate.mock.calls[0]?.[1]).not.toHaveProperty(legacyKey, true);
  });
});
