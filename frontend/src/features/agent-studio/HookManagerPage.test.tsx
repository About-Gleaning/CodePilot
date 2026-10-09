import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiJson, apiRequest } from '../../api/client';
import { HookManagerPage } from './HookManagerPage';

vi.mock('../../api/client', () => ({ apiJson: vi.fn(), apiRequest: vi.fn() }));
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiRequest).mockResolvedValue({ hooks: [], total: 0 }); });
afterEach(cleanup);

describe('Hook 独立管理', () => {
  it.each([{ agents: [] }, { agents: [{ agent_id: 'a', name: '我的 Agent' }] }])('引用只读且可返回：%j', async ({ agents }) => {
    const back = vi.fn();
    const item = { hook_id: 'personal:ref', name: '引用检查', visibility: 'private', revision: 'r', version: 'v', files: {}, definition: { name: '引用检查', plugin_type: 'prompt', hook_type: 'llm.before', content: '正文' } };
    vi.mocked(apiRequest).mockImplementation(async url => String(url).endsWith('/references') ? { agents } : String(url).includes('/personal:ref') ? item : { hooks: [item], total: 1 });
    render(<HookManagerPage onReturn={back} />);
    fireEvent.click(await screen.findByRole('button', { name: /引用检查/ }));
    fireEvent.click(await screen.findByRole('button', { name: '引用情况' }));
    await screen.findByText(agents.length ? '我的 Agent' : '暂无 Agent 引用');
    expect(screen.queryByLabelText('选择要配置的 Agent')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '配置 Agent' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回 Agent 配置' }));
    expect(back).toHaveBeenCalledOnce();
    expect(apiJson).not.toHaveBeenCalled();
  });

  it('显示可读编号和更新时间，完整摘要仅通过复制获取', async () => {
    const digest = 'a'.repeat(64);
    const item = { hook_id: 'personal:v', name: '版本检查', visibility: 'private', archived: false, revision: 'r', version: digest, version_number: 3, updated_at: '2026-09-18T10:00:00Z', files: {}, definition: { name: '版本检查', plugin_type: 'prompt', hook_type: 'llm.before', content: '正文' } };
    vi.mocked(apiRequest).mockImplementation(async url => String(url).endsWith('/references') ? { agents: [] } : String(url).includes('/personal:v') ? item : { hooks: [item], total: 1 });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<HookManagerPage />);
    fireEvent.click(await screen.findByRole('button', { name: /版本检查/ }));
    expect(await screen.findByText(/第 3 版 · 更新于/)).toBeInTheDocument();
    expect(screen.queryByText(digest, { exact: false })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('技术详情'));
    fireEvent.click(screen.getByRole('button', { name: '复制完整内容标识' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(digest));
  });

  it('编辑旧补充提示时不提交残留命令', async () => {
    const item = { hook_id: 'personal:old', name: '旧提示', visibility: 'private', archived: false, revision: 'r', version: 'v', files: {}, definition: { name: '旧提示', plugin_type: 'prompt', hook_type: 'llm.before', content: '原文', argv: ['python3', '@file:missing.py'] } };
    vi.mocked(apiRequest).mockImplementation(async url => String(url).endsWith('/references') ? { agents: [] } : String(url).includes('/personal:old') ? item : { hooks: [item], total: 1 });
    vi.mocked(apiJson).mockResolvedValue(item);
    render(<HookManagerPage />);
    fireEvent.click(await screen.findByRole('button', { name: /旧提示/ }));
    fireEvent.change(await screen.findByLabelText('提示正文'), { target: { value: '新正文' } });
    fireEvent.click(screen.getByRole('button', { name: '保存钩子' }));
    await waitFor(() => expect(apiJson).toHaveBeenCalled());
    expect(vi.mocked(apiJson).mock.calls[0].slice(0, 2)).toEqual(['/api/hooks/personal:old', 'PUT']);
    const body = vi.mocked(apiJson).mock.calls[0][2] as any;
    expect(body.definition.content).toBe('新正文');
    expect(body.definition).not.toHaveProperty('argv');
  });

  it.each(['prompt', 'http'])('新建 %s 只提交对应字段，切换保留脚本草稿', async kind => {
    vi.mocked(apiRequest).mockImplementation(async url => String(url).startsWith('/api/connections') ? { catalog: [], connections: [] } : { hooks: [], total: 0 });
    vi.mocked(apiJson).mockResolvedValue({ hook_id: 'personal:test', version: 'v', revision: 'r', visibility: 'private' });
    render(<HookManagerPage />);
    fireEvent.click(screen.getByRole('button', { name: '新建执行钩子' }));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '按类型保存' } });
    fireEvent.change(screen.getByLabelText('执行方式'), { target: { value: 'command' } });
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.change(screen.getByLabelText('脚本 hook.py'), { target: { value: '# 保留草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '上一步' }));
    fireEvent.click(screen.getByRole('button', { name: '上一步' }));
    fireEvent.change(screen.getByLabelText('执行方式'), { target: { value: kind } });
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.change(screen.getByLabelText(kind === 'prompt' ? '提示正文' : 'HTTP 地址'), { target: { value: kind === 'prompt' ? '检查' : 'https://example.test/' } });
    fireEvent.click(screen.getByRole('button', { name: '保存钩子' }));
    await screen.findByRole('button', { name: '测试' });
    const body = vi.mocked(apiJson).mock.calls[0][2] as any;
    expect(body.files).toEqual({});
    expect(body.definition).not.toHaveProperty('argv');
    expect(body.definition).not.toHaveProperty(kind === 'prompt' ? 'url' : 'content');
    if (kind === 'prompt') expect(body.definition).not.toHaveProperty('credential_headers');
    fireEvent.change(screen.getByLabelText('执行方式'), { target: { value: 'command' } });
    expect(screen.getByLabelText('脚本 hook.py')).toHaveValue('# 保留草稿');
    expect(screen.getByLabelText('解释器')).toHaveValue('python3');
  });

  it('迟到的轮询不能覆盖取消结果', async () => {
    let resolvePoll: ((value: unknown) => void) | undefined;
    vi.mocked(apiRequest).mockImplementation(async url => {
      if (String(url).startsWith('/api/hook-tests/')) return new Promise(resolve => { resolvePoll = resolve; });
      if (String(url).startsWith('/api/workspace')) return { roots: [], directories: [], selected: '' };
      return { hooks: [], total: 0 };
    });
    vi.mocked(apiJson).mockImplementation(async url => String(url).endsWith('/cancel') ? { test_id: 't', status: 'cancelled' } : String(url).endsWith('/tests') ? { test_id: 't', status: 'running' } : { hook_id: 'personal:t', version: 'v', revision: 'r', visibility: 'private' });
    render(<HookManagerPage />);
    fireEvent.click(screen.getByRole('button', { name: '新建执行钩子' }));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '测试取消' } });
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.change(screen.getByLabelText('提示正文'), { target: { value: '检查' } });
    fireEvent.click(screen.getByRole('button', { name: '保存钩子' }));
    fireEvent.click(await screen.findByRole('button', { name: '测试' }));
    expect((vi.mocked(apiJson).mock.calls[0][2] as any).definition).not.toHaveProperty('argv');
    fireEvent.click(screen.getByRole('button', { name: '运行测试' }));
    await waitFor(() => expect(resolvePoll).toBeDefined(), { timeout: 2000 });
    fireEvent.click(screen.getByRole('button', { name: '取消测试' }));
    await screen.findByText('状态：已取消');
    resolvePoll!({ test_id: 't', status: 'running' });
    await waitFor(() => expect(screen.getByText('状态：已取消')).toBeInTheDocument());
  });

  it('向导校验名称，后退保留内容，HTTP 无效地址不提交', async () => {
    render(<HookManagerPage />);
    fireEvent.click(screen.getByRole('button', { name: '新建执行钩子' }));
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    expect(screen.getByRole('alert')).toHaveTextContent('填写名称');
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '通知' } });
    fireEvent.change(screen.getByLabelText('执行方式'), { target: { value: 'http' } });
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.change(screen.getByLabelText('触发时机'), { target: { value: 'tool.after' } });
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.change(screen.getByLabelText('HTTP 地址'), { target: { value: 'invalid' } });
    fireEvent.click(screen.getByRole('button', { name: '上一步' }));
    expect(screen.getByLabelText('触发时机')).toHaveValue('tool.after');
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    expect(screen.getByLabelText('HTTP 地址')).toHaveValue('invalid');
    fireEvent.click(screen.getByRole('button', { name: '保存钩子' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('有效的 HTTP');
    expect(apiJson).not.toHaveBeenCalled();
  });

  it('编辑旧命令保留参数与高级设置，归档展示引用数量', async () => {
    const item = { hook_id: 'personal:old', name: '旧命令', visibility: 'private', archived: false, revision: 'r', version: 'v', files: {}, definition: { name: '旧命令', description: '原配置', plugin_type: 'command', hook_type: 'tool.after', argv: ['/bin/echo', 'a b', '--flag'], parameters: {}, applies_to_tools: ['read_file'], order: 42, timeout_seconds: 17, on_error: 'continue', credential_headers: {} } };
    vi.mocked(apiRequest).mockImplementation(async url => String(url).endsWith('/references') ? { agents: [{ name: '我的 Agent', agent_id: 'a' }] } : String(url).includes('/personal:old') ? item : { hooks: [item], total: 1 });
    vi.mocked(apiJson).mockResolvedValue(item);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<HookManagerPage />);
    fireEvent.click(await screen.findByRole('button', { name: /旧命令/ }));
    await screen.findByLabelText('名称');
    fireEvent.change(screen.getByLabelText('用途'), { target: { value: '新用途' } });
    fireEvent.click(screen.getByRole('button', { name: '保存钩子' }));
    await waitFor(() => expect(apiJson).toHaveBeenCalled());
    expect((vi.mocked(apiJson).mock.calls[0][2] as any).definition).toMatchObject({ argv: ['/bin/echo', 'a b', '--flag'], order: 42, timeout_seconds: 17, on_error: 'continue', applies_to_tools: ['read_file'] });
    fireEvent.click(screen.getByLabelText('钩子更多操作'));
    fireEvent.click(screen.getByRole('button', { name: '归档' }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('1 个 Agent'));
    confirm.mockRestore();
  });

  it('Prompt 限定节点并在保存冲突后保留草稿', async () => {
    vi.mocked(apiJson).mockRejectedValue(new Error('版本冲突'));
    render(<HookManagerPage />);
    fireEvent.click(screen.getByRole('button', { name: '新建执行钩子' }));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '检查' } });
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    expect(screen.queryByRole('option', { name: '模型响应后' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.change(screen.getByLabelText('提示正文'), { target: { value: '检查所有变更' } });
    fireEvent.click(screen.getByRole('button', { name: '保存钩子' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('版本冲突');
    expect(screen.getByLabelText('提示正文')).toHaveValue('检查所有变更');
  });

  it('脚本和定义一次提交，参数保持数组', async () => {
    vi.mocked(apiJson).mockResolvedValue({ hook_id: 'personal:test', version: 'v', revision: 'r', visibility: 'private' });
    vi.mocked(apiRequest).mockImplementation(async (url) => String(url).startsWith('/api/workspace')
      ? { roots: [], directories: [], selected: '' } : { hooks: [], total: 0 });
    render(<HookManagerPage />);
    fireEvent.click(screen.getByRole('button', { name: '新建执行钩子' }));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '命令检查' } });
    fireEvent.change(screen.getByLabelText('执行方式'), { target: { value: 'command' } });
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    expect((screen.getByLabelText('脚本 hook.py') as HTMLTextAreaElement).value).toContain('"action": "continue"');
    fireEvent.change(screen.getByLabelText('脚本 hook.py'), { target: { value: 'print("hello")' } });
    fireEvent.click(screen.getByRole('button', { name: '保存钩子' }));
    await waitFor(() => expect(apiJson).toHaveBeenCalled());
    const body = vi.mocked(apiJson).mock.calls[0][2] as { definition: { argv: string[] }; files: Record<string, string> };
    expect(body.definition.argv).toEqual(['python3', '@file:hook.py']);
    expect(atob(body.files['hook.py'])).toBe('print("hello")');
    fireEvent.click(await screen.findByRole('button', { name: '测试' }));
    await screen.findByRole('button', { name: '运行测试' });
  });

  it('私人 HTTP 连接不显示团队回退身份', async () => {
    const { ConnectionEditor } = await import('./ConnectionEditor');
    vi.mocked(apiRequest).mockResolvedValue({ catalog: [{ target: 'hook:personal:test', fields: ['auth'], kind: 'hook', personal: true }], connections: [] });
    render(<ConnectionEditor selected={[]} disabled={false} onChange={vi.fn()} targetFilter="hook:personal:test" />);
    await screen.findByText('hook:personal:test');
    expect(screen.queryByRole('option', { name: '团队身份' })).not.toBeInTheDocument();
  });
});
