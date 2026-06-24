import fs from "node:fs/promises";
import path from "node:path";
import type { Browser, BrowserContext, Page } from "@playwright/test";
import { expect } from "@playwright/test";

const DEFAULT_BASE_URL = "http://localhost:4173";
const DEFAULT_API_BASE = "https://api.example.com/api";
const DEFAULT_USERNAME = "trial_user1";
const DEFAULT_PASSWORD = "Synthora2026#1";

export const PLAYWRIGHT_BASE_URL = process.env.PLAYWRIGHT_BASE_URL || DEFAULT_BASE_URL;
export const PLAYWRIGHT_API_BASE = process.env.PLAYWRIGHT_API_BASE || DEFAULT_API_BASE;
export const E2E_USERNAME = process.env.E2E_USERNAME || DEFAULT_USERNAME;
export const E2E_PASSWORD = process.env.E2E_PASSWORD || DEFAULT_PASSWORD;
export const AUTH_STATE_PATH = path.resolve(process.cwd(), "playwright/.cache/auth-state.json");

const MODE_LABELS = /^(Auto|Deep|Research|苏格拉底)$/;

export async function clearAuthState(): Promise<void> {
  await fs.rm(AUTH_STATE_PATH, { force: true });
}

export async function persistAuthState(context: BrowserContext): Promise<void> {
  await fs.mkdir(path.dirname(AUTH_STATE_PATH), { recursive: true });
  await context.storageState({ path: AUTH_STATE_PATH });
}

export async function createStoredSession(browser: Browser, baseURL: string): Promise<{
  context: BrowserContext;
  page: Page;
}> {
  const hasStoredAuth = await fs.access(AUTH_STATE_PATH).then(() => true).catch(() => false);
  const context = await browser.newContext(
    hasStoredAuth
      ? {
          baseURL,
          storageState: AUTH_STATE_PATH,
        }
      : {
          baseURL,
        }
  );
  const page = await context.newPage();
  await page.goto("/");
  if (!(await isLoginPageVisible(page))) {
    await expectHomeReady(page);
  }
  return { context, page };
}

export async function isLoginPageVisible(page: Page): Promise<boolean> {
  return page.getByLabel("手机号 / 用户名").isVisible().catch(() => false);
}

export async function loginFromAuthPage(page: Page): Promise<void> {
  await page.getByLabel("手机号 / 用户名").fill(E2E_USERNAME);
  await page.getByLabel("密码").fill(E2E_PASSWORD);
  await page.locator("form").getByRole("button", { name: /^登录$/ }).click();
}

export async function dismissConsentBanner(page: Page): Promise<void> {
  const consentButton = page.getByRole("button", { name: "知道了" });
  if (await consentButton.isVisible().catch(() => false)) {
    await consentButton.click();
  }
}

export async function expectHomeReady(page: Page): Promise<void> {
  await expect(page.locator("textarea[data-query-input]")).toBeVisible({ timeout: 45_000 });
  await dismissConsentBanner(page);
  await expect(page.locator("textarea[data-query-input]")).toBeVisible();
}

export function modeTrigger(page: Page) {
  return page.getByRole("button", { name: MODE_LABELS }).first();
}
