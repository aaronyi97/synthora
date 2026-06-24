import { useState, useEffect, useCallback, useRef } from 'react';
import type { AskResponse } from '@/types';
import type { HistoryItem } from '@/types';
import { api } from '@/api/client';
import i18n from '@/i18n';

export interface Message {
  role: 'user' | 'assistant';
  content: string;
  response?: AskResponse;
  timestamp: number;
}

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  sessionId: string | null;
  turnCount: number;
  createdAt: number;
  updatedAt: number;
}

const STORAGE_KEY = 'synthora_conversations';
const STORAGE_TS_KEY = 'synthora_conversations_ts';
const MAX_CONVERSATIONS = 50;
const MAX_MESSAGES_PER_CONVERSATION = 20;
const CONVERSATION_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function historyConvId(item: HistoryItem): string {
  return item.session_id ? `sess-${item.session_id}` : `hist-${item.query_id}`;
}

function canonicalConversationId(id: string, sessionId: string | null | undefined): string {
  return sessionId ? `sess-${sessionId}` : id;
}

function messageKey(msg: Message): string {
  return `${msg.role}|${msg.timestamp}|${msg.content}`;
}

function mergeMessages(left: Message[], right: Message[]): Message[] {
  const merged = [...left, ...right].sort((a, b) => a.timestamp - b.timestamp);
  const seen = new Set<string>();
  const out: Message[] = [];
  for (const msg of merged) {
    const key = messageKey(msg);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(msg);
  }
  return out;
}

function mergeConversation(base: Conversation, incoming: Conversation): Conversation {
  const messages = mergeMessages(base.messages, incoming.messages).slice(-MAX_MESSAGES_PER_CONVERSATION);
  const firstUser = messages.find((m) => m.role === "user");
  return {
    ...base,
    id: canonicalConversationId(base.id, incoming.sessionId ?? base.sessionId),
    title: firstUser ? generateTitle(firstUser.content) : (base.title || incoming.title || i18n.t("hooks.useConversations.newConversationTitle")),
    messages,
    sessionId: incoming.sessionId ?? base.sessionId ?? null,
    turnCount: Math.max(base.turnCount, incoming.turnCount, Math.ceil(messages.length / 2)),
    createdAt: Math.min(base.createdAt, incoming.createdAt),
    updatedAt: Math.max(base.updatedAt, incoming.updatedAt),
  };
}

function trimConversation(conv: Conversation): Conversation {
  const messages = [...conv.messages]
    .sort((a, b) => a.timestamp - b.timestamp)
    .slice(-MAX_MESSAGES_PER_CONVERSATION);
  const firstUser = messages.find((m) => m.role === "user");
  return {
    ...conv,
    title: firstUser ? generateTitle(firstUser.content) : conv.title,
    messages,
    turnCount: Math.max(conv.turnCount, Math.ceil(messages.length / 2)),
  };
}

function normalizeConversations(convs: Conversation[]): Conversation[] {
  const byId = new Map<string, Conversation>();
  for (const conv of convs) {
    const canonicalId = canonicalConversationId(conv.id, conv.sessionId);
    const normalized = trimConversation({
      ...conv,
      id: canonicalId,
      sessionId: conv.sessionId ?? null,
    });
    const existing = byId.get(canonicalId);
    if (existing) {
      byId.set(canonicalId, mergeConversation(existing, normalized));
    } else {
      byId.set(canonicalId, normalized);
    }
  }
  return Array.from(byId.values())
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_CONVERSATIONS);
}

