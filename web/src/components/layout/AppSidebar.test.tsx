import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { I18nextProvider } from "react-i18next";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AppSidebar from "./AppSidebar";
import i18n from "@/i18n";
import { api } from "@/api/client";
import { LANGUAGE_STORAGE_KEY, persistClientLanguage } from "@/i18n/language";
import { translateZh } from "@/test/i18nMock";

vi.mock("@/components/ui/CreditBar", () => ({
  default: () => <div>credit-bar</div>,
}));

vi.mock("@/api/client", () => ({
  api: {
    isLoggedIn: vi.fn(),
    setLanguage: vi.fn(),
  },
}));

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

describe("AppSidebar language switcher", () => {
  beforeEach(async () => {
    vi.resetAllMocks();
    installLocalStorageMock();
    persistClientLanguage("zh-CN");
    await i18n.changeLanguage("zh-CN");
  });

  it("切换语言时会更新 i18n、本地存储并同步后端 profile", async () => {
    vi.mocked(api.isLoggedIn).mockReturnValue(true);
    vi.mocked(api.setLanguage).mockResolvedValue({ language: "en-US" } as never);

    render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <AppSidebar
            conversations={[]}
            currentConvId={null}
            onNewConversation={vi.fn()}
            onSwitchConversation={vi.fn()}
            onDeleteConversation={vi.fn()}
            open={false}
            onClose={vi.fn()}
            onLogout={vi.fn()}
          />
        </MemoryRouter>
      </I18nextProvider>,
    );

    fireEvent.click(
      screen.getByRole(
        "button",
        { name: new RegExp(`${translateZh("common.sidebar.language")}|sidebar\\.language`, "i") },
      ),
    );

    await waitFor(() => expect(i18n.language).toBe("en-US"));
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en-US");
    expect(api.setLanguage).toHaveBeenCalledWith("en-US");
  });
});
