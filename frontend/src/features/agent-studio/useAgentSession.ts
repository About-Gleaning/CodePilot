import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, apiJson, apiRequest } from '../../api/client';
import type { MessageRecord, PendingAttachment, StreamEvent } from '../../types';
import type {
  ReplayResponse,
  RunRef,
  SessionRuntime,
  SessionSummary,
  SessionViewState,
} from './types';

const SESSION_EVENTS = [
  'session_started', 'session_title_updated', 'session_status_changed', 'session_finished', 'session_failed',
  'user_message_created', 'assistant_message_started', 'assistant_message_completed',
  'llm_delta', 'llm_reasoning_delta', 'tool_call_started', 'tool_call_finished', 'tool_call_failed',
  'context_compacted', 'human_approval_required', 'human_approval_resolved',
  'human_question_required', 'human_question_resolved', 'error',
];

const EMPTY_VIEW: SessionViewState = {
  messages: [],
  events: [],
  liveDelta: '',
  liveReasoningDelta: '',
  subagentLiveDeltas: {},
  subagentLiveReasoningDeltas: {},
};

type SendInput = {
  content: string;
  attachments: PendingAttachment[];
  provider?: string;
  model?: string;
  thinkingValue?: string;
  clientRequestId: string;
};

export function useAgentSession(
  agentId: string | null,
  sessionId: string | null,
  onSessionCreated: (sessionId: string) => void,
  onCatalogRefresh: () => void,
) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsAgentId, setSessionsAgentId] = useState<string | null>(null);
  const [view, setView] = useState<SessionViewState>(EMPTY_VIEW);
  const [runtime, setRuntime] = useState<SessionRuntime | null>(null);
  const [loading, setLoading] = useState(false);
  const [streamOffline, setStreamOffline] = useState(false);
  const [error, setError] = useState('');
  const sourceRef = useRef<EventSource | null>(null);
  const generationRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const latestSeqRef = useRef(0);
  const seenEventIdsRef = useRef(new Set<string>());
  const seenEventOrderRef = useRef<string[]>([]);
  const frameRef = useRef<number | null>(null);
  const pendingDeltaRef = useRef<Array<{ kind: 'text' | 'reasoning'; text: string; subagent: string | null }>>([]);
  const onSessionCreatedRef = useRef(onSessionCreated);
  const onCatalogRefreshRef = useRef(onCatalogRefresh);

  useEffect(() => {
    onSessionCreatedRef.current = onSessionCreated;
    onCatalogRefreshRef.current = onCatalogRefresh;
  }, [onCatalogRefresh, onSessionCreated]);

  const closeStream = useCallback(() => {
    sourceRef.current?.close();
    sourceRef.current = null;
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
    reconnectTimerRef.current = null;
    frameRef.current = null;
    pendingDeltaRef.current = [];
  }, []);

  const rememberEvent = (eventId: string) => {
    if (!eventId || seenEventIdsRef.current.has(eventId)) return false;
    seenEventIdsRef.current.add(eventId);
    seenEventOrderRef.current.push(eventId);
    while (seenEventOrderRef.current.length > 1200) {
      const removed = seenEventOrderRef.current.shift();
      if (removed) seenEventIdsRef.current.delete(removed);
    }
    return true;
  };

  const flushDeltas = useCallback(() => {
    frameRef.current = null;
    const pending = pendingDeltaRef.current.splice(0);
    if (!pending.length) return;
    setView((current) => {
      let next = current;
      for (const item of pending) {
        if (item.subagent) {
          const key = item.kind === 'text' ? 'subagentLiveDeltas' : 'subagentLiveReasoningDeltas';
          next = {
            ...next,
            [key]: {
              ...next[key],
              [item.subagent]: `${next[key][item.subagent] || ''}${item.text}`,
            },
          };
        } else {
          const key = item.kind === 'text' ? 'liveDelta' : 'liveReasoningDelta';
          next = { ...next, [key]: `${next[key]}${item.text}` };
        }
      }
      return next;
    });
  }, []);

  const handleEvent = useCallback((event: StreamEvent, expectedAgentId: string, expectedSessionId: string) => {
    if (event.agent_id && event.agent_id !== expectedAgentId) return;
    if (event.session_id !== expectedSessionId) return;
    if (!rememberEvent(event.event_id)) return;
    latestSeqRef.current = Math.max(latestSeqRef.current, event.seq);
    if (event.event_type === 'llm_delta' || event.event_type === 'llm_reasoning_delta') {
      pendingDeltaRef.current.push({
        kind: event.event_type === 'llm_delta' ? 'text' : 'reasoning',
        text: String(event.data.text || ''),
        subagent: event.data.agent_kind === 'subagent'
          ? String(event.data.context_id || event.data.parent_call_id || 'subagent')
          : null,
      });
      if (frameRef.current === null) frameRef.current = window.requestAnimationFrame(flushDeltas);
      return;
    }
    setView((current) => {
      const events = [...current.events, event].slice(-240);
      if (
        (event.event_type === 'assistant_message_completed' || event.event_type === 'user_message_created')
        && event.data.message && typeof event.data.message === 'object'
      ) {
        const message = event.data.message as MessageRecord;
        const messages = upsertMessage(current.messages, message);
        if (message.info?.agent_kind === 'subagent') {
          const key = String(message.info.context_id || message.info.parent_call_id || 'subagent');
          const text = { ...current.subagentLiveDeltas };
          const reasoning = { ...current.subagentLiveReasoningDeltas };
          delete text[key];
          delete reasoning[key];
          return { ...current, messages, events, subagentLiveDeltas: text, subagentLiveReasoningDeltas: reasoning };
        }
        return { ...current, messages, events, liveDelta: '', liveReasoningDelta: '' };
      }
      return { ...current, events };
    });
    if (event.event_type === 'error') {
      const message = String(event.data.message || '本次执行发生错误。').slice(0, 800);
      setError(message);
    }
    if (event.event_type === 'human_approval_required' || event.event_type === 'human_question_required') {
      const interactionId = String(
        event.data.approval_id || event.data.question_id || event.data.interaction_id || '',
      );
      setRuntime((current) => current ? {
        ...current,
        status: 'WAITING_HUMAN',
        pending_interaction: {
          interaction_id: interactionId,
          run_id: event.run_id || current.active_run?.run_id || '',
          kind: event.event_type === 'human_approval_required' ? 'approval' : 'question',
          request: event.data,
        },
      } : current);
      onCatalogRefreshRef.current();
    }
    if (event.event_type === 'human_approval_resolved' || event.event_type === 'human_question_resolved') {
      setRuntime((current) => current ? { ...current, status: 'RUNNING', pending_interaction: null } : current);
      onCatalogRefreshRef.current();
    }
    if (['session_finished', 'session_failed'].includes(event.event_type)) {
      if (event.event_type === 'session_failed' && !event.data.message) setError((current) => current || '本次执行失败，请查看最近 Run 状态。');
      setRuntime((current) => current ? { ...current, status: event.event_type === 'session_failed' ? 'FAILED' : 'COMPLETED', active_run: null } : current);
      onCatalogRefreshRef.current();
    }
  }, [flushDeltas]);

  const connect = useCallback((selectedAgentId: string, selectedSessionId: string, generation: number) => {
    sourceRef.current?.close();
    const source = new EventSource(
      `/api/agents/${encodeURIComponent(selectedAgentId)}/sessions/${encodeURIComponent(selectedSessionId)}/stream?after_seq=${latestSeqRef.current}`,
    );
    const receive = (raw: Event) => {
      if (generation !== generationRef.current) return;
      try {
        handleEvent(JSON.parse((raw as MessageEvent).data) as StreamEvent, selectedAgentId, selectedSessionId);
      } catch {
        setError('收到无法解析的会话事件，正在重新同步。');
      }
    };
    SESSION_EVENTS.forEach((type) => source.addEventListener(type, receive));
    source.addEventListener('stream_reset_required', () => {
      source.close();
      generationRef.current += 1;
      setStreamOffline(true);
    });
    source.onopen = () => {
      reconnectAttemptRef.current = 0;
      setStreamOffline(false);
    };
    source.onerror = () => {
      if (sourceRef.current !== source || generation !== generationRef.current) return;
      source.close();
      setStreamOffline(true);
      const delay = Math.min(1000 * (2 ** reconnectAttemptRef.current++), 10000);
      reconnectTimerRef.current = window.setTimeout(
        () => connect(selectedAgentId, selectedSessionId, generation),
        delay,
      );
    };
    sourceRef.current = source;
  }, [handleEvent]);

  const loadSessions = useCallback(async (selectedAgentId: string, signal?: AbortSignal) => {
    const response = await apiRequest<{ sessions: SessionSummary[] }>(
      `/api/agents/${encodeURIComponent(selectedAgentId)}/sessions`,
      { signal },
    );
    if (!signal?.aborted) {
      setSessions(response.sessions);
      setSessionsAgentId(selectedAgentId);
    }
  }, []);

  const loadReplay = useCallback(async (
    selectedAgentId: string,
    selectedSessionId: string,
    signal?: AbortSignal,
  ) => {
    const generation = ++generationRef.current;
    closeStream();
    setLoading(true);
    setError('');
    const replay = await apiRequest<ReplayResponse>(
      `/api/agents/${encodeURIComponent(selectedAgentId)}/sessions/${encodeURIComponent(selectedSessionId)}/replay`,
      { signal },
    );
    if (signal?.aborted || generation !== generationRef.current) return;
    latestSeqRef.current = replay.latest_event_seq;
    seenEventIdsRef.current.clear();
    seenEventOrderRef.current = [];
    setView({ ...EMPTY_VIEW, messages: replay.messages });
    setRuntime(replay.runtime);
    setLoading(false);
    connect(selectedAgentId, selectedSessionId, generation);
  }, [closeStream, connect]);

  useEffect(() => {
    const controller = new AbortController();
    if (!agentId) {
      setSessions([]);
      setSessionsAgentId(null);
      setView(EMPTY_VIEW);
      setRuntime(null);
      return () => controller.abort();
    }
    setSessionsAgentId(null);
    void loadSessions(agentId, controller.signal).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '会话列表加载失败');
    });
    return () => controller.abort();
  }, [agentId, loadSessions]);

  useEffect(() => {
    const controller = new AbortController();
    if (!agentId || !sessionId) {
      generationRef.current += 1;
      closeStream();
      setView(EMPTY_VIEW);
      setRuntime(null);
      setLoading(false);
      setError('');
      return () => controller.abort();
    }
    void loadReplay(agentId, sessionId, controller.signal).catch((reason) => {
      if (!controller.signal.aborted) {
        setLoading(false);
        setError(reason instanceof Error ? reason.message : '会话恢复失败');
      }
    });
    return () => {
      controller.abort();
      generationRef.current += 1;
      closeStream();
    };
  }, [agentId, closeStream, loadReplay, sessionId]);

  const send = useCallback(async (input: SendInput) => {
    if (!agentId) throw new Error('请先选择 Agent。');
    const payload: Record<string, unknown> = {
      session_id: sessionId,
      content: input.content,
      client_request_id: input.clientRequestId,
      attachments: input.attachments.map(({ filename, mime, data_base64 }) => ({ filename, mime, data_base64 })),
      metadata: input.thinkingValue ? { thinking_value: input.thinkingValue } : {},
    };
    if (input.provider && input.model) {
      payload.provider = input.provider;
      payload.model = input.model;
    }
    const run = await apiJson<{ ref: RunRef; status: string }>(
      `/api/agents/${encodeURIComponent(agentId)}/runs`,
      'POST',
      payload,
    );
    if (!sessionId) onSessionCreatedRef.current(run.ref.session_id);
    setRuntime((current) => ({
      status: run.status,
      provider: current?.provider || input.provider || null,
      model: current?.model || input.model || null,
      thinking_value: current?.thinking_value || input.thinkingValue || null,
      active_run: {
        run_id: run.ref.run_id,
        status: run.status,
        revision_id: run.ref.revision_id,
        started_at: new Date().toISOString(),
      },
      pending_interaction: null,
    }));
    await loadSessions(agentId);
    onCatalogRefreshRef.current();
    return run;
  }, [agentId, loadSessions, sessionId]);

  const cancel = useCallback(async () => {
    if (!agentId || !sessionId || !runtime?.active_run) return;
    const runId = runtime.active_run.run_id;
    await apiJson(
      `/api/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/cancel`,
      'POST',
    );
    setRuntime((current) => current ? { ...current, status: 'CANCELLED', active_run: null, pending_interaction: null } : current);
    onCatalogRefreshRef.current();
  }, [agentId, runtime?.active_run, sessionId]);

  const replyInteraction = useCallback(async (payload: Record<string, unknown>) => {
    const pending = runtime?.pending_interaction;
    if (!agentId || !sessionId || !pending) return;
    await apiJson(
      `/api/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(pending.run_id)}/interactions/${encodeURIComponent(pending.interaction_id)}`,
      'POST',
      payload,
    );
    setRuntime((current) => current ? { ...current, status: 'RUNNING', pending_interaction: null } : current);
    onCatalogRefreshRef.current();
  }, [agentId, runtime?.pending_interaction, sessionId]);

  return {
    sessions,
    sessionsAgentId,
    view,
    runtime,
    loading,
    streamOffline,
    error,
    setError,
    refreshSessions: () => agentId ? loadSessions(agentId) : Promise.resolve(),
    send,
    cancel,
    replyInteraction,
  };
}

function upsertMessage(messages: MessageRecord[], message: MessageRecord): MessageRecord[] {
  const id = message.info?.id;
  if (!id) return [...messages, message];
  const index = messages.findIndex((item) => item.info?.id === id);
  if (index < 0) return [...messages, message];
  const next = [...messages];
  next[index] = message;
  return next;
}

export function isUnknownRequest(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500;
}
