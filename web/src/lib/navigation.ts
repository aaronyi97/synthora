import type { NavigateFunction, NavigateOptions, To } from "react-router-dom";

export function navigateWithFlushSync(
  navigate: NavigateFunction,
  to: To,
  options: NavigateOptions = {},
) {
  navigate(to, { ...options, flushSync: true });
}
