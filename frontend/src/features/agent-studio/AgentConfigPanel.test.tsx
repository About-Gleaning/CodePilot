import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiJson, apiRequest } from '../../api/client';
import type { AgentCapabilities, AgentDetail } from './types';
import { AgentConfigPanel } from './AgentConfigPanel';

vi.mock('../../api/client', () => ({ apiJson: vi.fn(), apiRequest: vi.fn() }));

const detail: AgentDetail = {
  agent_id: 'agent-config', revision_id: 'revision-1', name: 'reviewer', description: '代码审查',
  source: 'custom', visibility: 'private', archived: false, validation_status: 'valid', kind: 'agent',
  default_provider: 'test', default_model: 'model', system_prompt: '检查变更', max_iterations: 20,
  readonly: false, memory_enabled: true, skill_ids: [], hook_ids: [], subagent_ids: [],
  working_directory: null, connection_ids: null, tool_names: ['read_file', 'task'], mcp_server_names: [],
};

const capabilities: AgentCapabilities = {
  providers: [{ provider: 'test', label: '测试模型', models: ['model'] }],
  tools: [
    { name: 'read_file', description: '读取文件', side_effect: 'read_only', assignable: true },
    { name: 'write_file', description: '写入文件', side_effect: 'workspace_mutation', assignable: true, requires_approval: true },
    { name: 'task', description: '委派任务', side_effect: 'runtime_mutation', assignable: true },
  ],
  mcp_servers: [{ name: 'issue-tracker', status: 'available', requires_approval: true, description: '问题跟踪' }],
  hooks: [{ hook_id: 'platform:audit', name: '审计钩子', hook_type: 'tool.after', plugin_type: 'prompt', available: true }],
  subagents: [{ agent_id: 'subagent-a', name: 'researcher', description: '资料检索' }],
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(apiRequest).mockImplementation(async (url) => {
    if (url === '/api/agent-capabilities') return capabilities;
    if (url.startsWith('/api/agents/')) return detail;
    if (url.startsWith('/api/skills')) return { skills: [], shared: [] };
    if (url.startsWith('/api/connections')) return { catalog: [], connections: [] };
    if (url.startsWith('/api/workspace/directories')) return { roots: [], directories: [], selected: '' };
    if (url.startsWith('/api/memories/')) return { content: '', revision: 'memory-1' };
    throw new Error(`未处理请求：${url}`);
  });
});
afterEach(cleanup);