function loadConversations(): Conversation[] {
  try {
    const tsRaw = localStorage.getItem(STORAGE_TS_KEY);
    const ts = tsRaw ? Number(tsRaw) : Date.now();
    if (!Number.isFinite(ts) || Date.now() - ts > CONVERSATION_TTL_MS) {
      localStorage.removeItem(STORAGE_KEY);
      localStorage.removeItem(STORAGE_TS_KEY);
      return [];
    }
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) return [];
    const parsed = JSON.parse(stored);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveConversations(convs: Conversation[]): void {
  try {
    const trimmed = convs
      .slice(0, MAX_CONVERSATIONS)
      .map(trimConversation);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
    localStorage.setItem(STORAGE_TS_KEY, Date.now().toString());
  } catch (e) {
    console.error('Failed to save conversations:', e);
  }
}

function generateTitle(question: string): string {
  const cleaned = question.trim().slice(0, 50);
  return cleaned.length < question.trim().length ? cleaned + '...' : cleaned;
}

function toTs(iso: string): number {
  const ts = new Date(iso).getTime();
  return Number.isFinite(ts) ? ts : Date.now();
}

function buildHistoryResponse(item: HistoryItem, displayAnswer?: string): AskResponse {
  return {
    query_id: item.query_id,
    question: item.question,
    mode: item.mode,
    final_answer: displayAnswer ?? item.final_answer,
    confidence: item.confidence ?? 0,
    quality_gate: item.quality_gate ?? 'unknown',
    has_divergence: item.has_divergence ?? false,
    divergence_summary: item.divergence_summary ?? null,
    key_insights: item.key_insights ?? [],
    latency_ms: item.latency_ms ?? 0,
    estimated_cost_usd: item.estimated_cost_usd ?? 0,
    contributor_count: item.contributor_count ?? 0,
    individual_responses: null,
    companion_hint: null,
    preflight: null,
    pipeline_started: true,
    fast_path: false,
    low_confidence_actions: [],
    session_id: item.session_id ?? null,
    context_compressed: false,
    search_citations: [],
    draft_answers: [],
    divergence_points: item.divergence_points ?? [],
    consensus_points: [],
    fact_warnings: [],
    next_steps: null,
    consensus_type: "unknown",
    reason_code: item.quality_gate === "low_confidence" ? "low_confidence" : "standard",
    ai_disclosure: i18n.t("common.app.aiDisclosure"),
  };
}

function buildHistoryAssistantMessage(item: HistoryItem, displayAnswer?: string): Message {
  const ts = toTs(item.created_at);
  return {
    role: 'assistant',
    content: displayAnswer ?? item.final_answer,
    response: buildHistoryResponse(item, displayAnswer),
    timestamp: ts + 1,
  };
}

function createConversationFromHistoryItem(item: HistoryItem): Conversation {
  const ts = toTs(item.created_at);
  return {
    id: historyConvId(item),
    title: generateTitle(item.question),
    messages: [
      { role: 'user', content: item.question, timestamp: ts },
      buildHistoryAssistantMessage(item),
    ],
    sessionId: item.session_id ?? null,
    turnCount: 1,
    createdAt: ts,
    updatedAt: ts + 1,
  };
}

function findNextUserIndex(messages: Message[], start: number): number {
  for (let i = start + 1; i < messages.length; i++) {
    if (messages[i].role === "user") return i;
  }
  return -1;
}

function findConversationIndexForHistoryItem(
  conversations: Conversation[],
  item: HistoryItem,
  canonicalId: string,
): number {
  const exact = conversations.findIndex((conv) =>
    conv.id === canonicalId
    || (!!item.session_id && conv.sessionId === item.session_id)
    || conv.messages.some((msg) => msg.role === "assistant" && msg.response?.query_id === item.query_id),
  );
  if (exact >= 0) return exact;

  for (let convIndex = 0; convIndex < conversations.length; convIndex++) {
    const messages = conversations[convIndex].messages;
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg.role !== "user" || msg.content !== item.question) continue;
      const nextUserIndex = findNextUserIndex(messages, i);
      const turnMessages = messages.slice(i + 1, nextUserIndex === -1 ? messages.length : nextUserIndex);
      const hasAssistant = turnMessages.some((m) => m.role === "assistant");
      if (!hasAssistant) return convIndex;
    }
  }

  return -1;
}

function mergeHistoryItemIntoConversation(
  conv: Conversation,
  item: HistoryItem,
): Conversation {
  const ts = toTs(item.created_at);
  const canonicalId = historyConvId(item);
  const assistantMsg = buildHistoryAssistantMessage(item);
  const queryId = item.query_id;
  const messages = [...conv.messages].sort((a, b) => a.timestamp - b.timestamp);
  let merged = false;

  const byQueryIdIndex = messages.findIndex(
    (msg) => msg.role === "assistant" && msg.response?.query_id === queryId,
  );
  if (byQueryIdIndex >= 0) {
    messages[byQueryIdIndex] = assistantMsg;
    merged = true;
  }

  if (!merged) {
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg.role !== "user" || msg.content !== item.question) continue;
      const nextUserIndex = findNextUserIndex(messages, i);
      let assistantIndex = -1;
      const turnEnd = nextUserIndex === -1 ? messages.length : nextUserIndex;
      for (let j = i + 1; j < turnEnd; j++) {
        if (messages[j].role === "assistant") {
          assistantIndex = j;
          break;
        }
      }
      if (assistantIndex >= 0) {
        messages[assistantIndex] = assistantMsg;
      } else {
        messages.splice(turnEnd, 0, assistantMsg);
      }
      merged = true;
      break;
    }
  }

  if (!merged) {
    messages.push({ role: 'user', content: item.question, timestamp: ts }, assistantMsg);
  }

  return trimConversation({
    ...conv,
    id: canonicalId,
    title: generateTitle(item.question),
    messages,
    sessionId: item.session_id ?? conv.sessionId ?? null,
    turnCount: Math.max(conv.turnCount, Math.ceil(messages.length / 2)),
    createdAt: Math.min(conv.createdAt, ts),
    updatedAt: Math.max(conv.updatedAt, ts + 1),
  });
}

