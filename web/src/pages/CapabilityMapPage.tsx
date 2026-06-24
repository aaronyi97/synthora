import { useState, useEffect, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  Map,
  Target,
  TrendingUp,
  Loader2,
  CheckCircle2,
  Play,
  X,
  Sparkles,
  BarChart3,
  Zap,
} from "lucide-react";
import { api } from "@/api/client";
import type { CapabilityMapResponse, ImprovementPlanSummary, ApiError } from "@/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

const LEVEL_COLORS: Record<number, string> = {
  1: "bg-zinc-600",
  2: "bg-sky-500",
  3: "bg-oracle-500",
  4: "bg-emerald-500",
  5: "bg-amber-400",
};

function buildLevelLabels(t: Translate): Record<number, string> {
  return {
    1: t("pages.capabilityMap.levelLabels.l1"),
    2: t("pages.capabilityMap.levelLabels.l2"),
    3: t("pages.capabilityMap.levelLabels.l3"),
    4: t("pages.capabilityMap.levelLabels.l4"),
    5: t("pages.capabilityMap.levelLabels.l5"),
  };
}

function buildQuadrantMeta(t: Translate): Record<string, { label: string; color: string }> {
  return {
    known_known: { label: t("common.cognitiveQuadrants.knownKnown.label"), color: "bg-emerald-500" },
    known_unknown: { label: t("common.cognitiveQuadrants.knownUnknown.label"), color: "bg-oracle-500" },
    unknown_known: { label: t("common.cognitiveQuadrants.unknownKnown.label"), color: "bg-sky-500" },
    unknown_unknown: { label: t("common.cognitiveQuadrants.unknownUnknown.label"), color: "bg-amber-500" },
  };
}

