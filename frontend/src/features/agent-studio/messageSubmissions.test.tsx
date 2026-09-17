import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MockEventSource } from '../../test/setup';
import { mergeSubmissions, useAgentSession } from './useAgentSession';

const ref = { agent_id: 'agent', session_id: 'session', run_id: 'run', revision_id: 'revision' };
const active = { run_id: 'run', status: 'RUNNING', revision_id: 'revision', started_at: 'original-time' };
const json = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value)));
let resolveSend: (value: Response) => void;
let submitted: Record<string, unknown>;

beforeEach(() => {
  MockEventSource.reset();
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith('/runs')) {
      submitted = JSON.parse(String(init?.body));
      return new Promise<Response>((resolve) => { resolveSend = resolve; });
    }
    if (url.includes('/replay')) return json({ messages: [], latest_event_seq: 0, submissions: [], runtime: {
      status: 'RUNNING', provider: 'test', model: 'model', thinking_value: null, active_run: active, pending_interaction: null,
    } });
    return json({ sessions: [] });
  }));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function emit(type: string, data: Record<string, unknown>, seq: number) {
  MockEventSource.active()[0].emit(type, { event_type: type, data, seq, event_id: `event_${seq}`, ...ref });
}

it('追加回执不清空流式文本，不重建 Run，且携带预期 Run', async () => {
  const { result } = renderHook(() => useAgentSession('agent', 'session', vi.fn(), vi.fn()));
  await waitFor(() => expect(result.current.runtime?.active_run).toEqual(active));
  act(() => emit('llm_delta', { text: '正在生成' }, 1));
  await waitFor(() => expect(result.current.view.liveDelta).toBe('正在生成'));
  let sending: Promise<unknown>;
  act(() => { sending = result.current.send({ content: '追加', attachments: [], clientRequestId: 'append' }); });
  expect(submitted.expected_run_id).toBe('run');
  act(() => emit('message_submission', { submissions: [{ mode: 'appended', message_id: 'message', status: 'included' }] }, 2));
  await act(async () => {
    resolveSend(new Response(JSON.stringify({ ref, status: 'RUNNING', submission: { mode: 'appended', message_id: 'message', status: 'pending' } })));
    await sending;
  });
  expect(result.current.view.liveDelta).toBe('正在生成');
  expect(result.current.runtime?.active_run).toEqual(active);
  expect(result.current.view.submissions?.[0].status).toBe('included');
});

it('延迟发送响应不能覆盖停止事件', async () => {
  const { result } = renderHook(() => useAgentSession('agent', 'session', vi.fn(), vi.fn()));
  await waitFor(() => expect(result.current.runtime?.active_run).toEqual(active));
  let sending: Promise<unknown>;
  act(() => { sending = result.current.send({ content: '追加', attachments: [], clientRequestId: 'append' }); });
  act(() => emit('session_finished', { status: 'CANCELLED', stop_reason: 'max_iterations' }, 1));
  await act(async () => {
    resolveSend(new Response(JSON.stringify({ ref, status: 'RUNNING', submission: { mode: 'appended', message_id: 'message', status: 'pending' } })));
    await sending;
  });
  expect(result.current.runtime?.status).toBe('CANCELLED');
  expect(result.current.runtime?.stop_reason).toBe('max_iterations');
  expect(result.current.runtime?.active_run).toBeNull();
});

it('消息状态合并不回退且保持内容', () => {
  const message = { info: { id: 'message', role: 'user' }, parts: [{ type: 'text', text: '追加内容' }] };
  const merged = mergeSubmissions([{ mode: 'appended', message_id: 'message', status: 'included', message }],
    [{ mode: 'appended', message_id: 'message', status: 'pending' }]);
  expect(merged).toHaveLength(1);
  expect(merged[0].status).toBe('included');
  expect(merged[0].message).toEqual(message);
});

it('新 Run 的开始事件先于 HTTP 返回时仍保留活动身份', async () => {
  const { result } = renderHook(() => useAgentSession('agent', 'session', vi.fn(), vi.fn()));
  await waitFor(() => expect(result.current.runtime?.active_run).toEqual(active));
  act(() => emit('session_finished', { status: 'COMPLETED' }, 1));
  let sending: Promise<unknown>;
  act(() => { sending = result.current.send({ content: '继续', attachments: [], clientRequestId: 'next' }); });
  expect(submitted.expected_run_id).toBeUndefined();
  act(() => MockEventSource.active()[0].emit('loop_started', {
    event_type: 'loop_started', data: { agent_kind: 'agent' }, seq: 2, event_id: 'new_loop', ...ref, run_id: 'next_run',
  }));
  await act(async () => {
    resolveSend(new Response(JSON.stringify({ ref: { ...ref, run_id: 'next_run' }, status: 'RUNNING',
      submission: { mode: 'started', message_id: 'new_message', status: 'accepted' } })));
    await sending;
  });
  expect(result.current.runtime?.status).toBe('RUNNING');
  expect(result.current.runtime?.active_run?.run_id).toBe('next_run');
});
