import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiJson, apiRequest } from '../../api/client';
import { MemoryEditor } from './MemoryEditor';
import { SkillManagerPage } from './SkillManagerPage';
import { ConnectionEditor } from './ConnectionEditor';

vi.mock('../../api/client', () => ({ apiJson: vi.fn(), apiRequest: vi.fn() }));

beforeEach(() => { vi.resetAllMocks(); });
afterEach(cleanup);

describe('组装资源编辑', () => {
  it('记忆冲突保留草稿，并提交读取时的版本', async () => {
    vi.mocked(apiRequest).mockResolvedValue({ content: '原记忆', revision: '版本一' });
    vi.mocked(apiJson).mockRejectedValue(new Error('记忆已更新'));
    render(<MemoryEditor agentId="agent-a" />);
    const input = await screen.findByDisplayValue('原记忆');
    fireEvent.change(input, { target: { value: '我的修改' } });
    fireEvent.click(screen.getByRole('button', { name: '保存记忆' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('记忆已更新');
    expect(input).toHaveValue('我的修改');
    expect(apiJson).toHaveBeenCalledWith('/api/memories/agent-a', 'PUT', { content: '我的修改', expected_revision: '版本一' });
  });

  it('Skill 保存失败保留完整正文', async () => {
    vi.mocked(apiRequest).mockResolvedValue({ skills: [], shared: [] });
    vi.mocked(apiJson).mockRejectedValue(new Error('版本发布失败'));
    render(<SkillManagerPage />);
    fireEvent.click(screen.getByRole('button', { name: '新建技能' }));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'review' } });
    fireEvent.change(screen.getByLabelText('用途'), { target: { value: '代码检查' } });
    fireEvent.change(screen.getByRole('textbox', { name: '技能正文' }), { target: { value: '新正文' } });
    fireEvent.click(screen.getByRole('button', { name: '保存技能' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('版本发布失败');
    expect(screen.getByRole('textbox', { name: '技能正文' })).toHaveValue('新正文');
  });

  it('个人连接凭证只用于提交，成功后关闭输入', async () => {
    vi.mocked(apiRequest).mockResolvedValue({ catalog: [{ target: 'mcp:test', fields: ['TOKEN'], kind: 'mcp' }], connections: [] });
    vi.mocked(apiJson).mockResolvedValue({});
    render(<ConnectionEditor selected={[]} disabled={false} onChange={vi.fn()} />);
    await screen.findByText('mcp:test');
    fireEvent.click(screen.getByRole('button', { name: '新增个人连接' }));
    fireEvent.change(screen.getByLabelText('TOKEN'), { target: { value: '测试凭证' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));
    await waitFor(() => expect(screen.queryByLabelText('TOKEN')).not.toBeInTheDocument());
    expect(apiJson).toHaveBeenCalledWith('/api/connections', 'POST', { target: 'mcp:test', credentials: { TOKEN: '测试凭证' }, expected_revision: undefined });
  });
});
