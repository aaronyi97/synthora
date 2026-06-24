import type { ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { api } from "./api/client";

vi.mock("./api/client", () => ({
  api: {
    isLoggedIn: vi.fn(),
    me: vi.fn(),
    logout: vi.fn(),
  },
}));

vi.mock("./components/ErrorBoundary", () => ({
  default: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("./components/ui/AuthPage", () => ({
  default: () => <div>auth-page</div>,
}));

vi.mock("./components/ui/LoadingOracle", () => ({
  default: () => <div>loading</div>,
}));

vi.mock("./components/ui/ConsentBanner", () => ({
  default: () => <div>consent-banner</div>,
  useConsentBanner: () => ({ show: false, accept: vi.fn() }),
}));

vi.mock("./components/layout/Layout", () => ({
  default: () => <div>layout</div>,
}));

vi.mock("./pages/HomePage", () => ({ default: () => <div>home-page</div> }));
vi.mock("./pages/SocraticPage", () => ({ default: () => <div>socratic-page</div> }));
vi.mock("./pages/HistoryPage", () => ({ default: () => <div>history-page</div> }));
vi.mock("./pages/ProfilePage", () => ({ default: () => <div>profile-page</div> }));
vi.mock("./pages/CapabilityMapPage", () => ({ default: () => <div>capability-map-page</div> }));
vi.mock("./pages/PrivacyPage", () => ({ default: () => <div>privacy-page</div> }));
vi.mock("./pages/RoundtablePage", () => ({ default: () => <div>roundtable-page</div> }));

function installLocalStorageMock() {
  const store = new Map<string, string>();
  const storage = {
    getItem: vi.fn((key: string) => store.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, String(value));
    }),
    removeItem: vi.fn((key: string) => {
      store.delete(key);
    }),
    clear: vi.fn(() => {
      store.clear();
    }),
  };

  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: storage,
  });
}

describe("App auth bootstrap", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.history.replaceState({}, "", "/");
    installLocalStorageMock();
    window.localStorage.removeItem("synthora_user");
    vi.mocked(api.isLoggedIn).mockReturnValue(true);
    vi.mocked(api.logout).mockResolvedValue(undefined);
  });

  it("401 me() 失败时保留本地用户并停在登录页", async () => {
    localStorage.setItem(
      "synthora_user",
      JSON.stringify({ username: "alice", display_name: "Alice", is_admin: false }),
    );
    vi.mocked(api.me).mockRejectedValueOnce({ status: 401, detail: "Unauthorized" } as never);

    render(<App />);

    expect(await screen.findByText("auth-page")).toBeInTheDocument();
    await waitFor(() => expect(api.me).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem("synthora_user")).not.toBeNull();
  });

  it("非 401 auth 异常时清理本地用户", async () => {
    localStorage.setItem(
      "synthora_user",
      JSON.stringify({ username: "alice", display_name: "Alice", is_admin: false }),
    );
    vi.mocked(api.me).mockRejectedValueOnce({ status: 403, detail: "Forbidden" } as never);

    render(<App />);

    expect(await screen.findByText("auth-page")).toBeInTheDocument();
    await waitFor(() => expect(api.me).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem("synthora_user")).toBeNull();
  });

  it("未登录时可以直接访问 /privacy", async () => {
    vi.mocked(api.isLoggedIn).mockReturnValue(false);
    window.history.replaceState({}, "", "/privacy");
    vi.resetModules();
    const { default: AppWithCurrentLocation } = await import("./App");

    render(<AppWithCurrentLocation />);

    expect(await screen.findByText("privacy-page")).toBeInTheDocument();
    expect(api.me).not.toHaveBeenCalled();
  });

  it("me() 成功时会把 preferred_language 写入新的语言存储 key", async () => {
    vi.mocked(api.me).mockResolvedValueOnce({
      username: "alice",
      display_name: "Alice",
      is_admin: false,
      query_count: 1,
      preferred_language: "en-US",
    } as never);

    render(<App />);

    expect(await screen.findByText("layout")).toBeInTheDocument();
    await waitFor(() => expect(localStorage.getItem("synthora_language")).toBe("en-US"));
  });
});
