import { describe, expect, it } from "vitest";
import { getRuntimeIdentity, normalizeBackendTarget } from "@/lib/runtimeIdentity";

describe("runtimeIdentity", () => {
  it("normalizes localhost targets", () => {
    expect(normalizeBackendTarget("http://localhost:8000")).toBe("localhost");
    expect(normalizeBackendTarget("http://127.0.0.1:9000")).toBe("localhost");
    expect(normalizeBackendTarget("/api")).toBe("localhost");
  });

  it("preserves remote backend hostnames", () => {
    expect(normalizeBackendTarget("https://api.example.com")).toBe("api.example.com");
  });

  it("exposes frontend and backend identity fields", () => {
    const identity = getRuntimeIdentity();
    expect(identity.frontendVersion).toBeTruthy();
    expect(identity.frontendCommit).toBeTruthy();
    expect(identity.frontendLabel).toContain("@");
    expect(identity.backendTargetLabel).toBeTruthy();
  });
});
