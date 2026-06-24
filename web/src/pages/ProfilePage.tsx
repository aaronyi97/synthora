import { useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Brain,
  Target,
  TrendingUp,
  Loader2,
  ShieldCheck,
  ShieldOff,
  Sprout,
  Compass,
  Activity,
  Clock,
  BarChart3,
  Trash2,
  Download,
  Gauge,
  AlertTriangle,
} from "lucide-react";
import { api } from "@/api/client";
import type { ApiError, CognitiveSummaryResponse, BehaviorSummaryResponse, GrowthDashboardResponse } from "@/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

function buildModeLabels(t: Translate): Record<string, string> {
  return {
    deep: t("pages.profile.modeLabels.deep"),
    light: t("pages.profile.modeLabels.light"),
    research: t("pages.profile.modeLabels.research"),
    socratic: t("pages.profile.modeLabels.socratic"),
  };
}

function buildQuadrantLabels(t: Translate): Record<string, { label: string; desc: string; color: string }> {
  return {
    known_known: {
      color: "bg-emerald-500",
      desc: t("common.cognitiveQuadrants.knownKnown.desc"),
      label: t("common.cognitiveQuadrants.knownKnown.label"),
    },
    known_unknown: {
      color: "bg-oracle-500",
      desc: t("common.cognitiveQuadrants.knownUnknown.desc"),
      label: t("common.cognitiveQuadrants.knownUnknown.label"),
    },
    unknown_known: {
      color: "bg-sky-500",
      desc: t("common.cognitiveQuadrants.unknownKnown.desc"),
      label: t("common.cognitiveQuadrants.unknownKnown.label"),
    },
    unknown_unknown: {
      color: "bg-amber-500",
      desc: t("common.cognitiveQuadrants.unknownUnknown.desc"),
      label: t("common.cognitiveQuadrants.unknownUnknown.label"),
    },
  };
}

