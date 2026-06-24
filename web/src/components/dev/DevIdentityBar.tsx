import { getRuntimeIdentity } from "@/lib/runtimeIdentity";

export default function DevIdentityBar() {
  if (!import.meta.env.DEV) return null;

  const identity = getRuntimeIdentity();

  return (
    <div className="pointer-events-none fixed inset-x-0 top-0 z-[90] flex justify-center px-3 pt-3">
      <div
        data-testid="dev-identity-bar"
        className="pointer-events-auto flex flex-wrap items-center justify-center gap-2 rounded-2xl border border-white/[0.14] bg-zinc-950/92 px-4 py-2 shadow-[0_18px_48px_rgba(0,0,0,0.35)] backdrop-blur-xl"
      >
        <span className="rounded-full border border-oracle-500/35 bg-oracle-500/[0.14] px-3 py-1 text-[13px] font-semibold text-oracle-200">
          Frontend {identity.frontendLabel}
        </span>
        <span className="rounded-full border border-sky-500/30 bg-sky-500/[0.12] px-3 py-1 text-[13px] font-semibold text-sky-200">
          Backend {identity.backendTargetLabel}
        </span>
      </div>
    </div>
  );
}
