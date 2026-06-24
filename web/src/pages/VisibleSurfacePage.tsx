import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import ModeSelector from "@/components/query/ModeSelector";
import PostAnswerGuidance from "@/components/query/PostAnswerGuidance";
import QueryInput from "@/components/query/QueryInput";
import QueryProgressBars from "@/components/query/QueryProgressBars";
import ResponseDisplay from "@/components/query/ResponseDisplay";
import type { QueryTask } from "@/hooks/useQueryTasks";
import type { AskResponse, Mode } from "@/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

function buildBaseResponse(t: Translate, overrides: Partial<AskResponse>): AskResponse {
  return {
    query_id: "visible-surface-response",
    question: t("pages.visibleSurface.mock.base.question"),
    mode: "light",
    final_answer: t("pages.visibleSurface.mock.base.answer"),
    confidence: 0.84,
    quality_gate: "synthesized",
    has_divergence: false,
    divergence_summary: null,
    key_insights: [],
    latency_ms: 2400,
    estimated_cost_usd: 0.01,
    contributor_count: 1,
    individual_responses: null,
    companion_hint: null,
    preflight: null,
    pipeline_started: true,
    fast_path: false,
    low_confidence_actions: [],
    session_id: null,
    context_compressed: false,
    search_citations: [],
    draft_answers: [],
    divergence_points: [],
    consensus_points: [],
    fact_warnings: [],
    companion_guide: null,
    consensus_type: "unknown",
    guidance: null,
    reason_code: "standard",
    ai_disclosure: t("pages.visibleSurface.mock.base.disclosure"),
    ...overrides,
  };
}

function buildLightResponse(t: Translate): AskResponse {
  return buildBaseResponse(t, {
    contributor_count: 1,
    fast_path: true,
    final_answer: t("pages.visibleSurface.mock.light.answer"),
    latency_ms: 1800,
    mode: "light",
    query_id: "visible-light",
    question: t("pages.visibleSurface.mock.light.question"),
    reason_code: "fast_path",
    search_citations: [
      {
        snippet: t("pages.visibleSurface.mock.light.citations.useEffect"),
        source: "react.dev",
        title: "React useEffect",
        url: "https://react.dev/reference/react/useEffect",
      },
      {
        snippet: t("pages.visibleSurface.mock.light.citations.pureComponents"),
        source: "react.dev",
        title: "Keeping Components Pure",
        url: "https://react.dev/learn/keeping-components-pure",
      },
    ],
  });
}

function buildDeepResponse(t: Translate): AskResponse {
  return buildBaseResponse(t, {
    confidence: 0.78,
    consensus_type: "majority",
    contributor_count: 5,
    estimated_cost_usd: 0.24,
    fast_path: false,
    final_answer: t("pages.visibleSurface.mock.deep.answer"),
    key_insights: [
      t("pages.visibleSurface.mock.deep.insights.asymmetric"),
      t("pages.visibleSurface.mock.deep.insights.migration"),
    ],
    latency_ms: 94200,
    mode: "research",
    quality_gate: "best_single",
    query_id: "visible-deep",
    question: t("pages.visibleSurface.mock.deep.question"),
    reason_code: "single_model_fast_path",
  });
}

function buildDispatcherGuidanceResponse(t: Translate): AskResponse {
  return buildBaseResponse(t, {
    guidance: {
      confidence_level: "medium",
      confidence_statement: "",
      intensity: "rich",
      is_folded: true,
      message: t("pages.visibleSurface.mock.dispatcher.message"),
      route_reason: t("pages.visibleSurface.mock.dispatcher.routeReason"),
      show_dismiss: true,
      source: "dispatcher",
      suggestions: [
        {
          action_payload: { question: t("pages.visibleSurface.mock.dispatcher.followupQuestion") },
          action_type: "query_followup",
          estimated_cost_usd: 0,
          estimated_seconds: 45,
          id: "dispatcher-followup",
          label: t("pages.visibleSurface.mock.dispatcher.followupLabel"),
          rationale: "",
          requires_confirm: false,
        },
        {
          action_payload: {
            navigate: "/roundtable",
            question: t("pages.visibleSurface.mock.deep.question"),
          },
          action_type: "navigate",
          estimated_cost_usd: 0,
          estimated_seconds: 60,
          id: "dispatcher-roundtable",
          label: t("pages.visibleSurface.mock.dispatcher.roundtableLabel"),
          rationale: "",
          requires_confirm: false,
        },
      ],
      trigger: "fold",
    },
    mode: "deep",
    query_id: "visible-guidance-dispatcher",
    question: t("pages.visibleSurface.mock.dispatcher.question"),
  });
}

