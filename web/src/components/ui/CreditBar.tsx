import { useEffect, useState } from "react";
import { Zap } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/api/client";

interface QuotaData {
  enabled: boolean;
  total_credits: number;
  credits_used: number;
  credits_remaining: number;
  modes: Array<{ mode: string; used_lifetime: number; used_today: number; credit_cost: number; credits_spent: number }>;
}

export default function CreditBar({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  const [quota, setQuota] = useState<QuotaData | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    api.getQuota().then(setQuota).catch(() => {});
  }, []);

  if (!quota || !quota.enabled) return null;
  const modeLabels: Record<string, string> = {
    light: "Light",
    deep: "Deep",
    research: "Research",
    socratic: t("common.modes.socraticName"),
  };

  const pct = Math.max(0, Math.min(100, (quota.credits_remaining / quota.total_credits) * 100));
  const barColor =
    pct > 50 ? "bg-emerald-500" : pct > 20 ? "bg-amber-500" : "bg-red-500";
  const textColor =
    pct > 50 ? "text-emerald-400" : pct > 20 ? "text-amber-400" : "text-red-400";

  if (compact) {
    return (
      <div className="pb-1">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-center gap-1.5 group px-1 py-1"
        >
          <Zap size={10} className={textColor} />
          <span className={`text-[13px] font-medium ${textColor} shrink-0`}>
            {quota.credits_remaining}
          </span>
          <div className="flex-1 h-0.5 rounded-full bg-zinc-800 overflow-hidden">
            <div className={`h-full rounded-full transition-all duration-500 ${barColor}`} style={{ width: `${pct}%` }} />
          </div>
          <span className="text-[13px] text-zinc-400 shrink-0">{quota.credits_used}/{quota.total_credits}</span>
        </button>
        {expanded && (
          <div className="mt-1 grid grid-cols-2 gap-1 pb-1">
            {quota.modes.map((m) => (
              <div key={m.mode} className="rounded-lg bg-zinc-800/50 border border-zinc-700/30 px-2 py-1">
                <div className="text-[13px] text-zinc-400">{modeLabels[m.mode] || m.mode}</div>
                <div className="text-[13px] font-semibold text-zinc-300">{t("components.creditBar.usedLifetime", { count: m.used_lifetime })}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="shrink-0 border-b border-zinc-800/40 bg-zinc-900/60 px-2 sm:px-4 py-1">
      <div className="max-w-3xl mx-auto">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-center gap-1.5 sm:gap-2 group"
        >
          <Zap size={10} className={textColor} />
          <span className={`text-[13px] font-medium ${textColor} shrink-0`}>
            {quota.credits_remaining}
          </span>
          <div className="flex-1 h-0.5 sm:h-1 rounded-full bg-zinc-800 overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${barColor}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className="text-[13px] text-zinc-400 shrink-0">
            {quota.credits_used}/{quota.total_credits}
          </span>
        </button>

        {expanded && (
          <div className="mt-1.5 grid grid-cols-2 sm:grid-cols-4 gap-1.5 pb-1">
            {quota.modes.map((m) => (
              <div
                key={m.mode}
                className="rounded-lg bg-zinc-800/50 border border-zinc-700/30 px-2 py-1.5"
              >
                <div className="flex items-center justify-between mb-0.5">
                  <span className="text-[13px] text-zinc-300 font-medium">
                    {modeLabels[m.mode] || m.mode}
                  </span>
                  <span className="text-[13px] text-zinc-400">
                    {t("components.creditBar.creditCost", { count: m.credit_cost })}
                  </span>
                </div>
                <div className="flex items-baseline gap-1">
                  <span className="text-[13px] font-semibold text-zinc-200">{m.used_lifetime}</span>
                  <span className="text-[13px] text-zinc-400">{t("components.creditBar.unit")}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
