import * as Sentry from "@sentry/react";
import { Component, type ReactNode } from "react";
import i18n from "@/i18n";

interface Props { children: ReactNode; }
interface State { error: Error | null; }

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    Sentry.captureException(error, {
      contexts: { react: { componentStack: info.componentStack } },
    });
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      const isDev = import.meta.env.DEV;
      return (
        <div className="flex flex-col items-center justify-center min-h-screen bg-surface-0 gap-4 px-6">
          <div className="w-12 h-12 rounded-full bg-red-500/10 flex items-center justify-center">
            <span className="text-2xl">💥</span>
          </div>
          <div className="text-center">
            <div className="text-base font-semibold text-zinc-200 mb-1">{i18n.t("components.errorBoundary.title")}</div>
            <div className="text-sm text-zinc-500">{i18n.t("components.errorBoundary.subtitle")}</div>
          </div>
          {isDev && (
            <div className="w-full max-w-2xl glass rounded-xl p-4 text-left">
              <pre className="text-xs text-amber-400 whitespace-pre-wrap break-all">{this.state.error.message}</pre>
              <pre className="text-xs text-zinc-500 whitespace-pre-wrap break-all mt-2">{this.state.error.stack}</pre>
            </div>
          )}
          <button
            onClick={() => { this.setState({ error: null }); window.location.reload(); }}
            className="px-4 py-2 rounded-lg bg-oracle-500/15 text-oracle-300 border border-oracle-500/30 text-sm hover:bg-oracle-500/25 transition-colors"
          >
            {i18n.t("components.errorBoundary.reload")}
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
