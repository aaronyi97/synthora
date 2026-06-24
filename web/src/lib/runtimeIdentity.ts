function normalizeHostname(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "/api") return "localhost";
  try {
    const url = trimmed.includes("://") ? new URL(trimmed) : new URL(`https://${trimmed}`);
    const host = url.hostname.toLowerCase();
    if (host === "localhost" || host === "::1" || host === "0.0.0.0" || host.startsWith("127.")) {
      return "localhost";
    }
    return host;
  } catch {
    return trimmed.replace(/^https?:\/\//, "").replace(/\/.*$/, "") || "localhost";
  }
}

export function getRuntimeIdentity() {
  const version = typeof __APP_VERSION__ !== "undefined" ? __APP_VERSION__ : "0.1.0";
  const commit = typeof __APP_COMMIT__ !== "undefined" ? __APP_COMMIT__ : "dev";
  const backendTargetRaw = typeof __API_TARGET__ !== "undefined" ? __API_TARGET__ : "http://localhost:8000";
  const backendTargetLabel = normalizeHostname(backendTargetRaw);

  return {
    frontendVersion: version,
    frontendCommit: commit,
    frontendLabel: `${version}@${commit}`,
    backendTargetRaw,
    backendTargetLabel,
  };
}

export { normalizeHostname as normalizeBackendTarget };
