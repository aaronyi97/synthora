import * as Sentry from "@sentry/react";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import DevIdentityBar from "./components/dev/DevIdentityBar";
import i18n from "./i18n";
import "./index.css";

const sentryEnvironment = (import.meta.env.VITE_SENTRY_ENVIRONMENT || import.meta.env.MODE || "development").trim() || "development";
const sentryRelease = (import.meta.env.VITE_SENTRY_RELEASE || import.meta.env.VITE_APP_VERSION || __APP_VERSION__).trim() || "0.1.0";

Sentry.init({
  dsn: import.meta.env.VITE_SENTRY_DSN,
  environment: sentryEnvironment,
  release: sentryRelease,
  beforeSend(event) {
    if (event.request?.url) {
      event.request.url = event.request.url.split("?")[0];
    }
    return event;
  },
});

// DEBUG: Render startup errors directly into the page to avoid silent white screens.
try {
  const rootEl = document.getElementById("root");
  if (!rootEl) {
    throw new Error("Missing #root element");
  }
  ReactDOM.createRoot(rootEl).render(
    <>
      <DevIdentityBar />
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </>,
  );
} catch (e: unknown) {
  const msg = e instanceof Error ? e.message + "\n" + e.stack : String(e);
  const rootEl = document.getElementById("root");
  if (rootEl) {
    rootEl.replaceChildren();
    const pre = document.createElement("pre");
    pre.style.color = "red";
    pre.style.padding = "20px";
    pre.style.fontSize = "14px";
    pre.style.whiteSpace = "pre-wrap";
    pre.textContent = `${i18n.t("common.runtime.startupError")}\n${msg}`;
    rootEl.appendChild(pre);
  }
}
