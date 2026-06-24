import type {
  AskRequest,
  AskResponse,
  ClarificationNeeded,
  CompanionHint,
  SearchCitation,
  SocraticStartResponse,
  SocraticRespondResponse,
  SocraticRevealResponse,
  HealthResponse,
  AuthResponse,
  UserProfile,
  HistoryItem,
  CognitiveSummaryResponse,
  CognitiveConsentResponse,
  DeleteCognitiveResponse,
  BehaviorSummaryResponse,
  GrowthDashboardResponse,
  CapabilityMapResponse,
  ImprovementPlanActionResponse,
  ModesResponse,
  QuotaResponse,
  RecentTurnsResponse,
  RoundtableResumeResponse,
  UsageResponse,
  ApiError,
} from "@/types";
import i18n from "@/i18n";
import { normalizeAppLanguage, readStoredLanguage, type AppLanguage } from "@/i18n/language";

// Dev must always stay same-origin via Vite proxy to eliminate CORS drift.
const RAW_API_BASE = (import.meta.env.VITE_API_BASE || "").trim();
const API_BASE = import.meta.env.DEV ? "/api" : (RAW_API_BASE || "/api");

const USER_KEY = "synthora_user";
const ROUNDTABLE_SSE_INACTIVITY_MS = 45000;

type QuestionConfirmationPayload = {
  original_question: string;
  optimized_question: string;
  changes_summary: string;
  needs_confirmation: boolean;
};

// Prevent concurrent 401→logout→reload race (审计 S1)
let _logoutInProgress = false;

type RoundtableStreamErrorPayload = {
  error?: string;
  code?: string;
  phase?: string;
  reason?: string;
  detail?: string;
};

function mapRoundtableStreamError(payload: unknown): string {
  const data = (payload && typeof payload === "object" ? payload : {}) as RoundtableStreamErrorPayload;
  const code = typeof data.code === "string" && data.code ? data.code : typeof data.error === "string" ? data.error : "";
  const phase = typeof data.phase === "string" ? data.phase : "";
  const reason = typeof data.reason === "string" ? data.reason : "";

  if (code === "roundtable_stream_error" || code === "roundtable_stream_inactivity" || code === "roundtable_stream_disconnected") {
    return i18n.t("api.client.errors.roundtable.connectionInterrupted");
  }
  if (code === "roundtable_s2_moderator_timeout") {
    return i18n.t("api.client.errors.roundtable.s2ModeratorTimedOut");
  }
  if (code === "roundtable_s4_moderator_timeout") {
    return i18n.t("api.client.errors.roundtable.s4ModeratorTimedOut");
  }
  if (code.includes("_upstream_model_timeout") || reason === "upstream_model_timeout_retry_exhausted") {
    return i18n.t("api.client.errors.roundtable.modelProcessingTimedOut", {
      phase: phase || i18n.t("api.client.errors.roundtable.currentStage"),
    });
  }
  if (code.includes("_upstream_model_failure") || reason === "upstream_model_retry_exhausted") {
    return i18n.t("api.client.errors.roundtable.modelProcessingFailed", {
      phase: phase || i18n.t("api.client.errors.roundtable.currentStage"),
    });
  }
  if (code === "roundtable_total_timeout") {
    return i18n.t("api.client.errors.roundtable.totalTimedOut");
  }
  if (code === "roundtable_processing_error") {
    return i18n.t("api.client.errors.roundtable.processingFailedLater");
  }
  if (typeof data.error === "string" && data.error.trim()) {
    return data.error;
  }
  return i18n.t("api.client.errors.roundtable.processingFailedRetry");
}

function mapSocraticStreamError(error: unknown): string {
  const raw = typeof error === "string" ? error.trim() : "";
  if (raw === "socratic_timeout") {
    return i18n.t("api.client.errors.socratic.preparationTimedOut");
  }
  if (raw === "socratic_pipeline_error") {
    return i18n.t("api.client.errors.socratic.preparationFailed");
  }
  if (raw) {
    return raw;
  }
  return i18n.t("api.client.errors.socratic.preparationFailed");
}

class ApiClient {
  isLoggedIn(): boolean {
    return !!this.getSavedUser();
  }

