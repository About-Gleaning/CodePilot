import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  mocks.session.replyInteraction.mockClear();
  mocks.session.send.mockClear();
  vi.unstubAllGlobals();
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

  it('支持 $ Skill 补全、键盘选择和 Escape 关闭', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ skills: [{ name: 'deploy', description: '发布流程' }] }), { status: 200 })));
    render(<AgentStudio />);
    const input = await screen.findByPlaceholderText('输入任务；Enter 发送，Shift+Enter 换行。');
    fireEvent.change(input, { target: { value: '$de', selectionStart: 3 } });
    expect(await screen.findByRole('option', { name: /deploy/ })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(input).toHaveValue('$deploy ');
    fireEvent.change(input, { target: { value: '$d', selectionStart: 2 } });
    expect(await screen.findByRole('listbox')).toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('支持 @ 文件补全并用鼠标替换触发词', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).includes('/workspace/files') ? { files: [{ path: 'src/main.py' }] } : { skills: [] }), { status: 200 })));
    render(<AgentStudio />);
    const input = await screen.findByPlaceholderText('输入任务；Enter 发送，Shift+Enter 换行。');
    fireEvent.change(input, { target: { value: '@src', selectionStart: 4 } });
    expect(await screen.findByRole('option', { name: /src\/main.py/ })).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole('option', { name: /src\/main.py/ }));
    expect(input).toHaveValue('@src/main.py ');
  });

  it('新会话可用下拉列表覆盖 Agent 默认模型', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      skills: [], activated_providers: [{ provider: 'deepseek', label: 'DeepSeek', models: ['deepseek-v4-pro'] }],
    }), { status: 200 })));
    render(<AgentStudio />);
    const input = await screen.findByPlaceholderText('输入任务；Enter 发送，Shift+Enter 换行。');
    fireEvent.click(screen.getByLabelText('覆盖 Agent 默认模型'));
    expect(await screen.findByLabelText('Provider')).toHaveValue('deepseek');
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'deepseek-v4-pro' } });
    fireEvent.change(input, { target: { value: '测试覆盖模型' } });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(mocks.session.send).toHaveBeenCalledWith(expect.objectContaining({
      provider: 'deepseek', model: 'deepseek-v4-pro',
    })));
  });

  it('Question 面板分步收集答案并提交', async () => {
    mocks.session.runtime.pending_interaction = {
      interaction_id: 'question-a', run_id: 'run-a', kind: 'question', request: {
        question_id: 'question-a', questions: [
          { id: 'one', question: '选择一项', options: [{ value: 'a', label: '选项 A' }] },
          { id: 'two', question: '选择多项', multiple: true, options: [{ value: 'b', label: '选项 B' }] },
        ],
      },
    };
    render(<AgentStudio />);
    expect(await screen.findByText('选择一项')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '下一题' }));
    expect(screen.getByText('请先选择一个选项。')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('选项 A'));
    fireEvent.click(screen.getByRole('button', { name: '下一题' }));
    expect(await screen.findByText('选择多项')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('选项 B'));
    fireEvent.change(screen.getByPlaceholderText('备注（可选）'), { target: { value: '补充说明' } });
    fireEvent.click(screen.getByRole('button', { name: '提交回答' }));
    await waitFor(() => expect(mocks.session.replyInteraction).toHaveBeenCalledWith({ type: 'question_reply', answers: {
      one: { values: ['a'], note: '' }, two: { values: ['b'], note: '补充说明' },
    } }));
  });

  it('Question 数据不完整时展示退出提示，不再静默隐藏', async () => {
    mocks.session.runtime.pending_interaction = {
      interaction_id: 'question-invalid', run_id: 'run-a', kind: 'question', request: { question_id: 'question-invalid', questions: [] },
    };
    render(<AgentStudio />);

    expect(await screen.findByRole('alert')).toHaveTextContent('无法展示问题');
    fireEvent.click(screen.getByRole('button', { name: '退出' }));
    await waitFor(() => expect(mocks.session.replyInteraction).toHaveBeenCalledWith({ type: 'question_decline' }));
  });
});