function buildSampleTasks(t: Translate, lightResponse: AskResponse): QueryTask[] {
  return [
    {
      abortController: new AbortController(),
      clarification: null,
      companionRoute: null,
      companionSkeleton: false,
      contributorsDone: 3,
      draftAnswers: [],
      elapsed: 68,
      error: null,
      id: "visible-task-1",
      mode: "deep",
      modelResponses: [],
      question: t("pages.visibleSurface.mock.tasks.streaming.question"),
      questionConfirmation: null,
      response: null,
      stage: t("pages.visibleSurface.mock.tasks.streaming.stage"),
      stageDetail: t("pages.visibleSurface.mock.tasks.streaming.stageDetail"),
      stageHistory: [],
      stageStartedAt: Date.now() - 16000,
      startTime: Date.now() - 68000,
      status: "streaming",
      streamCitations: [],
      streamPreview: "",
      streamTokens: t("pages.visibleSurface.mock.tasks.streaming.streamTokens"),
      waitIssueCode: null,
    },
    {
      abortController: new AbortController(),
      clarification: null,
      companionRoute: null,
      companionSkeleton: false,
      contributorsDone: 1,
      draftAnswers: [],
      elapsed: 12,
      error: null,
      id: "visible-task-2",
      mode: "light",
      modelResponses: [],
      question: t("pages.visibleSurface.mock.tasks.done.question"),
      questionConfirmation: null,
      response: lightResponse,
      stage: t("pages.visibleSurface.mock.tasks.done.stage"),
      stageDetail: "",
      stageHistory: [],
      stageStartedAt: Date.now() - 2000,
      startTime: Date.now() - 12000,
      status: "done",
      streamCitations: [],
      streamPreview: "",
      streamTokens: "",
      waitIssueCode: null,
    },
  ];
}

function SectionCard({
  id,
  title,
  description,
  children,
}: {
  id?: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-24 overflow-hidden rounded-[28px] border border-white/[0.1] bg-zinc-950/65 shadow-[0_24px_60px_rgba(0,0,0,0.28)] backdrop-blur-xl">
      <div className="border-b border-white/[0.08] bg-white/[0.03] px-6 py-4">
        <div className="text-[18px] font-semibold text-zinc-50">{title}</div>
        <p className="mt-1 text-[14px] leading-7 text-zinc-300">{description}</p>
      </div>
      <div className="px-5 py-5 sm:px-6">{children}</div>
    </section>
  );
}

