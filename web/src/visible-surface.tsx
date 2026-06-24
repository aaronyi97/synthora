import React from "react";
import ReactDOM from "react-dom/client";
import DevIdentityBar from "@/components/dev/DevIdentityBar";
import ErrorBoundary from "@/components/ErrorBoundary";
import VisibleSurfacePage from "@/pages/VisibleSurfacePage";
import "./index.css";

const rootEl = document.getElementById("root");

if (!rootEl) {
  throw new Error("Missing #root element");
}

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <DevIdentityBar />
    <ErrorBoundary>
      <VisibleSurfacePage />
    </ErrorBoundary>
  </React.StrictMode>,
);
