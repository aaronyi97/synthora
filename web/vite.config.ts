import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";
import { execSync } from "node:child_process";

const apiTarget = process.env.VITE_API_TARGET || "http://localhost:8000";
const frontendVersion = process.env.npm_package_version || "0.1.0";

function getFrontendCommit(): string {
  try {
    return execSync("git rev-parse --short HEAD", {
      cwd: __dirname,
      stdio: ["ignore", "pipe", "ignore"],
    }).toString().trim();
  } catch {
    return "dev";
  }
}

const frontendCommit = getFrontendCommit();

function shouldRewriteProxyOrigin(target: string): boolean {
  try {
    const u = new URL(target);
    const host = u.hostname.toLowerCase();
    const isLoopback =
      host === "localhost" ||
      host === "::1" ||
      host === "0.0.0.0" ||
      host.startsWith("127.");
    return !isLoopback;
  } catch {
    return false;
  }
}

const proxyConfig = {
  "/api": {
    target: apiTarget,
    changeOrigin: true,
    configure: (proxy: any, options: { target?: string }) => {
      const target = String(options.target || apiTarget);
      if (!shouldRewriteProxyOrigin(target)) return;
      const targetOrigin = new URL(target).origin;
      proxy.on("proxyReq", (proxyReq: any, req: { headers: { origin?: string; referer?: string } }) => {
        // Production API CSRF checks Origin/Referer. Rewrite only for remote targets.
        if (req.headers.origin) proxyReq.setHeader("origin", targetOrigin);
        if (req.headers.referer) proxyReq.setHeader("referer", `${targetOrigin}/`);
      });
    },
    // Default: proxy to local backend. To use remote: VITE_API_TARGET=https://api.example.com npm run dev
  },
};

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(frontendVersion),
    __APP_COMMIT__: JSON.stringify(frontendCommit),
    __API_TARGET__: JSON.stringify(apiTarget),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    strictPort: true,  // fail fast instead of silently drifting to 5174/5175
    allowedHosts: true,
    proxy: proxyConfig,
  },
  preview: {
    port: 4173,
    strictPort: true,
    proxy: proxyConfig,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
