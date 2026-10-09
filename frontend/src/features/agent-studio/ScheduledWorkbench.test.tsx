import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AutomationPanel } from './AutomationPanel';
import { SessionWorkbench } from './SessionWorkbench';
import { useAgentSession } from './useAgentSession';
import { MockEventSource } from '../../test/setup';
import type { AgentSummary, SessionRuntime } from './types';

const agent: AgentSummary = { agent_id: 'agent', revision_id: 'revision', name: '测试', source: 'custom', visibility: 'private', archived: false, validation_status: 'valid' };
const json = (value: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); MockEventSource.reset(); });

describe('定时任务编辑', () => {
  it.each(['once', 'interval', 'daily', 'weekly'])('保存 %s 触发与默认配置选项', async (kind) => {
    let saved: Record<string, unknown> | undefined;
    vi.stubGlobal('fetch', vi.fn((input, init) => {
      const url = String(input);
      if (init?.method === 'POST') { saved = JSON.parse(init.body); return json({ ok: true }); }
      if (url.includes('agent-capabilities')) return json({ providers: [] });
      if (url.includes('schedule-runs')) return json({ active: [], recent: [], total: 0 });
      return json({ schedules: [], total: 0 });
    }));
    render(<AutomationPanel active agents={[agent]} agentId="agent" onOpenSession={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '新建定时任务' }));
    fireEvent.change(screen.getByLabelText('任务名称'), { target: { value: '巡检' } });
    fireEvent.change(screen.getByLabelText('任务要求'), { target: { value: '完成巡检' } });
    fireEvent.change(screen.getByLabelText('触发方式'), { target: { value: kind } });
    if (kind === 'once') fireEvent.change(screen.getByLabelText('执行时间（本机时区）'), { target: { value: '2030-01-01T10:00' } });
    fireEvent.click(screen.getByRole('button', { name: '保存任务' }));
    await waitFor(() => expect(saved).toMatchObject({ agent_id: 'agent', follow_agent_model: true, follow_agent_directory: true, trigger: { kind } }));
  });

  it('保存失败及页面往返保留表单草稿', async () => {
    vi.stubGlobal('fetch', vi.fn((input, init) => {
      if (init?.method === 'POST') return json({ detail: { message: '依赖缺失' } }, 409);
      if (String(input).includes('agent-capabilities')) return json({ providers: [] });
      if (String(input).includes('schedule-runs')) return json({ active: [], recent: [], total: 0 });
      return json({ schedules: [], total: 0 });
    }));
    const props = { agents: [agent], agentId: 'agent', onOpenSession: vi.fn() };
    const view = render(<AutomationPanel {...props} active />);
    fireEvent.click(screen.getByRole('button', { name: '新建定时任务' }));
    fireEvent.change(screen.getByLabelText('任务名称'), { target: { value: '保留草稿' } });
    fireEvent.change(screen.getByLabelText('任务要求'), { target: { value: '测试任务' } });
    fireEvent.click(screen.getByRole('button', { name: '保存任务' }));
    await screen.findByText('依赖缺失');
    view.rerender(<AutomationPanel {...props} active={false} />);
    view.rerender(<AutomationPanel {...props} active />);
    expect(screen.getByLabelText('任务名称')).toHaveValue('保留草稿');
  });
});

const runtime: SessionRuntime = { source: 'schedule', schedule_run_id: 'run', status: 'CANCELLED', stop_reason: 'max_iterations',
  provider: 'test', model: 'test', thinking_value: null, active_run: null, pending_interaction: null,
  worker_exited: true, last_run: { run_id: 'run', status: 'CANCELLED', revision_id: 'rev', started_at: null, ended_at: null, error_code: 'max_iterations', error_summary: null } };

it('成果按 Run 隔离，超限不显示完成', () => {
  render(<SessionWorkbench runtime={runtime} events={[]} onStop={vi.fn()} messages={[
    { info: { id: 'old', run_id: 'old-run', role: 'assistant' }, parts: [{ type: 'text', text: '旧成果' }] },
    { info: { id: 'current', run_id: 'run', role: 'assistant' }, parts: [{ type: 'text', text: '本轮已有输出' }] },
  ]} />);
  expect(screen.getByText('已停止：达到轮数上限')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: '成果' }));
  expect(screen.getByText('本轮已有输出')).toBeInTheDocument();
  expect(screen.queryByText('旧成果')).not.toBeInTheDocument();
  expect(screen.queryByText('最终回复')).not.toBeInTheDocument();
});

it('定时会话增量轮询不创建 SSE，停止走定时接口', async () => {
  MockEventSource.reset();
  const calls: string[] = [];
  const running = { ...runtime, status: 'RUNNING', can_send: false, worker_exited: false,
    active_run: { run_id: 'run', status: 'RUNNING', revision_id: 'rev', started_at: null } };
  vi.stubGlobal('fetch', vi.fn((input) => {
    const url = String(input); calls.push(url);
    if (url.endsWith('/sessions')) return json({ sessions: [] });
    if (url.endsWith('/replay')) return json({ messages: [], latest_event_seq: 0, runtime: running });
    if (url.includes('/events?')) return json({ events: [], cursor: '2026-09-23:0', has_more: false, runtime: running });
    if (url.endsWith('/stop')) return json({ ok: true });
    throw new Error(url);
  }));
  function Probe() {
    const value = useAgentSession('agent', 'sess_test', vi.fn(), vi.fn());
    return <button onClick={() => void value.cancel()}>{value.runtime?.can_send === false ? '停止测试' : '准备中'}</button>;
  }
  const view = render(<Probe />);
  await screen.findByRole('button', { name: '停止测试' });
  await waitFor(() => expect(calls.some((url) => url.includes('/events?'))).toBe(true));
  expect(MockEventSource.active()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '停止测试' }));
  await waitFor(() => expect(calls).toContain('/api/schedule-runs/run/stop'));
  view.unmount();
});

it('切换会话后忽略旧定时轮询结果', async () => {
  let finish: ((value: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn((input) => {
    const url = String(input);
    if (url.endsWith('/sessions')) return json({ sessions: [] });
    if (url.includes('sess_first/replay')) return json({ messages: [], latest_event_seq: 0, runtime: { ...runtime, status: 'RUNNING', worker_exited: false } });
    if (url.endsWith('/replay')) return json({ messages: [], latest_event_seq: 0, runtime: { ...runtime, source: 'schedule', schedule_run_id: 'second', status: 'COMPLETED' } });
    if (url.includes('/run/events?')) return new Promise<Response>((resolve) => { finish = resolve; });
    return json({ events: [], cursor: '', has_more: false, runtime: { ...runtime, status: 'COMPLETED' } });
  }));
  function Probe({ sessionId }: { sessionId: string }) {
    const value = useAgentSession('agent', sessionId, vi.fn(), vi.fn());
    return <output>{value.runtime?.status}</output>;
  }
  const view = render(<Probe sessionId="sess_first" />);
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<Probe sessionId="sess_second" />);
  await screen.findByText('COMPLETED');
  await act(async () => { finish!(await json({ events: [], cursor: '', has_more: false, runtime: { ...runtime, status: 'FAILED' } })); });
  expect(screen.getByText('COMPLETED')).toBeInTheDocument();
});