export default function ProfilePage() {
  const { t } = useTranslation();
  const quadrantLabels = buildQuadrantLabels(t);
  const modeLabels = buildModeLabels(t);
  const [cogData, setCogData] = useState<CognitiveSummaryResponse | null>(null);
  const [behaviorData, setBehaviorData] = useState<BehaviorSummaryResponse | null>(null);
  const [growthData, setGrowthData] = useState<GrowthDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [consentLoading, setConsentLoading] = useState(false);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [usageData, setUsageData] = useState<{ usage: Record<string, number>; limits: Record<string, number>; remaining: Record<string, number> } | null>(null);
  const [exportLoading, setExportLoading] = useState(false);
  const [deleteAccountLoading, setDeleteAccountLoading] = useState(false);
  const [showDeleteAccountConfirm, setShowDeleteAccountConfirm] = useState(false);
  const [deleteAccountPassword, setDeleteAccountPassword] = useState("");
  const [deleteAccountError, setDeleteAccountError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [cog, beh, growth] = await Promise.all([
        api.cognitiveSummary(),
        api.behaviorSummary(),
        api.growthDashboard(),
      ]);
      setCogData(cog);
      setBehaviorData(beh);
      setGrowthData(growth);
    } catch {
      setError(t("pages.profile.errors.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { void load(); }, [load]);

  // Load usage quota
  useEffect(() => {
    api.profileUsage().then(setUsageData).catch(() => {});
  }, []);

  const handleExport = async () => {
    setExportLoading(true);
    try {
      const blob = await api.profileExport();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `synthora-profile-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      setError(t("pages.profile.errors.exportFailed"));
    } finally {
      setExportLoading(false);
    }
  };

  const toggleConsent = async () => {
    if (!cogData) return;
    setConsentLoading(true);
    try {
      const res = await api.cognitiveConsent(!cogData.cognitive_tracking_consent);
      setCogData({ ...cogData, cognitive_tracking_consent: res.cognitive_tracking_consent });
    } catch {
      setError(t("pages.profile.errors.updateSettingsFailed"));
    } finally {
      setConsentLoading(false);
    }
  };

  const handleDeleteAccount = async () => {
    if (!deleteAccountPassword.trim()) {
      setDeleteAccountError(t("pages.profile.deleteAccount.passwordRequired"));
      return;
    }
    setDeleteAccountError(null);
    setDeleteAccountLoading(true);
    try {
      await api.deleteAccount(deleteAccountPassword);
      localStorage.clear();
      window.location.href = "/";
    } catch (e) {
      const err = e as ApiError;
      if (err.detail) {
        setDeleteAccountError(err.detail);
      } else {
        setDeleteAccountError(t("pages.profile.deleteAccount.deleteFailed"));
      }
      setDeleteAccountLoading(false);
    }
  };

  const deleteCognitiveData = async () => {
    if (!confirm(t("pages.profile.deleteCognitiveData.confirm"))) return;
    setDeleteLoading(true);
    try {
      await api.cognitiveDelete();
      await load();
    } catch {
      setError(t("pages.profile.errors.deleteFailed"));
    } finally {
      setDeleteLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 size={24} className="animate-spin text-zinc-600" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
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

  const d = cogData?.data;
  const hasCogData = d && d.socratic_sessions > 0;
  const hasBehavior = behaviorData?.has_data;
  const totalQ = d ? Object.values(d.quadrant_dist).reduce((a, b) => a + b, 0) || 1 : 1;

  return (
    <div className="h-full overflow-y-auto max-w-3xl mx-auto px-4 sm:px-6 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-display text-zinc-100 flex items-center gap-2.5">
            <Brain size={22} className="text-oracle-400" />
            {t("pages.profile.title")}
          </h1>
          <p className="text-sm text-zinc-600 mt-1">
            {t("pages.profile.subtitle")}
          </p>
        </div>
        {cogData && (
          <div className="flex items-center gap-2">
            <button
              onClick={handleExport}
              disabled={exportLoading}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded-full text-xs text-zinc-600 border border-surface-4/30 hover:text-sky-400 hover:border-sky-500/30 hover:bg-sky-500/5 transition-all disabled:opacity-50"
              title={t("pages.profile.actions.exportAllData")}
            >
              {exportLoading ? <Loader2 size={11} className="animate-spin" /> : <Download size={11} />}
            </button>
            <button
              onClick={deleteCognitiveData}
              disabled={deleteLoading}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded-full text-xs text-zinc-600 border border-surface-4/30 hover:text-red-400 hover:border-red-500/30 hover:bg-red-500/5 transition-all disabled:opacity-50"
              title={t("pages.profile.actions.deleteAllCognitiveData")}
            >
              <Trash2 size={11} />
            </button>
            <button
              onClick={toggleConsent}
              disabled={consentLoading}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs transition-all ${
                cogData.cognitive_tracking_consent
                  ? "text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/10"
                  : "text-zinc-500 border border-surface-4/50 hover:bg-surface-3"
              }`}
            >
              {cogData.cognitive_tracking_consent ? <ShieldCheck size={13} /> : <ShieldOff size={13} />}
              {consentLoading ? "..." : cogData.cognitive_tracking_consent ? t("pages.profile.tracking.enabled") : t("pages.profile.tracking.disabled")}
            </button>
          </div>
        )}
      </div>

      {/* ============ Daily Usage Quota ============ */}
      {usageData && (
        <section className="mb-8">
          <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
            <Gauge size={16} className="text-sky-400" />
            {t("pages.profile.sections.dailyQuota")}
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {Object.entries(usageData.limits).map(([mode, limit]) => {
              const used = usageData.usage[mode] || 0;
              const remaining = usageData.remaining[mode] ?? limit;
              const pct = limit > 0 ? (used / limit) * 100 : 0;
              return (
                <div key={mode} className="glass rounded-xl p-3 text-center">
                  <div className="text-xs text-zinc-500 mb-1 capitalize">{modeLabels[mode] || mode}</div>
                  <div className="text-lg font-display text-zinc-100">{remaining}</div>
                  <div className="text-[10px] text-zinc-600">{t("pages.profile.quota.remaining", { limit })}</div>
                  <div className="h-1 bg-surface-2 rounded-full mt-2 overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${pct > 80 ? "bg-red-500" : pct > 50 ? "bg-amber-500" : "bg-emerald-500"}`}
                      style={{ width: `${Math.min(pct, 100)}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* Growth dashboard */}
      <section className="mb-8">
        <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
          <TrendingUp size={16} className="text-emerald-400" />
          {t("pages.profile.sections.growthMap")}
        </h2>

        {!growthData?.has_data ? (
          <div className="glass rounded-2xl p-6 text-center">
            <div className="w-14 h-14 mx-auto rounded-full bg-emerald-500/10 flex items-center justify-center mb-3">
              <Sprout size={24} className="text-emerald-500/50" />
            </div>
            <p className="text-zinc-400 text-sm mb-1">{t("pages.profile.growth.empty.title")}</p>
            <p className="text-xs text-zinc-600 max-w-sm mx-auto">
              {t("pages.profile.growth.empty.subtitle")}
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Summary stats */}
            <div className="grid grid-cols-3 gap-3">
              <div className="glass rounded-xl p-4 text-center">
                <div className="text-2xl font-display text-zinc-100">{growthData.summary.total_topics}</div>
                <div className="text-xs text-zinc-600 mt-1">{t("pages.profile.growth.stats.exploredTopics")}</div>
              </div>
              <div className="glass rounded-xl p-4 text-center">
                <div className="text-2xl font-display text-emerald-400">{growthData.summary.deep_topics}</div>
                <div className="text-xs text-zinc-600 mt-1">{t("pages.profile.growth.stats.deepTopics")}</div>
              </div>
              <div className="glass rounded-xl p-4 text-center">
                <div className="text-2xl font-display text-oracle-400">{growthData.summary.growth_score}%</div>
                <div className="text-xs text-zinc-600 mt-1">{t("pages.profile.growth.stats.growthScore")}</div>
              </div>
            </div>

            {/* Topic depth cards */}
            <div className="glass rounded-2xl p-5">
              <h3 className="text-sm font-medium text-zinc-300 mb-4">{t("pages.profile.growth.topicDepthProgress")}</h3>
              <div className="space-y-3">
                {growthData.topics.map((topic) => (
                  <div key={topic.topic}>
                    <div className="flex items-center justify-between text-xs mb-1">
                      <span className="text-zinc-300 font-medium">{topic.topic}</span>
                      <span className={`px-1.5 py-0.5 rounded text-xs ${
                        topic.depth >= 4 ? "bg-oracle-500/20 text-oracle-400" :
                        topic.depth >= 3 ? "bg-emerald-500/20 text-emerald-400" :
                        "bg-surface-3 text-zinc-500"
                      }`}>
                        {t("pages.profile.growth.topicDepthLabel", { depth: topic.depth, label: topic.depth_label })}
                      </span>
                    </div>
                    <div className="h-2 bg-surface-2 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all ${
                          topic.depth >= 4 ? "bg-oracle-500" :
                          topic.depth >= 3 ? "bg-emerald-500" :
                          topic.depth >= 2 ? "bg-sky-500/60" :
                          "bg-zinc-600"
                        }`}
                        style={{ width: `${(topic.depth / 5) * 100}%` }}
                      />
                    </div>
                    <div className="flex justify-between text-xs text-zinc-700 mt-0.5">
                      <span>{t("pages.profile.growth.explorationCount", { count: topic.frequency })}</span>
                      <span>{t("pages.profile.growth.expertise", { percent: (topic.expertise * 100).toFixed(0) })}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </section>

      {/* Behavior narrative */}
      <section className="mb-8">
        <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
          <Activity size={16} className="text-oracle-400" />
          {t("pages.profile.sections.behaviorInsights")}
        </h2>

        {!hasBehavior ? (
          <div className="glass rounded-2xl p-6 text-center">
            <div className="w-14 h-14 mx-auto rounded-full bg-oracle-500/10 flex items-center justify-center mb-3">
              <Compass size={24} className="text-oracle-500/50" />
            </div>
            <p className="text-zinc-400 text-sm mb-1">{t("pages.profile.behavior.empty.title")}</p>
            <p className="text-xs text-zinc-600 max-w-sm mx-auto">
              {t("pages.profile.behavior.empty.subtitle")}
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Narrative card */}
            <div className="glass rounded-2xl p-6 glow-oracle">
              <div className="text-sm leading-relaxed text-zinc-300 whitespace-pre-wrap">
                {behaviorData!.narrative}
              </div>
              <div className="mt-4 pt-3 border-t border-surface-4/20 flex items-center justify-between text-xs text-zinc-600">
                <span>{t("pages.profile.behavior.basedOnQueries", { count: behaviorData!.total_queries })}</span>
                <span className="text-oracle-500/40">{t("pages.profile.behavior.noLabels")}</span>
              </div>
            </div>

            {/* Metrics cards */}
            {behaviorData!.metrics && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                {/* Divergent-Convergent card */}
                <div className="glass rounded-xl p-5">
                  <h3 className="text-xs font-medium text-zinc-400 mb-3 flex items-center gap-1.5">
                    <BarChart3 size={13} className="text-oracle-400" />
                    {t("pages.profile.behavior.explorationStyle")}
                  </h3>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-zinc-500">{t("pages.profile.behavior.newDirections")}</span>
                      <span className="text-sm text-zinc-200 font-medium">
                        {behaviorData!.metrics!.divergent_convergent.new_topics}
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-zinc-500">{t("pages.profile.behavior.deepenedTopics")}</span>
                      <span className="text-sm text-zinc-200 font-medium">
                        {behaviorData!.metrics!.divergent_convergent.deepened_topics}
                      </span>
                    </div>
                    {/* Switch-rate bar */}
                    <div className="mt-2">
                      <div className="flex justify-between text-xs text-zinc-600 mb-1">
                        <span>{t("pages.profile.behavior.convergent")}</span>
                        <span>{t("pages.profile.behavior.divergent")}</span>
                      </div>
                      <div className="h-2 bg-surface-2 rounded-full overflow-hidden">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-oracle-500 transition-all"
                          style={{ width: `${(behaviorData!.metrics!.divergent_convergent.switch_rate) * 100}%` }}
                        />
                      </div>
                    </div>
                    {behaviorData!.metrics!.divergent_convergent.top_recurring.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {behaviorData!.metrics!.divergent_convergent.top_recurring.map((t, i) => (
                          <span key={i} className="px-2 py-0.5 rounded-full text-xs bg-surface-3 text-zinc-400">
                            {t}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                {/* Engagement card */}
                <div className="glass rounded-xl p-5">
                  <h3 className="text-xs font-medium text-zinc-400 mb-3 flex items-center gap-1.5">
                    <Clock size={13} className="text-oracle-400" />
                    {t("pages.profile.behavior.usagePatterns")}
                  </h3>
                  <div className="space-y-2">
                    {behaviorData!.metrics!.engagement.peak_hours.length > 0 && (
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-zinc-500">{t("pages.profile.behavior.peakHours")}</span>
                        <span className="text-sm text-zinc-200 font-medium">
                          {behaviorData!.metrics!.engagement.peak_hours.map(h => `${h}:00`).join(", ")}
                        </span>
                      </div>
                    )}
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-zinc-500">{t("pages.profile.behavior.favoriteMode")}</span>
                      <span className="text-sm text-zinc-200 font-medium capitalize">
                        {modeLabels[behaviorData!.metrics!.engagement.favorite_mode] || behaviorData!.metrics!.engagement.favorite_mode}
                      </span>
                    </div>
                    {/* Mode distribution bars */}
                    <div className="mt-2 space-y-1.5">
                      {Object.entries(behaviorData!.metrics!.engagement.mode_distribution)
                        .sort(([, a], [, b]) => b - a)
                        .map(([mode, pct]) => (
                          <div key={mode}>
                            <div className="flex items-center justify-between text-xs mb-0.5">
                              <span className="text-zinc-500 capitalize">{modeLabels[mode] || mode}</span>
                              <span className="text-zinc-600">{pct}%</span>
                            </div>
                            <div className="h-1.5 bg-surface-2 rounded-full overflow-hidden">
                              <div
                                className="h-full rounded-full bg-oracle-500/50 transition-all"
                                style={{ width: `${pct}%` }}
                              />
                            </div>
                          </div>
                        ))}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </section>

      {/* Socratic cognitive data */}
      {!hasCogData ? (
        <section>
          <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
            <Target size={16} className="text-oracle-400" />
            {t("pages.profile.sections.socraticGrowth")}
          </h2>
          <div className="glass rounded-2xl p-6 text-center">
            <p className="text-zinc-400 text-sm mb-1">{t("pages.profile.socratic.empty.title")}</p>
            <p className="text-xs text-zinc-600 max-w-sm mx-auto">
              {t("pages.profile.socratic.empty.subtitle")}
            </p>
          </div>
        </section>
      ) : d && (
        <section>
          <h2 className="text-base font-medium text-zinc-200 mb-4 flex items-center gap-2">
            <Target size={16} className="text-oracle-400" />
            {t("pages.profile.sections.socraticGrowth")}
          </h2>
          <div className="space-y-4">
            {/* Stats row */}
            <div className="grid grid-cols-3 gap-3">
              <div className="glass rounded-xl p-4 text-center">
                <div className="text-2xl font-display text-zinc-100">{d.socratic_sessions}</div>
                <div className="text-xs text-zinc-600 mt-1">{t("pages.profile.socratic.stats.sessions")}</div>
              </div>
              <div className="glass rounded-xl p-4 text-center">
                <div className="text-2xl font-display text-zinc-100">
                  {(d.avg_reasoning_quality * 100).toFixed(0)}%
                </div>
                <div className="text-xs text-zinc-600 mt-1">{t("pages.profile.socratic.stats.reasoningQuality")}</div>
              </div>
              <div className="glass rounded-xl p-4 text-center">
                <div className="text-2xl font-display text-zinc-100">
                  {(d.completion_rate * 100).toFixed(0)}%
                </div>
                <div className="text-xs text-zinc-600 mt-1">{t("pages.profile.socratic.stats.completionRate")}</div>
              </div>
            </div>

            {/* Johari Window */}
            <div className="glass rounded-2xl p-5">
              <h3 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-2">
                <Target size={14} className="text-oracle-400" />
                {t("pages.profile.socratic.cognitiveQuadrants")}
              </h3>
              <div className="space-y-3">
                {Object.entries(quadrantLabels).map(([key, meta]) => {
                  const count = d.quadrant_dist[key] || 0;
                  const pct = (count / totalQ) * 100;
                  return (
                    <div key={key}>
                      <div className="flex items-center justify-between text-xs mb-1">
                        <span className="text-zinc-400">{meta.label}</span>
                        <span className="text-zinc-600">{pct.toFixed(0)}% ({count})</span>
                      </div>
                      <div className="h-2 bg-surface-2 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${meta.color} transition-all`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <p className="text-xs text-zinc-700 mt-0.5">{meta.desc}</p>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Growth zones */}
            {d.growth_zone_topics.length > 0 && (
              <div className="glass rounded-2xl p-5">
                <h3 className="text-sm font-medium text-zinc-300 mb-3 flex items-center gap-2">
                  <Sprout size={14} className="text-emerald-400" />
                  {t("pages.profile.socratic.growthZoneTopics")}
                </h3>
                <div className="flex flex-wrap gap-2">
                  {d.growth_zone_topics.map((topic, i) => (
                    <span key={i} className="px-3 py-1 rounded-full text-xs bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                      {topic}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Reasoning trend */}
            {d.reasoning_trend.length > 1 && (
              <div className="glass rounded-2xl p-5">
                <h3 className="text-sm font-medium text-zinc-300 mb-3 flex items-center gap-2">
                  <TrendingUp size={14} className="text-oracle-400" />
                  {t("pages.profile.socratic.reasoningTrend")}
                </h3>
                <div className="flex items-end gap-1 h-16">
                  {d.reasoning_trend.map((v, i) => (
                    <div
                      key={i}
                      className="flex-1 bg-oracle-500/30 rounded-t transition-all hover:bg-oracle-500/50"
                      style={{ height: `${v * 100}%` }}
                      title={t("pages.profile.socratic.dialogueQualityTitle", { index: i + 1, percent: (v * 100).toFixed(0) })}
                    />
                  ))}
                </div>
                <div className="flex justify-between text-xs text-zinc-700 mt-1">
                  <span>{t("pages.profile.range.first")}</span>
                  <span>{t("pages.profile.range.latest")}</span>
                </div>
              </div>
            )}
          </div>
        </section>
      )}

      {/* Account management */}
      <section className="mt-10 pt-6 border-t border-surface-4/20">
        <h2 className="text-xs font-medium text-zinc-600 uppercase tracking-wider mb-4">{t("pages.profile.accountManagement.title")}</h2>
        <div className="space-y-3">
          <div className="flex items-center justify-between glass rounded-xl px-4 py-3">
            <div>
              <div className="text-sm text-zinc-300">{t("pages.profile.accountManagement.export.title")}</div>
              <div className="text-xs text-zinc-600 mt-0.5">{t("pages.profile.accountManagement.export.subtitle")}</div>
            </div>
            <button
              onClick={handleExport}
              disabled={exportLoading}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-sky-400 border border-sky-500/30 hover:bg-sky-500/10 transition-all disabled:opacity-50"
            >
              {exportLoading ? <Loader2 size={11} className="animate-spin" /> : <Download size={11} />}
              {t("pages.profile.accountManagement.export.action")}
            </button>
          </div>

          <div className="flex items-center justify-between glass rounded-xl px-4 py-3">
            <div>
              <div className="text-sm text-zinc-400">{t("pages.profile.accountManagement.privacy.title")}</div>
              <div className="text-xs text-zinc-600 mt-0.5">{t("pages.profile.accountManagement.privacy.subtitle")}</div>
            </div>
            <Link
              to="/privacy"
              className="text-xs text-oracle-400/70 hover:text-oracle-400 transition-colors"
            >
              {t("pages.profile.accountManagement.privacy.action")}
            </Link>
          </div>

          <div className="flex items-center justify-between glass rounded-xl px-4 py-3 border border-red-500/10">
            <div>
              <div className="text-sm text-red-400">{t("pages.profile.accountManagement.deleteAccount.title")}</div>
              <div className="text-xs text-zinc-600 mt-0.5">{t("pages.profile.accountManagement.deleteAccount.subtitle")}</div>
            </div>
            <button
              onClick={() => {
                setDeleteAccountPassword("");
                setDeleteAccountError(null);
                setShowDeleteAccountConfirm(true);
              }}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-red-400 border border-red-500/30 hover:bg-red-500/10 transition-all"
            >
              <Trash2 size={11} />
              {t("pages.profile.accountManagement.deleteAccount.action")}
            </button>
          </div>
        </div>
      </section>

      {/* Delete-account confirmation modal */}
      {showDeleteAccountConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
          <div className="bg-surface-1 border border-surface-4/40 rounded-2xl p-6 max-w-sm w-full shadow-2xl">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-red-500/10 flex items-center justify-center">
                <AlertTriangle size={18} className="text-red-400" />
              </div>
              <div>
                <div className="font-semibold text-zinc-100">{t("pages.profile.deleteAccount.confirmTitle")}</div>
                <div className="text-xs text-zinc-500 mt-0.5">{t("pages.profile.deleteAccount.irreversible")}</div>
              </div>
            </div>
            <p className="text-sm text-zinc-400 mb-6 leading-relaxed">
              {t("pages.profile.deleteAccount.description")}
            </p>
            <label className="block mb-4">
              <span className="block text-xs text-zinc-500 mb-2">{t("pages.profile.deleteAccount.passwordPrompt")}</span>
              <input
                type="password"
                value={deleteAccountPassword}
                onChange={(e) => {
                  setDeleteAccountPassword(e.target.value);
                  if (deleteAccountError) setDeleteAccountError(null);
                }}
                autoComplete="current-password"
                className="w-full px-3 py-2 rounded-xl bg-surface-2 border border-surface-4/40 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:ring-2 focus:ring-red-500/30"
                placeholder={t("pages.profile.deleteAccount.passwordPlaceholder")}
              />
            </label>
            {deleteAccountError && (
              <div className="mb-4 rounded-xl border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                {deleteAccountError}
              </div>
            )}
            <div className="flex gap-3">
              <button
                onClick={() => {
                  setShowDeleteAccountConfirm(false);
                  setDeleteAccountPassword("");
                  setDeleteAccountError(null);
                }}
                className="flex-1 px-4 py-2 rounded-xl text-sm text-zinc-400 border border-surface-4/40 hover:bg-surface-3 transition-colors"
              >
                {t("common.actions.cancel")}
              </button>
              <button
                onClick={handleDeleteAccount}
                disabled={deleteAccountLoading || !deleteAccountPassword.trim()}
                className="flex-1 px-4 py-2 rounded-xl text-sm text-red-300 bg-red-500/15 border border-red-500/30 hover:bg-red-500/25 transition-colors disabled:opacity-50"
              >
                {deleteAccountLoading ? <Loader2 size={14} className="animate-spin mx-auto" /> : t("pages.profile.deleteAccount.confirmAction")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
