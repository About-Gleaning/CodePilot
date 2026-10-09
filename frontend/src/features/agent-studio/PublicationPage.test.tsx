import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiJson, apiRequest } from '../../api/client';
import { PublicationPage } from './PublicationPage';

vi.mock('../../api/client', () => ({ apiJson: vi.fn(), apiRequest: vi.fn() }));
vi.mock('./ConnectionEditor', () => ({ ConnectionEditor: () => <div>个人连接</div> }));
vi.mock('./WorkingDirectoryPicker', () => ({ WorkingDirectoryPicker: () => <div>个人目录</div> }));
const profile = { name: 'review', description: '审查助手', system_prompt: '审查指令', kind: 'agent', allowed_tools: ['read_file'], default_provider: 'test', default_model: 'model', max_iterations: 5, readonly: true, memory_enabled: true };
const detail = { id: 'p', name: 'review', description: '审查助手', author: '作者', owned: false, status: 'published', number: 1, revision: 'revision', profiles: { p: profile }, resources: [{ id: 's', name: 'skill-demo', kind: 'skill' }] };
const usage = { revision: null, working_directory: null, connections: {} };
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(apiRequest).mockImplementation(async url => String(url).endsWith('/usage') ? usage : String(url).endsWith('/resources/s') ? { revision: 'r', files: ['SKILL.md'] } : String(url).includes('?') ? { publications: [detail] } : detail);
  const bytes = new TextEncoder().encode('---\nname: demo\ndescription: 技能说明\n---\n原正文');
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, arrayBuffer: async () => bytes.buffer })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('使用者只读公共资源，使用前保存个人设置', async () => {
  const use = vi.fn().mockResolvedValue(undefined);
  vi.mocked(apiJson).mockResolvedValue({ ...usage, revision: 'new' });
  render(<PublicationPage onUse={use} />);
  fireEvent.click(await screen.findByRole('button', { name: /review 审查助手/ }));
  await screen.findByRole('button', { name: '使用 Agent' });
  fireEvent.click(screen.getByRole('button', { name: '配置与资源' }));
  fireEvent.click(screen.getByRole('button', { name: 'skill-demo 技能' }));
  await waitFor(() => expect(screen.getByLabelText('技能内容')).toBeDisabled());
  expect(screen.queryByRole('button', { name: '保存到公共资源' })).not.toBeInTheDocument();
  expect(screen.queryByText('复制')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '使用 Agent' }));
  await waitFor(() => expect(use).toHaveBeenCalledWith('p'));
  expect(apiJson).toHaveBeenCalledWith('/api/agent-publications/p/usage', 'PUT', expect.objectContaining({ connections: {} }));
});

it('作者编辑公共技能，切换标签保留草稿，失败后保留正文', async () => {
  vi.mocked(apiRequest).mockImplementation(async url => String(url).endsWith('/usage') ? usage : String(url).endsWith('/resources/s') ? { revision: 'r', files: ['SKILL.md'] } : String(url).includes('?') ? { publications: [detail] } : { ...detail, owned: true });
  vi.mocked(apiJson).mockRejectedValue(new Error('版本冲突'));
  render(<PublicationPage onUse={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: /review 审查助手/ }));
  fireEvent.click(await screen.findByRole('button', { name: '配置与资源' }));
  fireEvent.click(screen.getByRole('button', { name: 'skill-demo 技能' }));
  await waitFor(() => expect(screen.getByLabelText('技能内容')).toBeEnabled());
  fireEvent.change(screen.getByLabelText('技能内容'), { target: { value: '修改正文' } });
  fireEvent.click(screen.getByRole('button', { name: '概览' }));
  fireEvent.click(screen.getByRole('button', { name: '配置与资源' }));
  expect(screen.getByLabelText('技能内容')).toHaveValue('修改正文');
  fireEvent.click(screen.getByRole('button', { name: '保存到公共资源' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('技能内容')).toHaveValue('修改正文');
});

it('确认发布带预览摘要与稳定请求 ID，失败重试不生成新身份', async () => {
  vi.mocked(apiJson).mockImplementation(async url => {
    if (String(url).endsWith('/preview')) return { candidate_id: 'c', digest: 'd', expected_revision: null, profiles: { p: profile }, resources: [], excluded: ['凭证'] };
    throw new Error('网络暂时失败');
  });
  render(<PublicationPage sourceAgentId="source" onUse={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '确认发布' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: '确认发布' }));
  await waitFor(() => expect(vi.mocked(apiJson).mock.calls.filter(call => call[0].endsWith('/confirm'))).toHaveLength(2));
  const calls = vi.mocked(apiJson).mock.calls.filter(call => call[0].endsWith('/confirm'));
  expect(calls[0][2]).toEqual(calls[1][2]);
});

