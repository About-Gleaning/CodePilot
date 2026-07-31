import { StrictMode } from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MockEventSource } from '../../test/setup';
import { useAgentCatalog } from './useAgentCatalog';
import { useAgentSession } from './useAgentSession';

const AGENT = {
  agent_id: 'agent-a',
  revision_id: 'revision-a',
  name: 'build',
  description: '构建',
  source: 'builtin',
  archived: false,
  validation_status: 'valid',
  default_provider: 'test',
  default_model: 'model',
};

const RUNTIME = {
  agent_id: 'agent-a',
  desired_state: 'RUNNING',
  lifecycle_state: 'RUNNING',
  recent_session_id: null,
  active_run_count: 0,
  waiting_human_count: 0,
  error_code: null,
};

function json(value: unknown) {
  return Promise.resolve(new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }));
}

beforeEach(() => {
  MockEventSource.reset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('useAgentCatalog', () => {
  it('在 StrictMode 下只保留一个有效聚合 SSE', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/agents?')) return json({ agents: [AGENT] });
      return json({
        runtimes: [RUNTIME],
        capacity: { started_agents: 1, max_started_agents: 5, active_runs: 0, max_active_runs: 5 },
        cursor: 'cursor-1',
      });
    }));

    function Probe() {
      const value = useAgentCatalog();
      return <div>{value.loading ? 'loading' : `${value.agents.length}:${value.capacity.max_active_runs}`}</div>;
    }

    const view = render(<StrictMode><Probe /></StrictMode>);
    await screen.findByText('1:5');
    await waitFor(() => expect(MockEventSource.active()).toHaveLength(1));
    expect(MockEventSource.active()[0].url).toContain('cursor=cursor-1');
    view.unmount();
    expect(MockEventSource.active()).toHaveLength(0);
  });
});

describe('useAgentSession', () => {
  it('父组件回调引用变化时不会重复加载同一 replay', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/sessions')) return json({ sessions: [] });
      if (url.includes('/session-a/replay')) return json(replay('message-a', 'session-a'));
      throw new Error(`unexpected ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    function Probe({ marker }: { marker: number }) {
      const value = useAgentSession(
        'agent-a',
        'session-a',
        () => void marker,
        () => void marker,
      );
      return <div>{value.view.messages.map((item) => item.info?.id).join(',')}</div>;
    }

    const view = render(<Probe marker={1} />);
    await screen.findByText('message-a');
    view.rerender(<Probe marker={2} />);
    await waitFor(() => {
      const replayCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/session-a/replay'));
      expect(replayCalls).toHaveLength(1);
    });
  });

  it('快速切换后忽略旧 replay 响应和旧 SSE', async () => {
    let resolveOld: ((response: Response) => void) | undefined;
    const oldReplay = new Promise<Response>((resolve) => { resolveOld = resolve; });
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/sessions')) return json({ sessions: [] });
      if (url.includes('/session-old/replay')) return oldReplay;
      if (url.includes('/session-new/replay')) {
        return json(replay('message-new', 'session-new'));
      }
      throw new Error(`unexpected ${url}`);
    }));

    function Probe({ sessionId }: { sessionId: string }) {
      const value = useAgentSession('agent-a', sessionId, () => undefined, () => undefined);
      return <div>{value.view.messages.map((item) => item.info?.id).join(',')}</div>;
    }

    const view = render(<Probe sessionId="session-old" />);
    view.rerender(<Probe sessionId="session-new" />);
    await screen.findByText('message-new');
    const oldSource = MockEventSource.instances.find((item) => item.url.includes('session-old'));
    resolveOld?.(new Response(JSON.stringify(replay('message-old', 'session-old')), { status: 200 }));
    oldSource?.emit('assistant_message_completed', streamMessage('late-old', 'session-old'));
    await waitFor(() => expect(screen.queryByText(/message-old|late-old/)).not.toBeInTheDocument());
    expect(screen.getByText('message-new')).toBeInTheDocument();
  });
});

function replay(messageId: string, sessionId: string) {
  return {
    session: { data: { session_id: sessionId, status: 'COMPLETED', provider: 'test', model: 'model' } },
    messages: [{
      info: { id: messageId, session_id: sessionId, role: 'assistant', agent: 'build', time: { created: 1 } },
      parts: [{ type: 'text', text: messageId }],
    }],
    records: [],
    latest_event_seq: 1,
    runtime: {
      status: 'COMPLETED',
      provider: 'test',
      model: 'model',
      thinking_value: null,
      active_run: null,
      pending_interaction: null,
    },
  };
}

function streamMessage(messageId: string, sessionId: string) {
  return {
    seq: 2,
    event_id: `event-${messageId}`,
    event_type: 'assistant_message_completed',
    agent_id: 'agent-a',
    session_id: sessionId,
    run_id: 'run-a',
    run_seq: 2,
    created_at: '2026-07-31T00:00:00Z',
    data: {
      message: {
        info: { id: messageId, session_id: sessionId, role: 'assistant', agent: 'build', time: { created: 2 } },
        parts: [{ type: 'text', text: messageId }],
      },
    },
  };
}
