import type { BrowserContext, Page } from "@playwright/test";
import { expect, test } from "@playwright/test";
import {
  PLAYWRIGHT_API_BASE,
  PLAYWRIGHT_BASE_URL,
  clearAuthState,
  createStoredSession,
  expectHomeReady,
  isLoginPageVisible,
  loginFromAuthPage,
  modeTrigger,
  persistAuthState,
} from "./auth.setup";

type StoredConversation = {
  updatedAt?: number;
  messages?: Array<{
    role?: string;
    response?: {
      final_answer?: string;
    };
  }>;
};

test.describe.serial("frontend smoke", () => {
  let context: BrowserContext;
  let page: Page;
  let resolvedBaseURL = PLAYWRIGHT_BASE_URL;

  test.beforeAll(async ({ browser, baseURL }) => {
    resolvedBaseURL = baseURL || PLAYWRIGHT_BASE_URL;
    await clearAuthState();
    context = await browser.newContext({ baseURL: resolvedBaseURL });
    page = await context.newPage();
  });

  test.afterAll(async () => {
    await context?.close();
  });

  test("后端健康检查", async () => {
    const response = await fetch(`${PLAYWRIGHT_API_BASE}/health`);
    expect(response.ok).toBeTruthy();
  });

  test("登录流程完成", async () => {
    await page.goto("/");

    if (await isLoginPageVisible(page)) {
      await loginFromAuthPage(page);
    }

    await expectHomeReady(page);
    await expect(page.getByLabel("手机号 / 用户名")).toHaveCount(0);
    await persistAuthState(context);
  });

  test("首页加载 + 模式选择器可见", async ({ browser }) => {
    await context.close();
    ({ context, page } = await createStoredSession(browser, resolvedBaseURL));

    if (await isLoginPageVisible(page)) {
      await loginFromAuthPage(page);
      await expectHomeReady(page);
      await persistAuthState(context);
    }

    const queryInput = page.locator("textarea[data-query-input]");
    const selectorTrigger = modeTrigger(page);

    await expect(selectorTrigger).toBeVisible();
    await selectorTrigger.click();

    const modeButtons = page
      .locator("button")
      .filter({ hasText: /Auto|Deep|Research|苏格拉底/ });
    await expect(modeButtons.first()).toBeVisible();
    expect(await modeButtons.count()).toBeGreaterThanOrEqual(2);

    await queryInput.click();
    await expect(queryInput).toBeVisible();
  });

  test("提问并收到回答", async () => {
    const queryInput = page.locator("textarea[data-query-input]");

    await queryInput.fill("什么是量子计算");
    await queryInput.press("Enter");

    const answerText = await page.waitForFunction(() => {
      const normalize = (value: string | null | undefined) => value?.replace(/\s+/g, " ").trim() || "";

      const versionButtons = Array.from(document.querySelectorAll("button"))
        .filter((button) => /版本 \d+｜/.test(normalize(button.textContent)));
      const latestVersion = versionButtons.at(-1);
      const streamedContainer = latestVersion?.parentElement;
      const streamedText = normalize(streamedContainer?.textContent);
      if (streamedText.length > 20) {
        return streamedText;
      }

      const raw = window.localStorage.getItem("synthora_conversations");
      if (!raw) return "";

      try {
        const conversations = JSON.parse(raw) as StoredConversation[];
        const latest = [...conversations]
          .sort((left, right) => (right.updatedAt || 0) - (left.updatedAt || 0))[0];
        const assistant = latest?.messages
          ?.slice()
          .reverse()
          .find((message) => message.role === "assistant" && message.response?.final_answer?.trim());
        const finalAnswer = assistant?.response?.final_answer?.trim() || "";
        return finalAnswer.length > 20 ? finalAnswer : "";
      } catch {
        return "";
      }
    }, undefined, { timeout: 45_000 });

    expect((await answerText.jsonValue<string>()).length).toBeGreaterThan(20);
  });

  test("历史记录可查看", async ({ browser }) => {
    await context.close();
    ({ context, page } = await createStoredSession(browser, resolvedBaseURL));

    if (await isLoginPageVisible(page)) {
      await loginFromAuthPage(page);
      await expectHomeReady(page);
      await persistAuthState(context);
    }

    const historyResponsePromise = page.waitForResponse((response) => (
      response.url().includes("/api/history")
      && response.request().method() === "GET"
      && response.ok()
    ));

    await page.goto("/history");
    await expect(page.getByPlaceholder("搜索历史...")).toBeVisible({ timeout: 15_000 });

    await historyResponsePromise;

    const historyEntry = page.locator("button").filter({ hasText: "什么是量子计算" }).first();
    await expect(historyEntry).toBeVisible();
  });
});
