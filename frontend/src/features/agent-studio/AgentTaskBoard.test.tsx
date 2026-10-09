import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AgentTaskBoard } from './AgentTaskBoard';
import type { AgentSummary } from './types';

const agents: AgentSummary[] = [
  { agent_id: 'idle', revision_id: 'r1', name: 'build', description: '实现与验证', launch_modes: ['direct'], source: 'builtin', archived: false, validation_status: 'valid' },
  { agent_id: 'waiting', revision_id: 'r2', name: 'plan', description: '分析需求', launch_modes: ['direct'], source: 'builtin', archived: false, validation_status: 'valid' },
  { agent_id: 'stopped', revision_id: 'r3', name: 'explore', description: '代码探查', launch_modes: ['delegated'], source: 'builtin', visibility: 'builtin', readonly: true, archived: false, validation_status: 'valid', default_provider: 'test', default_model: 'reader' },
  { agent_id: 'delegate', revision_id: 'r4', name: 'researcher', launch_modes: ['delegated'], source: 'custom', visibility: 'private', archived: false, validation_status: 'valid' },
  { agent_id: 'archived-delegate', revision_id: 'r5', name: 'archived-reader', launch_modes: ['delegated'], source: 'custom', visibility: 'private', archived: true, validation_status: 'valid' },
];

const runtimes = {
  idle: { agent_id: 'idle', desired_state: 'STOPPED' as const, lifecycle_state: 'STOPPED' as const, recent_session_id: null, active_run_count: 0, waiting_human_count: 0, error_code: null },
  waiting: { agent_id: 'waiting', desired_state: 'RUNNING' as const, lifecycle_state: 'RUNNING' as const, recent_session_id: 's1', active_run_count: 1, waiting_human_count: 1, error_code: null },
};

afterEach(cleanup);

function renderBoard() {
  const callbacks = { onCreate: vi.fn(), onOpen: vi.fn(), onConfigure: vi.fn(), onStart: vi.fn() };
  render(<AgentTaskBoard
    agents={agents}
    runtimes={runtimes}
    recentSessions={{ waiting: { session_id: 's1', agent_id: 'waiting', title: '发布前检查', preview: '检查构建和测试结果', created_at: '2026-09-23T08:00:00Z', updated_at: '2026-09-23T09:00:00Z', status: 'RUNNING', agent_name: 'plan', provider: 'test', model: 'model', message_count: 2 } }}
    mutatingAgentIds={new Set()}
    {...callbacks}
  />);
  return callbacks;
}

describe('AgentTaskBoard', () => {
  it('在统一模块网格中分别展示 Agent 与最新任务状态', () => {
    renderBoard();

    expect(screen.getByRole('region', { name: 'Agent 总览' })).toHaveTextContent('5 个 Agent');
    expect(screen.getByRole('button', { name: '打开 plan' })).toHaveTextContent('等待处理');
    expect(screen.getByRole('button', { name: '打开 plan' })).toHaveTextContent('发布前检查');
    expect(screen.getByRole('button', { name: '打开 explore 配置' })).toHaveTextContent('仅委派');
    expect(screen.getByRole('button', { name: '打开 explore 配置' })).toHaveTextContent('Agent 委派');
    expect(screen.getByRole('button', { name: '打开 researcher 配置' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开 archived-reader 配置' })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('给 Agent 派发任务…')).not.toBeInTheDocument();
    expect(document.querySelector('.task-lane')).not.toBeInTheDocument();
  });

  it('模块主操作和必要快捷动作互不混淆', () => {
    const callbacks = renderBoard();
    fireEvent.click(screen.getByRole('button', { name: '打开 plan' }));
    expect(callbacks.onOpen).toHaveBeenCalledWith('waiting', 's1');

    fireEvent.click(screen.getByRole('button', { name: '配置 build' }));
    expect(callbacks.onConfigure).toHaveBeenCalledWith('idle');

    fireEvent.click(screen.getByRole('button', { name: '启动' }));
    expect(callbacks.onStart).toHaveBeenCalledWith('idle');

    const exploreCard = screen.getByRole('button', { name: '打开 explore 配置' }).closest('article');
    expect(exploreCard).not.toBeNull();
    expect(within(exploreCard!).queryByText('启动')).not.toBeInTheDocument();
    expect(within(exploreCard!).queryByText('新任务')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开 explore 配置' }));
    expect(callbacks.onConfigure).toHaveBeenCalledWith('stopped');
    expect(callbacks.onOpen.mock.calls.some(([agentId]) => agentId === 'stopped')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: '配置 researcher' }));
    expect(callbacks.onConfigure).toHaveBeenCalledWith('delegate');
  });

  it('支持搜索和归档范围筛选', () => {
    renderBoard();
    fireEvent.change(screen.getByPlaceholderText('搜索名称或职责'), { target: { value: '分析' } });
    expect(screen.getByRole('button', { name: '打开 plan' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开 build' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('搜索名称或职责'), { target: { value: '' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Agent 范围' }), { target: { value: 'archived' } });
    expect(screen.getByRole('button', { name: '打开 archived-reader 配置' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开 explore 配置' })).not.toBeInTheDocument();
  });
});
