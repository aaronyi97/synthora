import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./client";

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

describe("ApiClient auth", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    installLocalStorageMock();
  });

  it("login 之后后续请求依赖 HttpOnly cookie，不再发送 Bearer header", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "ok",
            username: "alice",
            display_name: "Alice",
            is_admin: false,
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            username: "alice",
            display_name: "Alice",
            is_admin: false,
            query_count: 1,
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await client.login("alice", "password123");
    await client.me();

    const loginOptions = fetchMock.mock.calls[0][1];
    const headers = new Headers(fetchMock.mock.calls[1][1]?.headers as HeadersInit | undefined);
    expect(loginOptions?.credentials).toBe("include");
    expect(fetchMock.mock.calls[1][1]?.credentials).toBe("include");
    expect(headers.has("Authorization")).toBe(false);
  });
});
