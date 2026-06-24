import { defineConfig } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:4173";
const apiBase = process.env.PLAYWRIGHT_API_BASE || "https://api.example.com/api";
const apiTarget = (() => {
  try {
    const url = new URL(apiBase);
    const pathname = url.pathname.replace(/\/api\/?$/, "");
    return `${url.origin}${pathname}`;
  } catch {
    return apiBase.replace(/\/api\/?$/, "");
  }
})();

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  reporter: [["html", { open: "never" }]],
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
      },
    },
  ],
  webServer: {
    command: "npm run build && npm run preview",
    port: 4173,
    reuseExistingServer: true,
    timeout: 180_000,
    env: {
      ...process.env,
      VITE_API_BASE: "/api",
      VITE_API_TARGET: apiTarget,
    },
  },
});
