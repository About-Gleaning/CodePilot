import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { apiJson, apiRequest } from '../../api/client';
import type { AgentRuntime, AgentSummary, RuntimeCapacity, RuntimeOverview } from './types';

const EMPTY_CAPACITY: RuntimeCapacity = {
  started_agents: 0,
  max_started_agents: 5,
  active_runs: 0,
  max_active_runs: 5,
};

const CONTROL_EVENTS = [
  'agent_starting', 'agent_running', 'agent_stopping', 'agent_stopped', 'agent_error',
  'run_running', 'run_waiting_human', 'run_completed', 'run_failed', 'run_cancelled',
  'interaction_pending', 'interaction_resolved',
];

export function useAgentCatalog() {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [runtimes, setRuntimes] = useState<Record<string, AgentRuntime>>({});
  const [capacity, setCapacity] = useState<RuntimeCapacity>(EMPTY_CAPACITY);
  const [loading, setLoading] = useState(true);
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState('');
  const [mutating, setMutating] = useState<Set<string>>(new Set());
  const cursorRef = useRef('');
  const sourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const refreshTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const generationRef = useRef(0);
  const mutatingRef = useRef(new Set<string>());

  const closeStream = useCallback(() => {
    sourceRef.current?.close();
    sourceRef.current = null;
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    reconnectTimerRef.current = null;
    refreshTimerRef.current = null;
  }, []);

  const applyOverview = useCallback((overview: RuntimeOverview) => {
    setRuntimes(Object.fromEntries(overview.runtimes.map((item) => [item.agent_id, item])));
    setCapacity(overview.capacity);
    cursorRef.current = overview.cursor;
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const generation = ++generationRef.current;
    const [catalog, overview] = await Promise.all([
      apiRequest<{ agents: AgentSummary[] }>('/api/agents?status=all', { signal }),
      apiRequest<RuntimeOverview>('/api/agent-runtimes', { signal }),
    ]);
    if (signal?.aborted || generation !== generationRef.current) return;
    setAgents(catalog.agents);
    applyOverview(overview);
    setError('');
    setOffline(false);
    setLoading(false);
  }, [applyOverview]);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimerRef.current !== null) return;
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = null;
      void refresh().catch(() => setOffline(true));
    }, 60);
  }, [refresh]);

  const connect = useCallback((cursor: string) => {
    sourceRef.current?.close();
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
    const source = new EventSource(`/api/agent-runtimes/stream${query}`);
    const receive = () => {
      reconnectAttemptRef.current = 0;
      setOffline(false);
      scheduleRefresh();
    };
    CONTROL_EVENTS.forEach((type) => source.addEventListener(type, receive));
    source.addEventListener('stream_reset_required', () => {
      source.close();
      void refresh().then(() => connect(cursorRef.current)).catch(() => setOffline(true));
    });
    source.onopen = () => {
      reconnectAttemptRef.current = 0;
      setOffline(false);
    };
    source.onerror = () => {
      if (sourceRef.current !== source) return;
      source.close();
      setOffline(true);
      const attempt = reconnectAttemptRef.current++;
      const delay = Math.min(1000 * (2 ** attempt), 10000);
      reconnectTimerRef.current = window.setTimeout(() => {
        void refresh()
          .then(() => connect(cursorRef.current))
          .catch(() => connect(cursorRef.current));
      }, delay);
    };
    sourceRef.current = source;
  }, [refresh, scheduleRefresh]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal)
      .then(() => connect(cursorRef.current))
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : 'Agent 列表加载失败');
          setLoading(false);
        }
      });
    return () => {
      controller.abort();
      generationRef.current += 1;
      closeStream();
    };
  }, [closeStream, connect, refresh]);

  const mutateRuntime = useCallback(async (agentId: string, action: 'start' | 'stop') => {
    if (mutatingRef.current.has(agentId)) return;
    mutatingRef.current.add(agentId);
    setMutating((current) => new Set(current).add(agentId));
    try {
      const runtime = await apiJson<AgentRuntime>(`/api/agents/${agentId}/${action}`, 'POST');
      setRuntimes((current) => ({ ...current, [agentId]: runtime }));
      await refresh();
    } finally {
      mutatingRef.current.delete(agentId);
      setMutating((current) => {
        const next = new Set(current);
        next.delete(agentId);
        return next;
      });
    }
  }, [refresh]);

  const agentsById = useMemo(
    () => Object.fromEntries(agents.map((agent) => [agent.agent_id, agent])),
    [agents],
  );

  return {
    agents,
    agentsById,
    runtimes,
    capacity,
    loading,
    offline,
    error,
    mutating,
    refresh,
    startAgent: (agentId: string) => mutateRuntime(agentId, 'start'),
    stopAgent: (agentId: string) => mutateRuntime(agentId, 'stop'),
  };
}