function mergeHistoryItemsIntoConversations(
  base: Conversation[],
  items: HistoryItem[],
): { conversations: Conversation[]; idMap: Map<string, string> } {
  let conversations = normalizeConversations(base);
  const idMap = new Map<string, string>();
  const ordered = [...items].sort((a, b) => toTs(a.created_at) - toTs(b.created_at));

  for (const item of ordered) {
    const canonicalId = historyConvId(item);
    const existingIndex = findConversationIndexForHistoryItem(conversations, item, canonicalId);
    if (existingIndex >= 0) {
      const existing = conversations[existingIndex];
      const merged = mergeHistoryItemIntoConversation(existing, item);
      if (merged.id !== existing.id) {
        idMap.set(existing.id, merged.id);
      }
      conversations = normalizeConversations([
        ...conversations.slice(0, existingIndex),
        merged,
        ...conversations.slice(existingIndex + 1),
      ]);
      continue;
    }
    conversations = normalizeConversations([createConversationFromHistoryItem(item), ...conversations]);
  }

  return { conversations, idMap };
}

function resolveCurrentConversationId(currentId: string | null, idMap: Map<string, string>): string | null {
  if (!currentId) return currentId;
  let resolved = currentId;
  const seen = new Set<string>();
  while (idMap.has(resolved) && !seen.has(resolved)) {
    seen.add(resolved);
    resolved = idMap.get(resolved)!;
  }
  return resolved;
}

