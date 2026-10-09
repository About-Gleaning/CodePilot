import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { AgentErrorDetails } from './AgentErrorDetails';

describe('Agent 错误说明', () => {
  afterEach(cleanup);
  it('展示具体原因和建议，技术码折叠', () => {
    render(<AgentErrorDetails message="无法启动 Agent" code="agent_invalid" issues={[{ code: 'agent_revision_mismatch', message: '配置版本校验失败', suggestion: '请修复配置文件后重新加载。' }]} />);
    expect(screen.getByText('配置版本校验失败')).toBeTruthy();
    expect(screen.getByText('请修复配置文件后重新加载。')).toBeTruthy();
    expect(screen.getByText('技术详情').parentElement?.hasAttribute('open')).toBe(false);
  });
  it('兼容没有问题列表的旧错误', () => {
    render(<AgentErrorDetails message="请求失败" />);
    expect(screen.getByText('请求失败')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
  });
});
