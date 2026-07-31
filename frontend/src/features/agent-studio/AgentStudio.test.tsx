import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { PendingInteraction } from './types';

const mocks = vi.hoisted(() => ({
  catalog: {
    agents: [
      {
        agent_id: 'agent-a',
        revision_id: 'revision-a',
        name: 'build',
        description: '构建',
        source: 'builtin',
        archived: false,
        validation_status: 'valid',
        default_provider: 'test',
        default_model: 'model',
      },
      {
        agent_id: 'agent-b',
        revision_id: 'revision-b',
        name: 'plan',
        description: '规划',
        source: 'builtin',
        archived: false,
        validation_status: 'valid',
        default_provider: 'test',
        default_model: 'model',
      },
    ],
    agentsById: {} as Record<string, unknown>,
    runtimes: {
      'agent-a': {
        agent_id: 'agent-a',
        desired_state: 'RUNNING',
        lifecycle_state: 'RUNNING',
        recent_session_id: null,
        active_run_count: 0,
        waiting_human_count: 0,
        error_code: null,
      },
      'agent-b': {
        agent_id: 'agent-b',
        desired_state: 'RUNNING',
        lifecycle_state: 'RUNNING',
        recent_session_id: null,
        active_run_count: 0,
        waiting_human_count: 0,
        error_code: null,
      },
    },
    capacity: { started_agents: 2, max_started_agents: 5, active_runs: 0, max_active_runs: 5 },
    loading: false,
    offline: false,
    error: '',
    mutating: new Set<string>(),
    refresh: vi.fn(async () => undefined),
    startAgent: vi.fn(async () => undefined),
    stopAgent: vi.fn(async () => undefined),
  },
  session: {
    sessions: [],
    sessionsAgentId: 'agent-a',
    view: {
      messages: [],
      events: [],
      liveDelta: '',
      liveReasoningDelta: '',
      subagentLiveDeltas: {},
      subagentLiveReasoningDeltas: {},
    },
    runtime: {
      status: 'COMPLETED',
      provider: 'test',
      model: 'model',
      thinking_value: null,
      active_run: null,
      pending_interaction: null as PendingInteraction | null,
    },
    loading: false,
    streamOffline: false,
    error: '',
    setError: vi.fn(),
    refreshSessions: vi.fn(async () => undefined),
    send: vi.fn(async () => undefined),
    cancel: vi.fn(async () => undefined),
    replyInteraction: vi.fn(async () => undefined),
  },
}));

mocks.catalog.agentsById = Object.fromEntries(
  mocks.catalog.agents.map((agent) => [agent.agent_id, agent]),
);

vi.mock('./useAgentCatalog', () => ({
  useAgentCatalog: () => mocks.catalog,
}));

vi.mock('./useAgentSession', () => ({
  isUnknownRequest: () => false,
  useAgentSession: () => mocks.session,
}));

import AgentStudio from './AgentStudio';

afterEach(() => {
  cleanup();
  mocks.catalog.error = '';
  mocks.session.runtime.pending_interaction = null;
});

describe('Agent Studio 底部交互区', () => {
  it('无错误提示时消息区与底部输入区保持稳定顺序', async () => {
    const view = render(<AgentStudio />);

    expect(await screen.findByPlaceholderText('输入任务；Enter 发送，Shift+Enter 换行。')).toBeInTheDocument();
    expect(view.container.querySelector('.studio-message-scroll')?.nextElementSibling).toHaveClass('studio-composer');
    expect(view.container.querySelector('.studio-chat > .chat-local-error')).not.toBeInTheDocument();
  });

  it('错误提示和 Agent 切换都不会移除普通输入框', async () => {
    mocks.catalog.error = '测试错误';
    const view = render(<AgentStudio />);

    expect(await screen.findByPlaceholderText('输入任务；Enter 发送，Shift+Enter 换行。')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('测试错误');

    fireEvent.click(screen.getByRole('button', { name: /plan/ }));
    expect(screen.getByPlaceholderText('输入任务；Enter 发送，Shift+Enter 换行。')).toBeInTheDocument();
    expect(view.container.querySelector('.studio-message-scroll')?.nextElementSibling).toHaveClass('studio-composer');
  });

  it('等待审批时由固定底部的审批面板替换普通输入框', async () => {
    mocks.session.runtime.pending_interaction = {
      interaction_id: 'interaction-a',
      run_id: 'run-a',
      kind: 'approval',
      request: { reason: '需要确认' },
    };
    const view = render(<AgentStudio />);

    expect(await screen.findByText('等待人工审批')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('输入任务；Enter 发送，Shift+Enter 换行。')).not.toBeInTheDocument();
    expect(view.container.querySelector('.studio-message-scroll')?.nextElementSibling).toHaveClass('studio-composer');
  });
});
