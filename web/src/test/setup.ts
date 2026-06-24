import "@testing-library/jest-dom";
import "@/i18n";
import { beforeEach, vi } from "vitest";

Object.defineProperty(window.HTMLElement.prototype, "scrollIntoView", {
  configurable: true,
  value: vi.fn(),
});

beforeEach(() => {
  window.localStorage?.clear?.();
  window.sessionStorage?.clear?.();
  vi.clearAllMocks();
});
