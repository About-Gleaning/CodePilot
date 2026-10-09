import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiJson, apiRequest } from '../../api/client';
import { ToolManagerPage } from './ToolManagerPage';
import { CodeToolPicker } from './CodeToolPicker';

vi.mock('../../api/client', () => ({ apiJson: vi.fn(), apiRequest: vi.fn() }));
const record = { tool_id: 'personal:demo', name: '查询工具', description: '查询信息', visibility: 'private', revision: 'r', version: 'v', definition: { name: '查询工具', description: '查询信息', input_schema: { type: 'object', properties: {} }, entrypoint: 'main.py', timeout_seconds: 30, credential_fields: [] }, files: { 'main.py': 'def execute(arguments):\n    return {}' } };
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiRequest).mockImplementation(async url => String(url).includes('personal:demo') ? record : { tools: [record], total: 1 }); });
afterEach(cleanup);

it('编辑保存工具，不把代码当成内置工具配置', async () => {
  vi.mocked(apiJson).mockResolvedValue(record);
  render(<ToolManagerPage />);
  fireEvent.click(await screen.findByRole('button', { name: /查询工具/ }));
  fireEvent.change(await screen.findByLabelText('Python 代码'), { target: { value: 'def execute(arguments):\n    return {"ok": True}' } });
  expect(screen.getByRole('button', { name: '运行测试' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '保存工具' }));
  await waitFor(() => expect(apiJson).toHaveBeenCalledWith('/api/tools/personal:demo', 'PUT', expect.objectContaining({ expected_revision: 'r', files: { 'main.py': 'def execute(arguments):\n    return {"ok": True}' } })));
});

it('公共工具只读，普通使用者不能撤回', async () => {
  const item = { ...record, tool_id: 'public:demo', visibility: 'public', can_manage: false };
  vi.mocked(apiRequest).mockImplementation(async url => String(url).includes('public:demo') ? item : { tools: [item], total: 1 });
  render(<ToolManagerPage />);
  fireEvent.change(screen.getByLabelText('工具来源'), { target: { value: 'public' } });
  fireEvent.click(await screen.findByRole('button', { name: /查询工具/ }));
  expect(await screen.findByLabelText('Python 代码')).toBeDisabled();
  expect(screen.queryByRole('button', { name: '保存工具' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '撤回发布' })).not.toBeInTheDocument();
});

it('绑定与移除使用稳定工具 ID，保留分页外已有绑定', async () => {
  const change = vi.fn();
  render(<CodeToolPicker selected={['public:existing']} disabled={false} connections={null} onChange={change} onConnections={vi.fn()} />);
  fireEvent.click(await screen.findByRole('checkbox'));
  expect(change).toHaveBeenCalledWith(['public:existing', 'personal:demo']);
  fireEvent.click(screen.getByRole('button', { name: '移除' }));
  expect(change).toHaveBeenCalledWith([]);
});

it('资源页隐藏后保留详情，不触发代码执行', async () => {
  let resolveFirst: ((value: unknown) => void) | undefined;
  vi.mocked(apiRequest).mockImplementation(async url => {
    if (String(url).endsWith('/personal:demo')) return new Promise(resolve => { resolveFirst = resolve; });
    return { tools: [record], total: 1 };
  });
  const { rerender } = render(<ToolManagerPage />);
  fireEvent.click(await screen.findByRole('button', { name: /查询工具/ }));
  // 跨资源页保留当前草稿，不因隐藏触发新选择或执行代码。
  rerender(<ToolManagerPage active={false} />);
  resolveFirst?.(record);
  await screen.findByLabelText('Python 代码');
  expect(apiJson).not.toHaveBeenCalled();
});

it('已有工具填写调用名称后保存，并在发布预览明确展示', async () => {
  vi.mocked(apiJson).mockImplementation(async (_url, _method, data) => {
    if (String(_url).endsWith('publication-preview')) return { token: 'preview', tool_id: 'public:demo', call_name: 'test_tool', definition: { ...record.definition, call_name: 'test_tool' }, files: record.files };
    return { ...record, call_name: 'test_tool', definition: (data as { definition: object }).definition };
  });
  render(<ToolManagerPage />);
  fireEvent.click(await screen.findByRole('button', { name: /查询工具/ }));
  fireEvent.change(await screen.findByLabelText('调用名称'), { target: { value: 'test_tool' } });
  fireEvent.click(screen.getByRole('button', { name: '保存工具' }));
  await waitFor(() => expect(apiJson).toHaveBeenCalledWith('/api/tools/personal:demo', 'PUT', expect.objectContaining({ definition: expect.objectContaining({ name: '查询工具', call_name: 'test_tool' }) })));
  fireEvent.click(await screen.findByRole('button', { name: '预览发布' }));
  expect(await screen.findByText('调用名称：test_tool')).toBeInTheDocument();
});

it('新建工具必须填写合法调用名称', async () => {
  vi.mocked(apiJson).mockResolvedValue(record);
  render(<ToolManagerPage />);
  fireEvent.click(screen.getByRole('button', { name: '新建工具' }));
  fireEvent.change(screen.getByLabelText('工具名称'), { target: { value: '查询工具' } });
  fireEvent.click(screen.getByRole('button', { name: '保存工具' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('调用名称');
  expect(apiJson).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('调用名称'), { target: { value: 'mcp__demo' } });
  fireEvent.click(screen.getByRole('button', { name: '保存工具' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('保留前缀'));
  expect(apiJson).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('调用名称'), { target: { value: 'test_tool' } });
  fireEvent.click(screen.getByRole('button', { name: '保存工具' }));
  await waitFor(() => expect(apiJson).toHaveBeenCalledWith('/api/tools', 'POST', expect.objectContaining({ definition: expect.objectContaining({ call_name: 'test_tool' }) })));
});

it('Agent 选择项展示调用名称，绑定仍使用工具 ID', async () => {
  vi.mocked(apiRequest).mockResolvedValue({ tools: [{ ...record, call_name: 'test_tool' }], total: 1 });
  const change = vi.fn();
  render(<CodeToolPicker selected={[]} disabled={false} connections={null} onChange={change} onConnections={vi.fn()} />);
  expect(await screen.findByText(/调用名称：test_tool/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox'));
  expect(change).toHaveBeenCalledWith(['personal:demo']);
});
