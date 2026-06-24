import { useState, useEffect, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { History, Search, ChevronDown, Loader2, Clock, Layers, Star, GitBranch, Lightbulb } from "lucide-react";
import EnhancedMarkdown from "@/components/query/EnhancedMarkdown";
import ActionBar from "@/components/query/ActionBar";
import { api } from "@/api/client";
import type { HistoryItem } from "@/types";
import { useConversationContext } from "@/contexts/ConversationContext";

type Translate = (key: string, options?: Record<string, unknown>) => string;

function buildModeBadges(t: Translate): Record<string, { label: string; className: string }> {
  return {
    deep: { label: t("common.modes.deepName"), className: "bg-blue-500/10 text-blue-400 border border-blue-500/20" },
    light: { label: t("common.modes.lightName"), className: "bg-zinc-800 text-zinc-400" },
    research: { label: t("common.modes.researchName"), className: "bg-amber-500/10 text-amber-400 border border-amber-500/20" },
    socratic: { label: t("common.modes.socraticName"), className: "bg-violet-500/10 text-violet-400 border border-violet-500/20" },
  };
}

function qualityScore(t: Translate, confidence: number, qualityGate: string): {
  label: string; stars: number; color: string; desc: string;
} {
  if (qualityGate === "low_confidence" || confidence < 0.4) {
    return {
      color: "text-amber-400",
      desc: t("pages.history.quality.reference.desc"),
      label: t("pages.history.quality.reference.label"),
      stars: 2,
    };
  }
  if (qualityGate === "best_single") {
    if (confidence >= 0.75) {
      return {
        color: "text-emerald-400",
        desc: t("pages.history.quality.bestSingleHigh.desc"),
        label: t("pages.history.quality.bestSingleHigh.label"),
        stars: 4,
      };
    }
    return {
      color: "text-sky-400",
      desc: t("pages.history.quality.bestSingleGood.desc"),
      label: t("pages.history.quality.bestSingleGood.label"),
      stars: 3,
    };
  }
  if (confidence >= 0.85) {
    return {
      color: "text-oracle-400",
      desc: t("pages.history.quality.veryHigh.desc"),
      label: t("pages.history.quality.veryHigh.label"),
      stars: 5,
    };
  }
  if (confidence >= 0.7) {
    return {
      color: "text-emerald-400",
      desc: t("pages.history.quality.high.desc"),
      label: t("pages.history.quality.high.label"),
      stars: 4,
    };
  }
  if (confidence >= 0.5) {
    return {
      color: "text-sky-400",
      desc: t("pages.history.quality.good.desc"),
      label: t("pages.history.quality.good.label"),
      stars: 3,
    };
  }
  return {
    color: "text-amber-400",
    desc: t("pages.history.quality.reference.desc"),
    label: t("pages.history.quality.reference.label"),
    stars: 2,
  };
}

function QualityBadge({ confidence, qualityGate }: { confidence: number; qualityGate: string }) {
  const { t } = useTranslation();
  const q = qualityScore(t, confidence, qualityGate);
  return (
    <span className={`flex items-center gap-0.5 text-[10px] font-medium ${q.color}`} title={q.desc}>
      {Array.from({ length: 5 }).map((_, i) => (
        <Star
          key={i}
          size={9}
          className={i < q.stars ? "fill-current" : "opacity-20"}
        />
      ))}
      <span className="ml-0.5">{q.label}</span>
    </span>
  );
}

function relativeTime(iso: string, t: Translate, locale: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return t("common.sidebar.justNow");
  if (m < 60) return t("common.sidebar.minutesAgo", { count: m });
  const h = Math.floor(m / 60);
  if (h < 24) return t("common.sidebar.hoursAgo", { count: h });
  const d = Math.floor(h / 24);
  if (d < 30) return t("common.sidebar.daysAgo", { count: d });
  return new Date(iso).toLocaleDateString(locale, { month: "short", day: "numeric" });
}

export default function HistoryPage() {
  const navigate = useNavigate();
  const { t, i18n } = useTranslation();
  const { openHistoryAsConversation } = useConversationContext();
  const modeBadges = buildModeBadges(t);
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Record<string, "final" | "best_single">>({});
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const limit = 20;

  const loadHistory = useCallback(async (off: number) => {
    setLoading(true);
    try {
      const res = await api.history(limit, off);
      setItems((prev) => (off === 0 ? res.history : [...prev, ...res.history]));
      setTotal(res.total);
      setOffset(off);
    } catch {
      setError(t("pages.history.errors.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { void loadHistory(0); }, [loadHistory]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return items;
    return items.filter(i =>
      i.question.toLowerCase().includes(q) ||
      i.final_answer.toLowerCase().includes(q)
    );
  }, [items, search]);

  const hasMore = items.length < total;

  const continueFromHistory = (item: HistoryItem, currentTab: string) => {
    const hasBoth = item.best_single_answer && item.best_single_answer !== item.final_answer;
    const answerOverride = (hasBoth && currentTab === "best_single") ? item.best_single_answer : undefined;
    openHistoryAsConversation(item, answerOverride);
    navigate("/");
  };

  return (
    <div className="h-full overflow-y-auto max-w-3xl mx-auto px-4 sm:px-6 py-8">
      {/* Header */}
      <div className="flex items-center gap-2 mb-5">
        <History size={14} className="text-zinc-400" />
        <h1 className="text-sm font-medium text-zinc-200">{t("common.sidebar.history")}</h1>
        {!loading && total > 0 && (
          <span className="ml-auto text-[10px] text-zinc-600">{t("pages.history.totalCount", { count: total })}</span>
        )}
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-600 pointer-events-none" />
        <input
          type="text"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder={t("pages.history.searchPlaceholder")}
          className="w-full rounded-xl border border-zinc-800/50 bg-zinc-900/60 backdrop-blur pl-8 pr-3 py-2 text-xs text-zinc-300 placeholder-zinc-600 outline-none focus:border-zinc-700/80 transition-colors"
        />
      </div>

      {error && (
        <div className="p-3 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-xs mb-4">{error}</div>
      )}

      {filtered.length === 0 && !loading && (
        <div className="flex flex-col items-center justify-center py-20 gap-3">
          <History size={24} className="text-zinc-500" />
          <p className="text-sm text-zinc-400">{search ? t("pages.history.empty.noMatches") : t("pages.history.empty.noHistory")}</p>
        </div>
      )}

      <div className="space-y-2">
        {filtered.map((item) => {
          const badge = modeBadges[item.mode] ?? { label: item.mode, className: "bg-zinc-800 text-zinc-400" };
          const isExpanded = expandedId === item.query_id;
          const hasBothVersions = item.best_single_answer && item.best_single_answer !== item.final_answer;
          const tab = activeTab[item.query_id] ?? "final";

          return (
            <div key={item.query_id} className="rounded-2xl border border-zinc-800/50 bg-zinc-900/80 backdrop-blur overflow-hidden">
              {/* Header row */}
              <button
                onClick={() => setExpandedId(isExpanded ? null : item.query_id)}
                className="w-full px-4 py-3 text-left hover:bg-zinc-800/30 transition-colors"
              >
                <div className="flex items-start gap-2">
                  <span className={`mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${badge.className}`}>
                    {badge.label}
                  </span>
                  <p className="flex-1 min-w-0 text-sm text-zinc-200 truncate">{item.question}</p>
                  <div className="flex items-center gap-2 shrink-0 ml-1">
                    <span className="text-[11px] text-zinc-400">{relativeTime(item.created_at, t, i18n.resolvedLanguage || i18n.language || "zh-CN")}</span>
                    <ChevronDown size={13} className={`text-zinc-400 transition-transform ${isExpanded ? "rotate-180" : ""}`} />
                  </div>
                </div>
                {!isExpanded && (
                  <p className="mt-1.5 text-[13px] text-zinc-400 line-clamp-2 leading-relaxed pl-[calc(theme(spacing.2)+theme(spacing.10))]">
                    {item.final_answer.replace(/[#*`>]/g, "").slice(0, 200)}
                  </p>
                )}
              </button>

              {isExpanded && (
                <div className="border-t border-zinc-800/50">
                  {/* Meta bar */}
                  <div className="flex items-center gap-3 px-4 py-2 text-[11px] text-zinc-400 border-b border-zinc-800/30">
                    <QualityBadge confidence={item.confidence} qualityGate={item.quality_gate} />
                    <span className="flex items-center gap-1">
                      <Clock size={9} />
                      {(item.latency_ms / 1000).toFixed(1)}s
                    </span>
                    {hasBothVersions && (
                      <span className="ml-auto flex items-center gap-1 text-zinc-400">
                        <Layers size={9} />
                        {t("pages.history.meta.twoVersions")}
                      </span>
                    )}
                  </div>

                  {/* Version tabs — only show when both versions exist and differ */}
                  {hasBothVersions && (
                    <div className="flex gap-0 border-b border-zinc-800/30">
                      <button
                        onClick={() => setActiveTab(p => ({ ...p, [item.query_id]: "final" }))}
                        className={`flex items-center gap-1.5 px-4 py-2 text-[11px] font-medium transition-colors border-b-2 ${
                          tab === "final"
                            ? "text-oracle-400 border-oracle-500"
                            : "text-zinc-500 border-transparent hover:text-zinc-300"
                        }`}
                      >
                        <Layers size={10} />
                        {t("pages.history.tabs.final")}
                      </button>
                      <button
                        onClick={() => setActiveTab(p => ({ ...p, [item.query_id]: "best_single" }))}
                        className={`flex items-center gap-1.5 px-4 py-2 text-[11px] font-medium transition-colors border-b-2 ${
                          tab === "best_single"
                            ? "text-sky-400 border-sky-500"
                            : "text-zinc-500 border-transparent hover:text-zinc-300"
                        }`}
                      >
                        <Star size={10} />
                        {t("pages.history.tabs.bestSingle")}
                      </button>
                    </div>
                  )}

                  {/* Key insights */}
                  {item.key_insights && item.key_insights.length > 0 && (
                    <div className="px-4 py-2.5 border-b border-zinc-800/30">
                      <div className="flex items-center gap-1.5 mb-1.5">
                        <Lightbulb size={10} className="text-amber-400" />
                        <span className="text-[12px] font-medium text-amber-300">{t("pages.history.insights.title")}</span>
                      </div>
                      <ul className="space-y-1">
                        {item.key_insights.map((insight, i) => (
                          <li key={i} className="flex items-start gap-2 text-[14px] leading-relaxed text-zinc-100">
                            <span className="mt-0.5 shrink-0 w-4 h-4 rounded-full bg-amber-500/10 text-amber-300 flex items-center justify-center text-[10px] font-bold">{i + 1}</span>
                            {insight}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Divergence points */}
                  {item.divergence_points && item.divergence_points.length > 0 && (
                    <div className="px-4 py-2.5 border-b border-zinc-800/30">
                      <div className="flex items-center gap-1.5 mb-1.5">
                        <GitBranch size={10} className="text-blue-400" />
                        <span className="text-[12px] font-medium text-blue-300">{t("pages.history.divergence.title")}</span>
                      </div>
                      <div className="space-y-1.5">
                        {item.divergence_points.map((dp, i) => (
                          <div key={i} className="rounded-lg bg-blue-500/5 border border-blue-500/10 px-2.5 py-1.5">
                            <div className="flex items-center justify-between mb-0.5">
                              <span className="text-[13px] font-semibold text-blue-100">{dp.topic}</span>
                              <span className="text-[12px] text-zinc-100">{t("pages.history.divergence.consensus", { percent: Math.round(dp.consensus_ratio * 100) })}</span>
                            </div>
                            <p className="text-[13px] leading-relaxed text-zinc-100">{dp.description}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Content */}
                  <div className="px-4 py-3">
                    {tab === "final" || !hasBothVersions ? (
                      <EnhancedMarkdown content={item.final_answer} />
                    ) : (
                      <EnhancedMarkdown content={item.best_single_answer} />
                    )}
                    <div className="mt-3 pt-2 border-t border-zinc-800/40 flex items-center justify-between">
                      <button
                        type="button"
                        onClick={() => continueFromHistory(item, tab)}
                        className="px-3 py-1.5 rounded-lg text-[13px] text-oracle-300 bg-oracle-500/10 border border-oracle-500/25 hover:bg-oracle-500/20 transition-colors"
                      >
                        {t("pages.history.continueQuestion")}
                      </button>
                      <ActionBar
                        content={tab === "final" || !hasBothVersions ? item.final_answer : item.best_single_answer}
                        title={item.question}
                        queryId={item.query_id}
                      />
                    </div>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {loading && (
        <div className="flex justify-center py-10">
          <Loader2 size={18} className="animate-spin text-zinc-600" />
        </div>
      )}

      {hasMore && !loading && (
        <div className="flex justify-center mt-5">
          <button
            onClick={() => loadHistory(offset + limit)}
            className="px-5 py-2 rounded-full text-xs text-zinc-500 border border-zinc-800/50 hover:border-zinc-700 hover:text-zinc-300 transition-all"
          >
            {t("pages.history.loadMore", { count: total - items.length })}
          </button>
        </div>
      )}
    </div>
  );
}
