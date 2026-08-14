import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { AgentTaskBoard } from './AgentTaskBoard';
import type { AgentSummary } from './types';

const agents: AgentSummary[] = [
  { agent_id: 'idle', revision_id: 'r1', name: 'build', source: 'builtin', archived: false, validation_status: 'valid' as const },
  { agent_id: 'waiting', revision_id: 'r2', name: 'plan', source: 'builtin', archived: false, validation_status: 'valid' as const },
  { agent_id: 'stopped', revision_id: 'r3', name: 'explore', source: 'builtin', archived: false, validation_status: 'valid' as const },
];

const runtimes = {
  idle: { agent_id: 'idle', desired_state: 'RUNNING' as const, lifecycle_state: 'RUNNING' as const, recent_session_id: null, active_run_count: 0, waiting_human_count: 0, error_code: null },
  waiting: { agent_id: 'waiting', desired_state: 'RUNNING' as const, lifecycle_state: 'RUNNING' as const, recent_session_id: 's1', active_run_count: 1, waiting_human_count: 1, error_code: null },
  stopped: { agent_id: 'stopped', desired_state: 'STOPPED' as const, lifecycle_state: 'STOPPED' as const, recent_session_id: null, active_run_count: 0, waiting_human_count: 0, error_code: null },
};

describe('AgentTaskBoard', () => {
  it('按运行状态分组，并隔离每张卡的任务输入', async () => {
    const onSend = vi.fn(async () => undefined);
    render(<AgentTaskBoard agents={agents} runtimes={runtimes} snapshots={{}} pinnedAgentIds={[]} sendingAgentIds={new Set()} onOpen={vi.fn()} onTogglePin={vi.fn()} onSend={onSend} onStart={vi.fn()} onStop={vi.fn()} />);

    expect(screen.getByRole('region', { name: '等待处理' })).toHaveTextContent('plan');
    expect(screen.getByRole('region', { name: '空闲' })).toHaveTextContent('build');
    expect(screen.getByRole('region', { name: '异常 / 已停止' })).toHaveTextContent('explore');

    const buildInput = screen.getByPlaceholderText('向 build 下达任务');
    fireEvent.change(buildInput, { target: { value: '构建功能' } });
    fireEvent.change(screen.getByPlaceholderText('向 plan 下达任务'), { target: { value: '先规划' } });
    fireEvent.click(buildInput.closest('form')!.querySelector('button[type="submit"]')!);
    expect(onSend).toHaveBeenCalledWith('idle', '构建功能');
    expect(screen.getByPlaceholderText('向 plan 下达任务')).toHaveValue('先规划');
  });
});