describe('Agent 配置控制台', () => {
  it('提供六个分区，并在能力目录中搜索和筛选', async () => {
    render(<AgentConfigPanel agent={detail} activeRunCount={0} onBack={vi.fn()} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);

    const rail = await screen.findByRole('complementary', { name: 'Agent 配置分区' });
    expect(within(rail).getAllByRole('button')).toHaveLength(6);
    fireEvent.click(within(rail).getByRole('button', { name: /能力/ }));

    const search = await screen.findByPlaceholderText('搜索本地工具');
    fireEvent.change(search, { target: { value: '写入' } });
    expect(screen.getByText('write_file')).toBeInTheDocument();
    expect(screen.queryByText('read_file')).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('region', { name: '本地工具' })).getByRole('button', { name: /已选/ }));
    expect(screen.getByText('没有符合条件的本地工具。')).toBeInTheDocument();
  });

  it('把校验错误映射到对应分区并在进入后展示', async () => {
    const invalid = { ...detail, validation_status: 'needs_configuration' as const, validation_issues: [{ code: 'skill_missing', field: 'skill_ids', message: '技能已归档' }] };
    vi.mocked(apiRequest).mockImplementation(async (url) => url === '/api/agent-capabilities' ? capabilities : invalid);
    render(<AgentConfigPanel agent={invalid} activeRunCount={0} onBack={vi.fn()} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);

    const rail = await screen.findByRole('complementary', { name: 'Agent 配置分区' });
    const capabilityButton = within(rail).getByRole('button', { name: /能力.*1/ });
    fireEvent.click(capabilityButton);
    expect(await screen.findByText('技能已归档')).toBeInTheDocument();
  });

  it('关闭继承模式时提交显式空数组，记忆与连接入口保持分区隔离', async () => {
    const created = { ...detail, agent_id: 'created', revision_id: 'revision-created' };
    vi.mocked(apiJson).mockResolvedValue(created);
    render(<AgentConfigPanel agent={null} activeRunCount={0} onBack={vi.fn()} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);

    const rail = await screen.findByRole('complementary', { name: 'Agent 配置分区' });
    fireEvent.click(within(rail).getByRole('button', { name: /能力/ }));
    fireEvent.click(await screen.findByLabelText(/沿用共享技能/));
    fireEvent.click(screen.getByLabelText(/沿用平台 Hook/));
    fireEvent.click(within(rail).getByRole('button', { name: /连接/ }));
    fireEvent.click(await screen.findByLabelText(/沿用团队连接/));
    fireEvent.click(screen.getByRole('button', { name: '保存 Agent' }));

    await waitFor(() => expect(apiJson).toHaveBeenCalled());
    expect(apiJson).toHaveBeenCalledWith('/api/agents', 'POST', expect.objectContaining({ skill_ids: [], hook_ids: [], connection_ids: [] }));
  });

  it('仅委派启动的 Agent 继承父目录，但仍可查看完整能力配置', async () => {
    const child = { ...detail, launch_modes: ['delegated'] as Array<'delegated'>, can_delegate: false, tool_names: [] };
    vi.mocked(apiRequest).mockImplementation(async (url) => url === '/api/agent-capabilities' ? capabilities : child);
    render(<AgentConfigPanel agent={child} activeRunCount={0} onBack={vi.fn()} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);

    const rail = await screen.findByRole('complementary', { name: 'Agent 配置分区' });
    expect(screen.getByLabelText('描述')).not.toBeDisabled();
    fireEvent.click(within(rail).getByRole('button', { name: /运行/ }));
    expect(screen.getByText('继承父任务目录')).toBeInTheDocument();
    expect(screen.queryByText('新会话默认目录')).not.toBeInTheDocument();
    fireEvent.click(within(rail).getByRole('button', { name: /能力/ }));
    expect(await screen.findByText('task')).toBeInTheDocument();
    expect(screen.queryByText('Agent 委派')).not.toBeInTheDocument();
  });

  it('内置仅委派 Agent 可以查看和复制，但不能直接编辑', async () => {
    const builtinExplore = {
      ...detail,
      agent_id: 'builtin-explore',
      name: 'explore',
      source: 'builtin' as const,
      visibility: 'builtin' as const,
      launch_modes: ['delegated'] as Array<'delegated'>,
      readonly: true,
    };
    vi.mocked(apiRequest).mockImplementation(async (url) => url === '/api/agent-capabilities' ? capabilities : builtinExplore);
    render(<AgentConfigPanel agent={builtinExplore} activeRunCount={0} onBack={vi.fn()} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);

    expect(await screen.findByLabelText('描述')).toBeDisabled();
    expect(screen.getByRole('button', { name: '保存 revision' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '更多 Agent 操作' }));
    expect(screen.getByRole('button', { name: '复制为新 Agent' })).toBeInTheDocument();
  });
});

it('勾选代码工具后保存完整角色字段和稳定工具 ID', async () => {
  const tool = { tool_id: 'personal:tool-id', name: 'test-tool', description: '测试代码工具', visibility: 'private' };
  vi.mocked(apiRequest).mockImplementation(async url => {
    if (url === '/api/agent-capabilities') return capabilities;
    if (url.startsWith('/api/tools')) return { tools: [tool], total: 1 };
    if (url.startsWith('/api/agents/')) return { ...detail, launch_modes: ['direct'], can_delegate: false, tool_ids: [] };
    return { skills: [], shared: [], catalog: [], connections: [] };
  });
  vi.mocked(apiJson).mockResolvedValue({ ...detail, tool_ids: [tool.tool_id], revision_id: 'revision-2' });
  render(<AgentConfigPanel agent={detail} activeRunCount={0} onBack={vi.fn()} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);
  const rail = await screen.findByRole('complementary', { name: 'Agent 配置分区' });
  fireEvent.click(within(rail).getByRole('button', { name: /能力/ }));
  fireEvent.click(await screen.findByRole('checkbox', { name: /test-tool/ }));
  fireEvent.click(screen.getByRole('button', { name: '保存 revision' }));
  await waitFor(() => expect(apiJson).toHaveBeenCalledWith('/api/agents/agent-config', 'PUT', expect.objectContaining({ tool_ids: [tool.tool_id], launch_modes: ['direct'], can_delegate: false, expected_revision_id: 'revision-1' })));
});