export default function CapabilityMapPage() {
  const { t } = useTranslation();
  const levelLabels = buildLevelLabels(t);
  const quadrantMeta = buildQuadrantMeta(t);
  const [data, setData] = useState<CapabilityMapResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.capabilityMap();
      setData(res);
      setError(null);
    } catch (e) {
      setError((e as ApiError)?.detail || t("pages.capabilityMap.errors.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleActivate = useCallback(async (planId: string) => {
    setActionLoading(planId);
    try {
      await api.activatePlan(planId);
      const fresh = await api.capabilityMap();
      setData(fresh);
    } catch (e) {
      setError((e as ApiError)?.detail || t("pages.capabilityMap.errors.activateFailed"));
    } finally {
      setActionLoading(null);
    }
  }, [t]);

  const handleAbandon = useCallback(async (planId: string) => {
    setActionLoading(planId);
    try {
      await api.abandonPlan(planId);
      const fresh = await api.capabilityMap();
      setData(fresh);
    } catch (e) {
      setError((e as ApiError)?.detail || t("pages.capabilityMap.errors.abandonFailed"));
    } finally {
      setActionLoading(null);
    }
  }, [t]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[60vh]">
        <Loader2 size={24} className="animate-spin text-oracle-500" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-8">
        <div className="p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
          {error}
        </div>
        <button
          type="button"
          onClick={() => {
            setError(null);
            setLoading(true);
            void load();
          }}
          className="mt-3 rounded-lg border border-red-500/25 bg-red-500/10 px-3 py-1.5 text-xs text-red-200 transition-colors hover:bg-red-500/20"
        >
          {t("common.actions.retry")}
        </button>
      </div>
    );
  }

  if (!data?.has_data) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-8">
        <h1 className="text-2xl font-display text-zinc-100 flex items-center gap-2.5 mb-6">
          <Map size={22} className="text-oracle-400" />
          {t("pages.capabilityMap.title")}
        </h1>
        <div className="glass rounded-2xl p-8 text-center">
          <div className="w-16 h-16 mx-auto rounded-full bg-oracle-500/10 flex items-center justify-center mb-4">
            <Map size={28} className="text-oracle-500/50" />
          </div>
          <p className="text-zinc-400 text-sm mb-1">{t("pages.capabilityMap.empty.title")}</p>
          <p className="text-xs text-zinc-600 max-w-sm mx-auto">
            {t("pages.capabilityMap.empty.subtitle")}
          </p>
        </div>
      </div>
    );
  }

  const totalQ = Object.values(data.cognitive_quadrant).reduce(
    (s, v) => s + v.count,
    0,
  ) || 1;

  return (
    <div className="h-full overflow-y-auto max-w-3xl mx-auto px-4 sm:px-6 py-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-display text-zinc-100 flex items-center gap-2.5">
          <Map size={22} className="text-oracle-400" />
          {t("pages.capabilityMap.title")}
        </h1>
        <p className="text-sm text-zinc-600 mt-1">
          {t("pages.capabilityMap.subtitle")}
        </p>
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-8">
        <StatCard
          icon={<Target size={14} className="text-oracle-400" />}
          value={data.total_topics_explored}
          label={t("pages.capabilityMap.stats.exploredTopics")}
        />
        <StatCard
          icon={<TrendingUp size={14} className="text-emerald-400" />}
          value={data.topics_at_l3_plus}
          label={t("pages.capabilityMap.stats.deepTopics")}
        />
        <StatCard
          icon={<CheckCircle2 size={14} className="text-sky-400" />}
          value={data.completed_plans_count}
          label={t("pages.capabilityMap.stats.completedPlans")}
        />
        <StatCard
          icon={<Sparkles size={14} className="text-amber-400" />}
          value={`${(data.average_reasoning_quality * 100).toFixed(0)}%`}
          label={t("pages.capabilityMap.stats.reasoningQuality")}
        />
      </div>

      {/* Topic depth chart */}
      <section className="mb-8">
        <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
          <BarChart3 size={16} className="text-oracle-400" />
          {t("pages.capabilityMap.sections.topicDepth")}
        </h2>
        <div className="glass rounded-2xl p-5 space-y-3">
          {data.topics.map((topic) => (
            <div key={topic.topic}>
              <div className="flex items-center justify-between text-xs mb-1">
                <span className="text-zinc-300 truncate max-w-[60%]">
                  {topic.topic}
                </span>
                <div className="flex items-center gap-2">
                  <span className="text-zinc-500">{t("pages.capabilityMap.topic.frequency", { count: topic.frequency })}</span>
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      topic.level >= 4
                        ? "text-emerald-400 bg-emerald-500/10"
                        : topic.level >= 3
                          ? "text-oracle-400 bg-oracle-500/10"
                        : "text-zinc-400 bg-surface-3"
                    }`}
                  >
                    {levelLabels[topic.level] || `L${topic.level}`}
                  </span>
                </div>
              </div>
              <div className="h-2 bg-surface-2 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${LEVEL_COLORS[topic.level] || "bg-zinc-500"}`}
                  style={{ width: `${Math.min((topic.level / 5) * 100, 100)}%` }}
                />
              </div>
              {topic.has_active_plan && topic.plan_progress !== null && (
                <div className="mt-1 flex items-center gap-1.5 text-[10px] text-oracle-400">
                  <Zap size={9} />
                  <span>
                    {t("pages.capabilityMap.topic.growthPlanProgress", { percent: (topic.plan_progress * 100).toFixed(0) })}
                  </span>
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      {/* Active improvement plans */}
      {data.active_plans.length > 0 && (
        <section className="mb-8">
          <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
            <Zap size={16} className="text-amber-400" />
            {t("pages.capabilityMap.sections.growthPlans")}
          </h2>
          <div className="space-y-3">
            {data.active_plans.map((plan) => (
              <PlanCard
                key={plan.plan_id}
                plan={plan}
                loading={actionLoading === plan.plan_id}
                onActivate={() => handleActivate(plan.plan_id)}
                onAbandon={() => handleAbandon(plan.plan_id)}
              />
            ))}
          </div>
        </section>
      )}

      {/* Cognitive quadrant */}
      <section className="mb-8">
        <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
          <Target size={16} className="text-oracle-400" />
          {t("pages.capabilityMap.sections.cognitiveQuadrants")}
        </h2>
        <div className="glass rounded-2xl p-5">
          <div className="grid grid-cols-2 gap-3">
            {Object.entries(quadrantMeta).map(([key, meta]) => {
              const q = data.cognitive_quadrant[key];
              const pct = q ? q.percentage : 0;
              return (
                <div
                  key={key}
                  className="rounded-xl bg-surface-1/50 p-4 text-center"
                >
                  <div
                    className={`w-3 h-3 rounded-full ${meta.color} mx-auto mb-2 opacity-60`}
                  />
                  <div className="text-lg font-display text-zinc-100">
                    {pct.toFixed(0)}%
                  </div>
                  <div className="text-xs text-zinc-500">{meta.label}</div>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* Reasoning trend */}
      {data.reasoning_trend.length > 1 && (
        <section className="mb-8">
          <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
            <TrendingUp size={16} className="text-emerald-400" />
            {t("pages.capabilityMap.sections.reasoningTrend")}
          </h2>
          <div className="glass rounded-2xl p-5">
            <div className="flex items-end gap-1 h-20">
              {data.reasoning_trend.map((v, i) => (
                <div
                  key={i}
                  className="flex-1 bg-oracle-500/30 rounded-t transition-all hover:bg-oracle-500/50"
                  style={{ height: `${v * 100}%` }}
                  title={`${(v * 100).toFixed(0)}%`}
                />
              ))}
            </div>
            <div className="flex justify-between text-xs text-zinc-700 mt-1">
              <span>{t("pages.capabilityMap.range.early")}</span>
              <span>{t("pages.capabilityMap.range.recent")}</span>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}

/* ---------- Sub-components ---------- */

function StatCard({
  icon,
  value,
  label,
}: {
  icon: React.ReactNode;
  value: number | string;
  label: string;
}) {
  return (
    <div className="glass rounded-xl p-4 text-center">
      <div className="flex items-center justify-center gap-1 mb-1">
        {icon}
      </div>
      <div className="text-xl font-display text-zinc-100">{value}</div>
      <div className="text-xs text-zinc-600">{label}</div>
    </div>
  );
}

function PlanCard({
  plan,
  loading,
  onActivate,
  onAbandon,
}: {
  plan: ImprovementPlanSummary;
  loading: boolean;
  onActivate: () => void;
  onAbandon: () => void;
}) {
  const { t } = useTranslation();
  const levelLabels = buildLevelLabels(t);
  const isProposed = plan.status === "proposed";
  const isActive = plan.status === "active";

  return (
    <div className="glass rounded-xl p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-medium text-zinc-200 truncate">
              {plan.topic}
            </span>
            <span
              className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                isProposed
                  ? "text-amber-400 bg-amber-500/10"
                  : "text-emerald-400 bg-emerald-500/10"
              }`}
            >
              {isProposed ? t("pages.capabilityMap.plan.status.proposed") : t("pages.capabilityMap.plan.status.active")}
            </span>
          </div>
          <div className="text-xs text-zinc-500">
            {levelLabels[plan.current_level] || `L${plan.current_level}`} →{" "}
            {levelLabels[plan.target_level] || `L${plan.target_level}`}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {isProposed && (
            <button
              onClick={onActivate}
              disabled={loading}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 hover:bg-emerald-500/20 transition-colors disabled:opacity-50"
            >
              {loading ? (
                <Loader2 size={11} className="animate-spin" />
              ) : (
                <Play size={11} />
              )}
              {t("pages.capabilityMap.plan.accept")}
            </button>
          )}
          {(isProposed || isActive) && (
            <button
              onClick={onAbandon}
              disabled={loading}
              className="flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-zinc-500 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-50"
            >
              <X size={11} />
            </button>
          )}
        </div>
      </div>

      {isActive && (
          <div className="mt-3">
            <div className="flex items-center justify-between text-xs text-zinc-500 mb-1">
              <span>
                {t("pages.capabilityMap.plan.challenges", {
                  delivered: plan.challenges_delivered,
                  engaged: plan.challenges_engaged,
                })}
              </span>
              <span>{(plan.progress * 100).toFixed(0)}%</span>
          </div>
          <div className="h-1.5 bg-surface-2 rounded-full overflow-hidden">
            <div
              className="h-full rounded-full bg-gradient-to-r from-oracle-500 to-emerald-500 transition-all"
              style={{ width: `${plan.progress * 100}%` }}
            />
          </div>
          {plan.milestones.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {plan.milestones.map((m, i) => (
                <span
                  key={i}
                  className="px-2 py-0.5 rounded-full text-[10px] bg-emerald-500/10 text-emerald-400/80 border border-emerald-500/10"
                >
                  ✓ {m}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