export default function VisibleSurfacePage() {
  const { t } = useTranslation();
  const [mode, setMode] = useState<Mode>("auto");
  const [selectedTaskId, setSelectedTaskId] = useState<string>("visible-task-1");
  const [submittedQuestion, setSubmittedQuestion] = useState("");
  const lightResponse = useMemo(() => buildLightResponse(t), [t]);
  const deepResponse = useMemo(() => buildDeepResponse(t), [t]);
  const dispatcherGuidanceResponse = useMemo(() => buildDispatcherGuidanceResponse(t), [t]);
  const sampleTasks = useMemo(() => buildSampleTasks(t, lightResponse), [lightResponse, t]);

  const footerRight = useMemo(
    () => (
      <ModeSelector
        selected={mode}
        onChange={setMode}
        webSearch
        onWebSearchChange={() => {}}
      />
    ),
    [mode],
  );

  return (
    <div className="min-h-screen overflow-y-auto bg-[radial-gradient(circle_at_top,_rgba(234,179,8,0.18),_transparent_30%),linear-gradient(180deg,_#0b0b0d_0%,_#09090b_100%)] px-4 pb-20 pt-24 sm:px-6">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <section className="overflow-hidden rounded-[32px] border border-white/[0.12] bg-zinc-950/72 shadow-[0_28px_80px_rgba(0,0,0,0.34)] backdrop-blur-xl">
          <div className="border-b border-white/[0.08] bg-white/[0.03] px-6 py-5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full border border-oracle-500/30 bg-oracle-500/[0.14] px-3 py-1 text-[13px] font-semibold text-oracle-200">
                Visible Surface Review
              </span>
              <span className="rounded-full border border-amber-500/30 bg-amber-500/[0.12] px-3 py-1 text-[13px] font-semibold text-amber-200">
                {t("pages.visibleSurface.badges.noAuthNoBackend")}
              </span>
            </div>
            <h1 className="mt-4 text-[32px] font-semibold tracking-tight text-zinc-50">
              {t("pages.visibleSurface.title")}
            </h1>
            <p className="mt-3 max-w-4xl text-[16px] leading-8 text-zinc-300">
              {t("pages.visibleSurface.subtitle")}
            </p>
          </div>

          <div className="grid gap-4 px-5 py-5 lg:grid-cols-[1.15fr_0.85fr] sm:px-6">
            <div className="rounded-[26px] border border-white/[0.12] bg-black/25 p-4">
              <div className="mb-3 text-[16px] font-semibold text-zinc-100">{t("pages.visibleSurface.inputPreview.title")}</div>
              <QueryInput
                mode={mode}
                loading={false}
                onSubmit={(question) => setSubmittedQuestion(question)}
                placeholder={t("pages.visibleSurface.inputPreview.placeholder")}
                footerRight={footerRight}
              />
              <p className="mt-3 text-[14px] leading-7 text-zinc-300">
                {t("pages.visibleSurface.inputPreview.lastSubmitted")}
                <span className="ml-2 text-zinc-100">{submittedQuestion || t("pages.visibleSurface.inputPreview.noneSubmitted")}</span>
              </p>
            </div>

            <div className="rounded-[26px] border border-white/[0.12] bg-black/25 p-4">
              <div className="mb-3 text-[16px] font-semibold text-zinc-100">{t("pages.visibleSurface.progressPreview.title")}</div>
              <QueryProgressBars
                tasks={sampleTasks}
                selectedId={selectedTaskId}
                onSelect={setSelectedTaskId}
                onCancel={() => {}}
                onRemove={() => {}}
              />
            </div>
          </div>
        </section>

        <div className="grid gap-6 xl:grid-cols-2">
          <SectionCard
            id="light-answer"
            title={t("pages.visibleSurface.cards.light.title")}
            description={t("pages.visibleSurface.cards.light.description")}
          >
            <ResponseDisplay response={lightResponse} onAction={() => {}} onRetry={() => {}} />
          </SectionCard>

          <SectionCard
            id="deep-answer"
            title={t("pages.visibleSurface.cards.deep.title")}
            description={t("pages.visibleSurface.cards.deep.description")}
          >
            <ResponseDisplay response={deepResponse} onAction={() => {}} onRetry={() => {}} />
          </SectionCard>
        </div>

        <div className="grid gap-6 xl:grid-cols-2">
          <SectionCard
            id="dispatcher-guidance"
            title={t("pages.visibleSurface.cards.dispatcher.title")}
            description={t("pages.visibleSurface.cards.dispatcher.description")}
          >
            <PostAnswerGuidance
              response={dispatcherGuidanceResponse}
              onCompanionAction={() => {}}
            />
          </SectionCard>
        </div>
      </div>
    </div>
  );
}