it('准确展示发布依赖与引用方，统一预览一次确认', async () => {
  const issue = { code: 'tool_publication_required', message: '请先发布或恢复代码工具：订单查询', resource_name: '订单查询', resource_kind: 'tool', referenced_by: [{ agent_id: 'source', name: '主助手' }, { agent_id: 'child', name: '查询助手' }], action: 'publish', suggestion: '查看并一并发布依赖。' };
  const dependency = { tool_id: 'public:tool', name: '订单查询', action: 'restore', call_name: 'query_order', version: 'old', definition: { name: '订单查询', call_name: 'query_order' }, files: { 'main.py': '原公共代码' } };
  const error = Object.assign(new Error('发布检查未通过'), { code: 'publication_dependencies_missing', issues: [issue] });
  vi.mocked(apiJson).mockImplementation(async (url, _method, body) => {
    if (String(url).endsWith('/preview')) {
      if (!(body as { include_dependencies?: boolean }).include_dependencies) throw error;
      return { candidate_id: 'c', digest: 'd', expected_revision: null, profiles: { p: { ...profile, tool_ids: ['public:tool'] } }, resources: [], dependencies: [dependency], excluded: ['凭证'] };
    }
    return { id: 'p' };
  });
  render(<PublicationPage sourceAgentId="source" onUse={vi.fn()} />);
  await screen.findByRole('alert');
  expect(screen.getByText('引用 Agent：主助手、查询助手')).toBeInTheDocument();
  expect(screen.queryByText(/ApiError:/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '查看并一并发布' }));
  await screen.findByText(/恢复影响所有引用此工具的 Agent/);
  expect(screen.getByText('原公共代码')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '发布依赖并发布 Agent' }));
  await screen.findByText('已发布');
  expect(vi.mocked(apiJson).mock.calls.filter(call => call[0].endsWith('/confirm'))).toHaveLength(1);
});

it('部分失败显示真实步骤，重试使用相同候选与请求 ID', async () => {
  const dependency = { tool_id: 'tool', name: '订单查询', action: 'publish', definition: {}, files: {}, call_name: 'query_order' };
  const candidate = { candidate_id: 'c', digest: 'd', expected_revision: null, profiles: { p: profile }, resources: [], dependencies: [dependency], excluded: [] };
  const error = Object.assign(new Error('发布保存失败'), { code: 'publication_storage_error', retryable: true, progress: [{ resource_id: 'tool', resource_name: '订单查询', action: 'publish', status: 'completed' }, { resource_id: 'p', resource_name: '主助手', action: 'publish_agent', status: 'failed' }] });
  vi.mocked(apiJson).mockImplementation(async url => {
    if (url.endsWith('/preview')) return candidate;
    throw error;
  });
  render(<PublicationPage sourceAgentId="source" onUse={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '发布依赖并发布 Agent' }));
  await screen.findByText('订单查询：发布 · 已成功');
  expect(screen.getByText('主助手：发布 · 失败')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重试发布' }));
  await waitFor(() => expect(vi.mocked(apiJson).mock.calls.filter(call => call[0].endsWith('/confirm'))).toHaveLength(2));
  const calls = vi.mocked(apiJson).mock.calls.filter(call => call[0].endsWith('/confirm'));
  expect(calls[0][2]).toEqual(calls[1][2]);
});

it('依赖变化禁止旧候选确认，要求重新检查', async () => {
  const candidate = { candidate_id: 'c', digest: 'd', expected_revision: null, profiles: { p: profile }, resources: [], dependencies: [], excluded: [] };
  vi.mocked(apiJson).mockImplementation(async url => {
    if (url.endsWith('/preview')) return candidate;
    throw Object.assign(new Error('公共工具已变化，请重新预览'), { code: 'publication_changed', retryable: false });
  });
  render(<PublicationPage sourceAgentId="source" onUse={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '确认发布' }));
  await screen.findByRole('alert');
  expect(screen.getByRole('button', { name: '确认发布' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '重新检查' })).toBeEnabled();
});

it('切换目标时清除旧预览并忽略迟到的发布响应', async () => {
  let resolveOld: (value: unknown) => void = () => {};
  vi.mocked(apiJson).mockImplementation(async (url, _method, body) => {
    if (url.endsWith('/confirm')) return new Promise(resolve => { resolveOld = resolve; });
    const source = (body as { source_agent_id: string }).source_agent_id;
    return { candidate_id: source, digest: 'd', expected_revision: null, profiles: { p: { ...profile, name: source } }, resources: [], excluded: [] };
  });
  const view = render(<PublicationPage sourceAgentId="old" onUse={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '确认发布' }));
  view.rerender(<PublicationPage sourceAgentId="new" onUse={vi.fn()} />);
  await screen.findByText(/new · 可直接启动/);
  resolveOld({ id: 'p' });
  await waitFor(() => expect(screen.queryByText('已发布')).not.toBeInTheDocument());
  expect(screen.getByText(/new · 可直接启动/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认发布' })).toBeEnabled();
});

it('离开页面后忽略迟到响应，返回时保留原发布候选', async () => {
  let resolveOld: (value: unknown) => void = () => {};
  vi.mocked(apiJson).mockImplementation(async url => {
    if (url.endsWith('/confirm')) return new Promise(resolve => { resolveOld = resolve; });
    return { candidate_id: 'c', digest: 'd', expected_revision: null, profiles: { p: profile }, resources: [], excluded: [] };
  });
  const view = render(<PublicationPage active sourceAgentId="source" onUse={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '确认发布' }));
  view.rerender(<PublicationPage active={false} sourceAgentId="source" onUse={vi.fn()} />);
  resolveOld({ id: 'p' });
  await waitFor(() => expect(screen.queryByText('已发布')).not.toBeInTheDocument());
  view.rerender(<PublicationPage active sourceAgentId="source" onUse={vi.fn()} />);
  expect(screen.getByRole('button', { name: '确认发布' })).toBeEnabled();
  expect(vi.mocked(apiJson).mock.calls.filter(call => call[0].endsWith('/preview'))).toHaveLength(1);
});
