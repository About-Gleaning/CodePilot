import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiJson, apiRequest } from '../../api/client';
import { SkillManagerPage } from './SkillManagerPage';

vi.mock('../../api/client', () => ({ apiJson: vi.fn(), apiRequest: vi.fn() }));
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiRequest).mockResolvedValue({ skills: [], shared: [], agents: [] }); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('技能独立管理流程', () => {
  it('保存停留详情，引用只读且可返回；非法源码禁止覆盖', async () => {
    const back = vi.fn();
    vi.mocked(apiRequest).mockResolvedValue({ skills: [], shared: [], agents: [{ agent_id: 'a', name: '我的 Agent' }] });
    vi.mocked(apiJson).mockResolvedValue({ skill_id: 's', name: 'review', revision: 'r', visibility: 'private', archived: false });
    render(<SkillManagerPage onReturn={back} />);
    fireEvent.click(screen.getByRole('button', { name: '新建技能' }));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'review' } });
    fireEvent.change(screen.getByLabelText('用途'), { target: { value: '审查' } });
    fireEvent.change(screen.getByLabelText('技能正文'), { target: { value: '正文' } });
    fireEvent.click(screen.getByRole('button', { name: '保存技能' }));
    await screen.findByRole('heading', { name: 'review' });
    expect(screen.getByLabelText('技能正文')).toHaveValue('正文');
    fireEvent.click(screen.getByRole('button', { name: '引用情况' }));
    await screen.findByText('我的 Agent');
    expect(screen.queryByLabelText('选择要配置的 Agent')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '配置 Agent' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回 Agent 配置' }));
    expect(back).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: '技能内容' }));
    fireEvent.change(screen.getByLabelText('SKILL.md 源码'), { target: { value: '无效源码' } });
    expect(screen.getByRole('button', { name: '保存技能' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('缺少有效');
    expect(apiJson).toHaveBeenCalledTimes(1);
  });

  it('共享技能只读，复制后允许编辑且不带原身份', async () => {
    const record = { skill_id: 'shared:review', name: 'review', description: '审查', revision: 'r', visibility: 'shared', archived: false, files: ['SKILL.md'] };
    vi.mocked(apiRequest).mockImplementation(async url => String(url).endsWith('/references') ? { agents: [] } : String(url).includes('/skills/shared:') ? record : { skills: [], shared: [record] });
    const bytes = new TextEncoder().encode('---\nname: review\ndescription: 审查\ncustom: preserved\n---\n正文');
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, arrayBuffer: async () => bytes.buffer })));
    vi.mocked(apiJson).mockResolvedValue({ ...record, skill_id: 'personal', visibility: 'private' });
    render(<SkillManagerPage />);
    fireEvent.click(await screen.findByRole('button', { name: /review/ }));
    await waitFor(() => expect(screen.getByLabelText('技能正文')).toBeDisabled());
    expect(screen.queryByRole('button', { name: '保存技能' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('技能更多操作'));
    fireEvent.click(screen.getByRole('button', { name: '复制为我的技能' }));
    expect(screen.getByLabelText('技能正文')).not.toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存技能' }));
    await waitFor(() => expect(apiJson).toHaveBeenCalled());
    expect(vi.mocked(apiJson).mock.calls[0][0]).toBe('/api/skills');
    expect(vi.mocked(apiJson).mock.calls[0][1]).toBe('POST');
  });
});