  async logout() {
    if (_logoutInProgress) return;
    _logoutInProgress = true;
    try {
      await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "include" });
    } catch { /* best effort */ }
    localStorage.removeItem(USER_KEY);
    _logoutInProgress = false;
  }

  saveUser(user: { username: string; display_name: string; is_admin: boolean }) {
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  }

  getSavedUser(): { username: string; display_name: string; is_admin: boolean } | null {
    const s = localStorage.getItem(USER_KEY);
    if (!s) return null;
    try {
      return JSON.parse(s);
    } catch {
      localStorage.removeItem(USER_KEY);
      return null;
    }
  }

  private _normalizeLanguage(raw?: string | null): "zh-CN" | "en-US" {
    return normalizeAppLanguage(raw);
  }

  private _resolveLanguage(): "zh-CN" | "en-US" {
    const runtimeLanguage = i18n.resolvedLanguage || i18n.language;
    if (runtimeLanguage) {
      return this._normalizeLanguage(runtimeLanguage);
    }

    const storedLanguage = readStoredLanguage();
    if (storedLanguage) {
      return storedLanguage;
    }

    const browserLanguage = typeof navigator !== "undefined" ? navigator.language : null;
    return this._normalizeLanguage(browserLanguage);
  }

  getCurrentLanguage(): "zh-CN" | "en-US" {
    return this._resolveLanguage();
  }

  private _authHeaders(headers?: HeadersInit): Headers {
    const authHeaders = new Headers(headers);
    authHeaders.set("Accept-Language", this._resolveLanguage());
    return authHeaders;
  }

  private _dispatchForceLogout(): void {
    window.dispatchEvent(new CustomEvent("synthora:force-logout"));
  }

  private _handleAuthFailure(
    status: number,
    errorCode?: string,
    options?: { skip?: boolean },
  ): void {
    if (options?.skip || !this.isLoggedIn() || _logoutInProgress) return;

    _logoutInProgress = true;
    if (status === 401) {
      this._dispatchForceLogout();
      _logoutInProgress = false;
      return;
    }
    if (status === 403 && errorCode === "AUTH_FORBIDDEN") {
      fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "include" }).catch(() => {});
      localStorage.removeItem(USER_KEY);
      this._dispatchForceLogout();
    }
    _logoutInProgress = false;
  }

  private async request<T>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const headers = this._authHeaders(options.headers as HeadersInit | undefined);
    const hasBody = options.body !== undefined && options.body !== null;
    const isFormDataBody = typeof FormData !== "undefined" && options.body instanceof FormData;
    const controller = !isFormDataBody ? new AbortController() : null;
    let timeoutId: number | null = null;
    let abortListener: (() => void) | null = null;
    if (hasBody && !isFormDataBody && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    if (controller) {
      timeoutId = window.setTimeout(() => controller.abort(), 30000);
      if (options.signal) {
        abortListener = () => controller.abort();
        if (options.signal.aborted) {
          controller.abort();
        } else {
          options.signal.addEventListener("abort", abortListener, { once: true });
        }
      }
    }

    let res: Response;
    try {
      res = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers,
        credentials: "include",
        signal: controller ? controller.signal : options.signal,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        throw {
          error_code: "TIMEOUT",
          detail: i18n.t("api.client.errors.requestTimedOut"),
        } satisfies ApiError;
      }
      throw err;
    } finally {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      if (options.signal && abortListener) {
        options.signal.removeEventListener("abort", abortListener);
      }
    }

    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const err: ApiError = (
        body && typeof body === "object"
          ? { ...(body as Record<string, unknown>), status: res.status }
          : {
              error_code: "UNKNOWN",
              detail: `HTTP ${res.status}`,
              status: res.status,
            }
      ) as ApiError;
      // Auto-logout on auth failure (token expired/revoked)
      // Never trigger on /auth/ paths — avoids infinite reload loop
      // F-06: dispatch event instead of full page reload so React can handle navigation
      this._handleAuthFailure(res.status, err.error_code, { skip: path.startsWith("/auth") });
      throw err;
    }

    return res.json();
  }

  // ── File Upload (v3.2: multimodal) ──

  async upload(file: File): Promise<{
    file_id: string;
    filename: string;
    content_type: string;
    size_bytes: number;
    is_image: boolean;
  }> {
    const formData = new FormData();
    formData.append("file", file);
    const headers = this._authHeaders();
    const res = await fetch(`${API_BASE}/upload`, {
      method: "POST",
      headers,
      body: formData,
      credentials: "include",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      this._handleAuthFailure(res.status, body?.error_code);
      throw new Error(body?.detail || `Upload failed: ${res.status}`);
    }
    return res.json();
  }

  // ── Auth ──

  async register(username: string, password: string, displayName?: string): Promise<AuthResponse> {
    return this.request<AuthResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name: displayName || username }),
    });
  }

  async login(usernameOrPhone: string, password: string): Promise<AuthResponse> {
    const isPhone = /^1[3-9]\d{9}$/.test(usernameOrPhone);
    return this.request<AuthResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify(
        isPhone
          ? { phone: usernameOrPhone, password, username: "" }
          : { username: usernameOrPhone, password, phone: "" }
      ),
    });
  }

  async me(): Promise<UserProfile> {
    return this.request("/auth/me");
  }

  async setLanguage(language: AppLanguage): Promise<{ language: AppLanguage }> {
    return this.request("/profile/language", {
      method: "PUT",
      body: JSON.stringify({ language }),
    });
  }

  async getQuota(): Promise<QuotaResponse> {
    return this.request("/quota");
  }

  // ── Health ──

  async health(): Promise<HealthResponse> {
    return this.request("/health");
  }

  // ── Modes (single source of truth for mode metadata) ──

  async modes(): Promise<ModesResponse> {
    return this.request("/modes");
  }

  // ── Query ──

  async ask(req: AskRequest): Promise<AskResponse> {
    const body = { ...req, locale: req.locale ?? this._resolveLanguage() };
    return this.request("/ask", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  async askStream(
    req: AskRequest,
    callbacks: {
      onCompanionHint?: (hint: CompanionHint) => void;
      onCompanionRoute?: (data: { message: string; actions: { label: string; capability_label?: string; model_label?: string; action_type: string; action_payload?: Record<string, unknown>; estimated_seconds?: number }[]; more_actions?: { label: string; capability_label?: string; model_label?: string; action_type: string; action_payload?: Record<string, unknown>; estimated_seconds?: number }[]; route_reason: string; auto_execute_seconds: number; is_silent: boolean; resolved_mode?: string; contributor_count?: number }) => void;
      onClarificationNeeded?: (data: ClarificationNeeded) => void;
      onStageStart?: (stage: string, detail: string) => void;
      onContributor?: (modelId: string, success: boolean, latencyMs: number) => void;
      onPreview?: (modelId: string, content: string) => void;
      onToken?: (token: string) => void;
      onStageComplete?: (stage: string, detail: string) => void;
      onDraftAnswer?: (stage: string, modelId: string, content: string) => void;
      onCitationsReady?: (citations: SearchCitation[]) => void;
      onQuestionConfirmation?: (data: { original_question: string; optimized_question: string; changes_summary: string; needs_confirmation: boolean }) => void;
      onComplete?: (result: AskResponse) => void;
      onError?: (error: string) => void;
    },
    signal?: AbortSignal,
  ): Promise<void> {
    const emitFromFallback = (fallback: AskResponse) => {
      const preflight = fallback.preflight as Record<string, unknown> | null | undefined;
      const preflightType = typeof preflight?.type === "string" ? preflight.type : "";

      if (preflightType === "question_confirmation") {
        if (callbacks.onQuestionConfirmation) {
          callbacks.onQuestionConfirmation(preflight as unknown as QuestionConfirmationPayload);
        } else {
          callbacks.onError?.(i18n.t("api.client.errors.questionConfirmationRequired"));
        }
        return;
      }

      if (preflightType === "clarification_needed") {
        if (callbacks.onClarificationNeeded) {
          callbacks.onClarificationNeeded(preflight as ClarificationNeeded);
        } else {
          callbacks.onError?.(i18n.t("api.client.errors.clarificationRequired"));
        }
        return;
      }

      callbacks.onComplete?.(fallback);
    };

    let res: Response;
    try {
      const headers = this._authHeaders({ "Content-Type": "application/json" });
      const body = { ...req, locale: req.locale ?? this._resolveLanguage() };
      res = await fetch(`${API_BASE}/ask/stream`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal,
        credentials: "include",
      });
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === "AbortError") throw { error_code: "CANCELLED", detail: "Request cancelled" };
      // SSE fetch failed — fallback to regular /ask
      console.warn("SSE fetch failed, falling back to /ask");
      const fallback = await this.ask(req);
      emitFromFallback(fallback);
      return;
    }

    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const err: ApiError = body ?? { error_code: "UNKNOWN", detail: `HTTP ${res.status}` };
      // F-06: match normal branch — 401/403 AUTH_FORBIDDEN + React event
      this._handleAuthFailure(res.status, err.error_code);
      throw err;
    }

    const reader = res.body?.getReader();
    if (!reader) {
      // No streaming support — fallback to regular /ask
      const fallback = await this.ask(req);
      emitFromFallback(fallback);
      return;
    }

    const decoder = new TextDecoder();
    let buffer = "";
    let gotComplete = false;
    let sawProgress = false;

    // E1: Inactivity timeout — if tokens were flowing but stop for 120s
    // (heartbeats keep connection alive but no meaningful content arrives),
    // force-terminate to prevent infinite "生成中" state.
    const INACTIVITY_TIMEOUT_MS = 120_000;
    let lastMeaningfulEventAt = Date.now();
    const inactivityTimer = setInterval(() => {
      if (sawProgress && !gotComplete && Date.now() - lastMeaningfulEventAt > INACTIVITY_TIMEOUT_MS) {
        clearInterval(inactivityTimer);
        reader?.cancel();
        callbacks.onError?.(i18n.t("api.client.errors.answerGenerationTimedOut"));
      }
    }, 10_000);

    // SSE parser: accumulate multi-line data fields per spec
    // An empty line (\n\n) terminates an event
    let currentEvent = "";
    let dataLines: string[] = [];

    const dispatchEvent = () => {
      if (!currentEvent || dataLines.length === 0) {
        currentEvent = "";
        dataLines = [];
        return;
      }
      const data = dataLines.join("\n");
      dataLines = [];
      const evt = currentEvent;
      currentEvent = "";

      try {
        switch (evt) {
          case "stage_start": {
            const d = JSON.parse(data);
            // G2: Stage events are genuine progress — prevent inactivity timeout during active processing
            lastMeaningfulEventAt = Date.now();
            callbacks.onStageStart?.(d.stage, d.detail);
            break;
          }
          case "contributor": {
            const d = JSON.parse(data);
            lastMeaningfulEventAt = Date.now();
            callbacks.onContributor?.(d.model_id, d.success, d.latency_ms);
            break;
          }
          case "preview": {
            const d = JSON.parse(data);
            callbacks.onPreview?.(d.model_id, d.content);
            if (typeof d.content === "string" && d.content.trim().length > 0) {
              sawProgress = true;
              lastMeaningfulEventAt = Date.now();
            }
            break;
          }
          case "token":
            if (data.trim().length > 0) { sawProgress = true; lastMeaningfulEventAt = Date.now(); }
            callbacks.onToken?.(data);
            break;
          case "stage_complete": {
            const d = JSON.parse(data);
            lastMeaningfulEventAt = Date.now();
            callbacks.onStageComplete?.(d.stage, d.detail);
            break;
          }
          case "draft_answer": {
            const d = JSON.parse(data);
            callbacks.onDraftAnswer?.(d.stage, d.model_id, d.content);
            if (typeof d.content === "string" && d.content.trim().length > 0) {
              sawProgress = true;
              lastMeaningfulEventAt = Date.now();
            }
            break;
          }
          case "citations": {
            const d = JSON.parse(data);
            callbacks.onCitationsReady?.(d.citations ?? []);
            break;
          }
          case "complete": {
            const d = JSON.parse(data) as AskResponse;
            // BUG-2 fix: always mark terminal on complete event regardless of final_answer content.
            // Empty final_answer is handled by UI (show preview + warning), not by re-triggering fallback.
            gotComplete = true;
            clearInterval(inactivityTimer);
            callbacks.onComplete?.(d);
            break;
          }
          case "companion_hint": {
            const d = JSON.parse(data);
            callbacks.onCompanionHint?.(d as CompanionHint);
            break;
          }
          case "companion_route": {
            const d = JSON.parse(data) as { message: string; actions: { label: string; capability_label?: string; model_label?: string; action_type: string; action_payload?: Record<string, unknown>; estimated_seconds?: number }[]; more_actions?: { label: string; capability_label?: string; model_label?: string; action_type: string; action_payload?: Record<string, unknown>; estimated_seconds?: number }[]; route_reason: string; auto_execute_seconds: number; is_silent: boolean; resolved_mode?: string; contributor_count?: number };
            lastMeaningfulEventAt = Date.now();
            callbacks.onCompanionRoute?.(d);
            break;
          }
          case "question_confirmation": {
            const d = JSON.parse(data);
            callbacks.onQuestionConfirmation?.(d);
            gotComplete = true;
            break;
          }
          case "error": {
            const d = JSON.parse(data);
            callbacks.onError?.(d.error);
            // Gate 4: error is also a terminal event — no fallback needed
            gotComplete = true;
            clearInterval(inactivityTimer);
            break;
          }
        }
      } catch (parseErr) {
        // BUG-2 fix: for terminal events, parsing failure must still mark gotComplete to prevent fallback
        if (evt === "complete" || evt === "error") {
          gotComplete = true;
          callbacks.onError?.(i18n.t("api.client.errors.parseFailed"));
        }
        console.warn(`[SSE] Failed to parse ${evt} event:`, data, parseErr);
      }
    };

    const processSseLine = (rawLine: string) => {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;

      if (line === "") {
        // Empty line = event boundary per SSE spec
        dispatchEvent();
      } else if (line.startsWith(":")) {
        // SSE comments (: ping ...) are silently ignored
        return;
      } else if (line.startsWith("event: ")) {
        // New event type — dispatch any pending event first
        if (dataLines.length > 0) dispatchEvent();
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith("event:")) {
        if (dataLines.length > 0) dispatchEvent();
        currentEvent = line.slice(6).trim();
      } else if (line.startsWith("data: ")) {
        dataLines.push(line.slice(6));
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5));
      }
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          if (buffer) {
            const tailLines = buffer.split("\n");
            for (const line of tailLines) processSseLine(line);
            buffer = "";
          }
          dispatchEvent();
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          processSseLine(line);
        }
      }
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === "AbortError") {
        clearInterval(inactivityTimer);
        reader.cancel();
        return; // User cancelled — silent, no error
      }
      clearInterval(inactivityTimer);
      // Stream read error — fallback if no complete yet and not aborted
      if (!gotComplete && !signal?.aborted) {
        if (sawProgress) {
          callbacks.onError?.(i18n.t("api.client.errors.streamInterruptedKeptContent"));
        } else {
          console.warn("SSE read error, falling back to /ask");
          try {
            const fallback = await this.ask(req);
            emitFromFallback(fallback);
          } catch (fallbackErr) {
            callbacks.onError?.("Stream failed and fallback also failed");
          }
        }
      }
      return;
    }

    clearInterval(inactivityTimer);
    // Stream ended but no complete event — fallback (only if not aborted)
    if (!gotComplete && !signal?.aborted) {
      if (sawProgress) {
        callbacks.onError?.(i18n.t("api.client.errors.streamInterruptedKeptContent"));
      } else {
        console.warn("SSE ended without complete event, falling back to /ask");
        try {
          const fallback = await this.ask(req);
          emitFromFallback(fallback);
        } catch {
          callbacks.onError?.(i18n.t("api.client.errors.responseTimedOut"));
        }
      }
    }
  }

  // ── Cognitive Profile ──

  async cognitiveSummary(): Promise<CognitiveSummaryResponse> {
    return this.request("/profile/cognitive-summary");
  }

  async cognitiveConsent(consent: boolean): Promise<CognitiveConsentResponse> {
    return this.request(`/profile/cognitive-consent?consent=${consent}`, { method: "POST" });
  }

  async cognitiveDelete(): Promise<DeleteCognitiveResponse> {
    return this.request("/profile/delete-cognitive?confirm=true", { method: "POST" });
  }

  // ── CBA Behavior Analytics (ADR-014) ──

  async behaviorSummary(): Promise<BehaviorSummaryResponse> {
    return this.request("/profile/behavior-summary");
  }

  // ── Growth Dashboard (P1-1, 原则#19) ──

  async growthDashboard(): Promise<GrowthDashboardResponse> {
    return this.request("/profile/growth");
  }

  // ── Capability Map & Improvement Plans ──

  async capabilityMap(): Promise<CapabilityMapResponse> {
    return this.request("/capability-map");
  }

  async activatePlan(planId: string): Promise<ImprovementPlanActionResponse> {
    return this.request(`/improvement-plans/${planId}/activate`, { method: "POST" });
  }

  async abandonPlan(planId: string): Promise<ImprovementPlanActionResponse> {
    return this.request(`/improvement-plans/${planId}/abandon`, { method: "POST" });
  }

  // ── Profile: Export & Usage (P2-8) ──

  async profileExport(): Promise<Blob> {
    const res = await fetch(`${API_BASE}/profile/export`, {
      credentials: "include",
      headers: this._authHeaders(),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      this._handleAuthFailure(res.status, body?.error_code);
      throw { error_code: "EXPORT_FAILED", detail: `HTTP ${res.status}` } as ApiError;
    }
    return res.blob();
  }

  async profileUsage(): Promise<UsageResponse> {
    return this.request("/profile/usage");
  }

  async deleteAccount(password: string): Promise<{ status: string; message: string }> {
    return this.request("/auth/account?confirm=DELETE", {
      method: "DELETE",
      body: JSON.stringify({ password }),
    });
  }

  async submitFeedback(data: {
    query_id: string;
    vote: string;
    mode: string;
    quality_gate: string;
  }): Promise<{ status: string }> {
    return this.request("/feedback", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async recentTurns(): Promise<RecentTurnsResponse> {
    return this.request("/profile/recent-turns");
  }

  // ── Socratic ──

  /**
   * Option C: SSE streaming Socratic start.
   * Yields progressive events instead of blocking for 60-120s.
   */
  async socraticStartStream(
    question: string,
    callbacks: {
      onStage?: (stage: string, detail: string) => void;
      onContributor?: (data: { model_id: string; success: boolean; latency_ms: number; done_count: number; total_count: number }) => void;
      onDivergence?: (data: { consensus_points: string[]; divergence_count: number; overall_consensus: number }) => void;
      onReady?: (data: SocraticStartResponse & { session_id: string }) => void;
      onError?: (error: string) => void;
    },
    signal?: AbortSignal,
  ): Promise<void> {
    const url = `${API_BASE}/socratic/start/stream`;
    const res = await fetch(url, {
      method: "POST",
      headers: this._authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ question, locale: this._resolveLanguage() }),
      credentials: "include",
      signal,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      callbacks.onError?.(mapSocraticStreamError(text || `HTTP ${res.status}`));
      return;
    }

    const reader = res.body?.getReader();
    if (!reader) {
      callbacks.onError?.(i18n.t("api.client.errors.noResponseBody"));
      return;
    }

    const decoder = new TextDecoder();
    let buffer = "";

    // SSE parser — exact same pattern as askStream (proven working in browser)
    let currentEvent = "";
    let dataLines: string[] = [];
    let receivedTerminal = false;  // v4.32: track if we got socratic_ready or socratic_error

    const dispatchEvent = () => {
      if (!currentEvent || dataLines.length === 0) {
        currentEvent = "";
        dataLines = [];
        return;
      }
      const data = dataLines.join("\n");
      dataLines = [];
      const evt = currentEvent;
      currentEvent = "";

      try {
        const parsed = JSON.parse(data);
        switch (evt) {
          case "socratic_stage":
            callbacks.onStage?.(parsed.stage, parsed.detail);
            break;
          case "socratic_contributor":
            callbacks.onContributor?.(parsed);
            break;
          case "socratic_divergence":
            callbacks.onDivergence?.(parsed);
            break;
          case "socratic_ready":
            receivedTerminal = true;
            callbacks.onReady?.({
              session_id: parsed.session_id,
              initial_guide: parsed.initial_guide,
              max_guide_rounds: parsed.max_guide_rounds,
              divergence_map: parsed.divergence_map,
              phase1_latency_ms: parsed.phase1_latency_ms,
            });
            break;
          case "socratic_error":
            receivedTerminal = true;
            callbacks.onError?.(mapSocraticStreamError(parsed.error));
            break;
          case "heartbeat":
            break;
        }
      } catch (parseErr) {
        // v4.32: log parse errors; treat terminal event parse failure as error
        console.warn(`[SSE] Failed to parse ${evt} event:`, data, parseErr);
        if (evt === "socratic_ready" || evt === "socratic_error") {
          receivedTerminal = true;
          callbacks.onError?.(i18n.t("api.client.errors.parseFailed"));
        }
      }
    };

    const processSseLine = (rawLine: string) => {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (line === "") {
        dispatchEvent();
      } else if (line.startsWith(":")) {
        return;
      } else if (line.startsWith("event: ")) {
        if (dataLines.length > 0) dispatchEvent();
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith("event:")) {
        if (dataLines.length > 0) dispatchEvent();
        currentEvent = line.slice(6).trim();
      } else if (line.startsWith("data: ")) {
        dataLines.push(line.slice(6));
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5));
      }
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          if (buffer) {
            const tailLines = buffer.split("\n");
            for (const line of tailLines) processSseLine(line);
            buffer = "";
          }
          dispatchEvent();
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          processSseLine(line);
        }
      }
      // v4.32: stream ended without terminal event — connection dropped
      if (!receivedTerminal) {
        callbacks.onError?.(i18n.t("api.client.errors.connectionClosedUnexpectedly"));
      }
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === "AbortError") {
        reader.cancel();
        return;
      }
      callbacks.onError?.(mapSocraticStreamError((e as Error).message || ""));
    }
  }

  async socraticRespond(
    sessionId: string,
    message: string,
  ): Promise<SocraticRespondResponse> {
    return this.request("/socratic/respond", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, message }),
    });
  }

  async socraticReveal(
    sessionId: string,
  ): Promise<SocraticRevealResponse> {
    return this.request("/socratic/reveal", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
  }

  async roundtableResume(sessionId: string): Promise<RoundtableResumeResponse> {
    return this.request(`/roundtable/${sessionId}/resume`);
  }

  async roundtableCheck(question: string): Promise<{ suitability: string; reason: string }> {
    try {
      const res = await fetch(`${API_BASE}/roundtable/check`, {
        method: "POST",
        headers: this._authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ question, locale: this._resolveLanguage() }),
        credentials: "include",
      });
      if (!res.ok) {
        return { suitability: "unknown", reason: "" };
      }
      return res.json();
    } catch {
      return { suitability: "unknown", reason: "" };
    }
  }

  async roundtableChoice(
    sessionId: string,
    choicePoint: string,
    action: string,
    userInput?: string | null,
    idempotencyKey?: string,
  ): Promise<{ ok: boolean }> {
    const headers: Record<string, string> = {};
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    return this.request(`/roundtable/${sessionId}/choice`, {
      method: "POST",
      body: JSON.stringify({
        choice_point: choicePoint,
        action,
        user_input: userInput ?? null,
      }),
      headers,
    });
  }

  async roundtableStream(
    req: { question: string; session_id?: string; locale?: "zh-CN" | "en-US" },
    callbacks: {
      onStarted?: (d: { session_id: string; expert_count: number; question: string }) => void;
      onExpertDone?: (d: {
        model_id: string; label: string; stance: string; confidence: number;
        my_dimensions: string[]; claims: { point: string; evidence: string; dimension: string }[];
        risk_warning: string; blind_spot_warning: string; challenge_to_others: string;
        raw_response: string; structured: boolean; success: boolean; error: string;
        latency_ms: number; done_count: number; total_count: number;
      }) => void;
      onDisputesMapped?: (d: {
        synthesized_dimensions: string[];
        dimension_sources: Record<string, string[]>;
        contention_points: {
          topic: string; severity: string; dispute_type: string[];
          factual_aspect: string; value_aspect: string;
          dimension_id: string; dimension_label: string; dimension_aliases: string[];
          adjudication_note: string;
          sides: { position: string; supporting_claims: string[]; lead_expert: string; main_argument: string }[];
          why_it_matters: string; suggested_focus: boolean;
        }[];
        consensus_points: { point: string; strength: string; agreed_by: string[] }[];
        suggested_focus: string; echo_chamber_warning: string; clarifying_questions: string[];
      }) => void;
      onAwaitingUserChoice?: (d: { choice_point: string; timeout_s: number; default_action: string }) => void;
      onDebateStarted?: (d: { round: number; assignments: Record<string, string> }) => void;
      onRebuttalDone?: (d: {
        model_id: string; label: string; role: string; target_dispute: string; response_type: string;
        response: string; new_evidence: string; revised_stance: string; stance_changed: boolean;
        confidence: number; structured: boolean; success: boolean; latency_ms: number;
        done_count: number; total_count: number;
      }) => void;
      onDebateComplete?: (d: { round: number; stance_changes: { expert: string; changed: boolean; revised_stance: string }[] }) => void;
      onModeratorStarted?: () => void;
      onComplete?: (d: {
        session_id: string; question: string; rounds_completed: number;
        experts: {
          model_id: string; label: string; stance: string; confidence: number;
          my_dimensions: string[]; challenge_to_others: string;
          claims: { point: string; evidence: string; dimension: string }[];
          risk_warning: string; blind_spot_warning: string; structured: boolean;
          raw_response: string; latency_ms: number; success: boolean; error: string;
        }[];
        dispute_map: {
          synthesized_dimensions: string[]; dimension_sources: Record<string, string[]>;
          contention_points: {
            topic: string; severity: string; dispute_type: string[];
            factual_aspect: string; value_aspect: string;
            dimension_id: string; dimension_label: string; dimension_aliases: string[];
            adjudication_note: string; why_it_matters: string; suggested_focus: boolean;
            sides: { position: string; lead_expert: string; main_argument: string; supporting_claims: string[] }[];
          }[];
          consensus_points: { point: string; strength: string; agreed_by: string[] }[];
          suggested_focus: string; echo_chamber_warning: string; clarifying_questions: string[];
        };
        decision_packet: {
          conclusion_type: string; confidence_basis: string; final_summary: string;
          stance_evolution: { expert: string; r1_stance: string; final_stance: string; changed: boolean; changed_reason: string }[];
          options: { choice: string; pros: string[]; cons: string[]; best_when: string; risk: string; mitigation: string }[];
          unresolved: { point: string; reason: string; how_to_resolve: string }[];
          what_changes_my_mind: string; recommended_action: string;
          value_disputes_to_user: { point: string; dimension_id: string; ask_user: string }[];
          echo_chamber_flag: boolean; degraded: boolean; degradation_reason: string;
          total_latency_ms: number; estimated_cost_usd: number;
        };
      }) => void;
      onAutoDraft?: (d: {
        decision_packet: {
          conclusion_type: string; confidence_basis: string; final_summary: string;
          stance_evolution: { expert: string; r1_stance: string; final_stance: string; changed: boolean; changed_reason: string }[];
          options: { choice: string; pros: string[]; cons: string[]; best_when: string; risk: string; mitigation: string }[];
          unresolved: { point: string; reason: string; how_to_resolve: string }[];
          what_changes_my_mind: string; recommended_action: string;
          value_disputes_to_user: { point: string; dimension_id: string; ask_user: string }[];
          echo_chamber_flag: boolean; degraded: boolean; degradation_reason: string;
          total_latency_ms: number; estimated_cost_usd: number;
        };
        message: string;
      }) => void;
      onError?: (msg: string) => void;
    },
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await fetch(`${API_BASE}/roundtable/start`, {
      method: "POST",
      headers: this._authHeaders({ "Content-Type": "application/json", "Accept": "text/event-stream" }),
      credentials: "include",
      body: JSON.stringify({ ...req, locale: req.locale ?? this._resolveLanguage() }),
      signal,
    });
    if (!res.ok || !res.body) {
      const body = await res.json().catch(() => ({}));
      callbacks.onError?.((body as { detail?: string }).detail || `HTTP ${res.status}`);
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let currentEvent = "";
    const dataLines: string[] = [];

    let receivedTerminal = false;
    let lastEventAt = Date.now();
    let streamInterrupted = false;
    const inactivityTimer = window.setInterval(() => {
      if (receivedTerminal || signal?.aborted) {
        window.clearInterval(inactivityTimer);
        return;
      }
      if (Date.now() - lastEventAt > ROUNDTABLE_SSE_INACTIVITY_MS) {
        streamInterrupted = true;
        receivedTerminal = true;
        window.clearInterval(inactivityTimer);
        reader.cancel().catch(() => {});
        callbacks.onError?.(i18n.t("api.client.errors.roundtable.connectionInterrupted"));
      }
    }, 1000);

    const dispatch = () => {
      if (!currentEvent && dataLines.length === 0) return;
      const data = dataLines.join("\n");
      dataLines.length = 0;
      const evt = currentEvent;
      currentEvent = "";
      try {
        const parsed = JSON.parse(data);
        lastEventAt = Date.now();
        switch (evt) {
          case "roundtable_started": callbacks.onStarted?.(parsed); break;
          case "expert_done": callbacks.onExpertDone?.(parsed); break;
          case "disputes_mapped": callbacks.onDisputesMapped?.(parsed); break;
          case "awaiting_user_choice": callbacks.onAwaitingUserChoice?.(parsed); break;
          case "debate_started": callbacks.onDebateStarted?.(parsed); break;
          case "rebuttal_done": callbacks.onRebuttalDone?.(parsed); break;
          case "debate_complete": callbacks.onDebateComplete?.(parsed); break;
          case "moderator_started": callbacks.onModeratorStarted?.(); break;
          case "roundtable_complete": receivedTerminal = true; callbacks.onComplete?.(parsed); break;
          case "roundtable_error": receivedTerminal = true; callbacks.onError?.(mapRoundtableStreamError(parsed)); break;
          case "auto_draft": receivedTerminal = true; callbacks.onAutoDraft?.(parsed); break;
          case "heartbeat": break;
        }
      } catch {
        if (["roundtable_complete", "roundtable_error", "auto_draft"].includes(evt)) {
          receivedTerminal = true;
        }
        callbacks.onError?.(i18n.t("api.client.errors.roundtable.responseParseFailed"));
      }
    };

    while (true) {
      const { done, value } = await reader.read().catch(() => ({ done: true as const, value: undefined }));
      if (done) {
        window.clearInterval(inactivityTimer);
        dispatch();
        if (!receivedTerminal && !signal?.aborted) {
          if (!streamInterrupted) {
            callbacks.onError?.(i18n.t("api.client.errors.roundtable.connectionInterrupted"));
          }
        }
        break;
      }
      lastEventAt = Date.now();
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line.startsWith("event:")) { dispatch(); currentEvent = line.slice(6).trim(); }
        else if (line.startsWith("data:")) { dataLines.push(line.slice(5).trim()); }
        else if (line === "") { dispatch(); }
      }
    }
    window.clearInterval(inactivityTimer);
  }
  // ── Roundtable v2.2.2 ──

  async history(limit = 20, offset = 0): Promise<{ history: HistoryItem[]; total: number }> {
    return this.request(`/history?limit=${limit}&offset=${offset}`);
  }
}

export { ApiClient };
export const api = new ApiClient();