export function useConversations() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  // E2: Guard to prevent save effect from writing [] before initial load completes
  const loadedRef = useRef(false);
  const conversationsRef = useRef<Conversation[]>([]);
  const currentIdRef = useRef<string | null>(null);

  useEffect(() => {
    conversationsRef.current = conversations;
  }, [conversations]);

  useEffect(() => {
    currentIdRef.current = currentId;
  }, [currentId]);

  // Load on mount
  useEffect(() => {
    const loaded = normalizeConversations(loadConversations());
    setConversations(loaded);
    loadedRef.current = true;
    // Auto-select most recent if exists
    if (loaded.length > 0 && !currentId) {
      setCurrentId(loaded[0].id);
    }
  }, []);

  const syncHistory = useCallback(async (limit = 100, offset = 0) => {
    const res = await api.history(limit, offset);
    if (!res.history?.length) return res;
    const merged = mergeHistoryItemsIntoConversations(conversationsRef.current, res.history);
    const nextCurrentId = resolveCurrentConversationId(currentIdRef.current, merged.idMap);
    setConversations(merged.conversations);
    if (nextCurrentId !== currentIdRef.current) {
      setCurrentId(nextCurrentId);
    }
    return res;
  }, []);

  // Backfill: import server-side query history as readable local conversations.
  useEffect(() => {
    syncHistory().catch((e) => {
      console.warn("[useConversations] syncHistory failed:", e);
    });
  }, [syncHistory]);

  // Save on change — D5: persist empty array so deletions stick; E2: skip until initial load done
  useEffect(() => {
    if (!loadedRef.current) return;
    saveConversations(conversations);
  }, [conversations]);

  const current = conversations.find(c => c.id === currentId) || null;

  const createConversation = useCallback(() => {
    const newConv: Conversation = {
      id: Date.now().toString(),
      title: i18n.t("hooks.useConversations.newConversationTitle"),
      messages: [],
      sessionId: null,
      turnCount: 0,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    setConversations(prev => {
      const updated = [newConv, ...prev].slice(0, MAX_CONVERSATIONS);
      return updated;
    });
    setCurrentId(newConv.id);
    return newConv.id;
  }, []);

  const deleteConversation = useCallback((id: string) => {
    setConversations(prev => {
      const filtered = prev.filter(c => c.id !== id);
      if (currentId === id && filtered.length > 0) {
        setCurrentId(filtered[0].id);
      } else if (filtered.length === 0) {
        setCurrentId(null);
      }
      return filtered;
    });
  }, [currentId]);

  const switchConversation = useCallback((id: string) => {
    setCurrentId(id);
  }, []);

  const addMessage = useCallback((convId: string, message: Message) => {
    setConversations(prev => prev.map(c => {
      if (c.id !== convId) return c;
      const updated = {
        ...c,
        messages: [...c.messages, message],
        updatedAt: Date.now(),
      };
      // Auto-generate title from first user message
      if (message.role === 'user' && c.messages.length === 0) {
        updated.title = generateTitle(message.content);
      }
      return updated;
    }));
  }, []);

  const updateSession = useCallback((convId: string, sessionId: string, turnCount: number) => {
    setConversations((prev) => {
      const now = Date.now();
      const canonicalId = canonicalConversationId(convId, sessionId);
      const list = normalizeConversations(prev);
      const idx = list.findIndex((c) => c.id === convId);
      if (idx < 0) {
        return list.map((c) =>
          c.id === canonicalId ? { ...c, sessionId, turnCount: Math.max(c.turnCount, turnCount), updatedAt: now } : c,
        );
      }
      const updated: Conversation = {
        ...list[idx],
        id: canonicalId,
        sessionId,
        turnCount: Math.max(list[idx].turnCount, turnCount),
        updatedAt: now,
      };
      const rest = list.filter((_, i) => i !== idx);
      const existingIdx = rest.findIndex((c) => c.id === canonicalId);
      if (existingIdx >= 0) {
        rest[existingIdx] = mergeConversation(rest[existingIdx], updated);
      } else {
        rest.unshift(updated);
      }
      return normalizeConversations(rest);
    });
    setCurrentId((prev) => (prev === convId ? canonicalConversationId(convId, sessionId) : prev));
  }, []);

  const updateLastMessage = useCallback((convId: string, updates: Partial<Message>) => {
    setConversations(prev => prev.map(c => {
      if (c.id !== convId) return c;
      const messages = [...c.messages];
      if (messages.length > 0) {
        messages[messages.length - 1] = { ...messages[messages.length - 1], ...updates };
      }
      return { ...c, messages, updatedAt: Date.now() };
    }));
  }, []);

  const openHistoryAsConversation = useCallback((item: HistoryItem, answerOverride?: string) => {
    const ts = toTs(item.created_at);
    const convId = historyConvId(item);
    // Use answerOverride when user selected "best_single" tab; default to final_answer
    const displayAnswer = answerOverride ?? item.final_answer;
    const historyResponse = buildHistoryResponse(item, displayAnswer);
    const assistantMsg: Message = { role: 'assistant', content: displayAnswer, response: historyResponse, timestamp: ts + 1 };
    setConversations((prev) => {
      const list = normalizeConversations(prev);
      const exists = list.find((c) => c.id === convId);
      if (exists) {
        return normalizeConversations(list.map((c) => {
          if (c.id !== convId) return c;
          const hasUserMsg = c.messages.some((m) => m.role === 'user' && m.content === item.question);
          if (hasUserMsg) {
            // D1: Update assistant message matching query_id; if no match, replace the
            // assistant message immediately after the matching user message (fallback).
            const qidMatch = c.messages.some((m) => m.role === 'assistant' && m.response?.query_id === item.query_id);
            if (qidMatch) {
              const updatedMessages = c.messages.map((m) => {
                if (m.role === 'assistant' && m.response?.query_id === item.query_id) {
                  return { ...m, content: displayAnswer, response: historyResponse, timestamp: ts + 1 };
                }
                return m;
              });
              return { ...c, messages: updatedMessages, updatedAt: Date.now() };
            }
            // Fallback: query_id changed (e.g. aggregated→best_single) — replace nearest assistant after user msg
            const userIdx = c.messages.findIndex((m) => m.role === 'user' && m.content === item.question);
            const nextAssistantIdx = c.messages.findIndex((m, i) => i > userIdx && m.role === 'assistant');
            if (nextAssistantIdx >= 0) {
              const updatedMessages = [...c.messages];
              updatedMessages[nextAssistantIdx] = assistantMsg;
              return { ...c, messages: updatedMessages, updatedAt: Date.now() };
            }
            // No assistant after user msg — append
            return { ...c, messages: [...c.messages, assistantMsg], updatedAt: Date.now() };
          }
          const nextMessages = [
            ...c.messages,
            { role: 'user' as const, content: item.question, timestamp: ts },
            assistantMsg,
          ].sort((a, b) => a.timestamp - b.timestamp);
          return {
            ...c,
            messages: nextMessages,
            turnCount: Math.max(c.turnCount, Math.ceil(nextMessages.length / 2)),
            updatedAt: Date.now(),
          };
        }));
      }
      const conv: Conversation = {
        id: convId,
        title: generateTitle(item.question),
        messages: [
          { role: 'user', content: item.question, timestamp: ts },
          assistantMsg,
        ],
        sessionId: item.session_id ?? null,
        turnCount: 1,
        createdAt: ts,
        updatedAt: Date.now(),
      };
      return normalizeConversations([conv, ...list]);
    });
    setCurrentId(convId);
    return convId;
  }, []);

  return {
    conversations,
    current,
    currentId,
    syncHistory,
    createConversation,
    deleteConversation,
    switchConversation,
    addMessage,
    updateSession,
    updateLastMessage,
    openHistoryAsConversation,
  };
}

export const __testing = {
  buildHistoryResponse,
  mergeHistoryItemsIntoConversations,
  createConversationFromHistoryItem,
};
