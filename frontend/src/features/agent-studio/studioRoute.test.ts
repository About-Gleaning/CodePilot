import { describe, expect, it } from 'vitest';
import { readStudioRoute, routeHash } from './studioRoute';

describe('studioRoute', () => {
  it('解析总览、Agent、会话和配置地址', () => {
    expect(readStudioRoute('#/studio')).toEqual({ kind: 'agents' });
    expect(readStudioRoute('#/agents/build')).toEqual({ kind: 'agent', agentId: 'build', sessionId: null, newSession: false });
    expect(readStudioRoute('#/agents/build/new')).toEqual({ kind: 'agent', agentId: 'build', sessionId: null, newSession: true });
    expect(readStudioRoute('#/agents/build/sessions/session-1')).toEqual({ kind: 'agent', agentId: 'build', sessionId: 'session-1', newSession: false });
    expect(readStudioRoute('#/agents/build/config')).toEqual({ kind: 'agent-config', agentId: 'build' });
  });

  it('编码动态身份并拒绝未知路径', () => {
    expect(routeHash({ kind: 'agent', agentId: 'agent/a', sessionId: 'session b', newSession: false })).toBe('#/agents/agent%2Fa/sessions/session%20b');
    expect(readStudioRoute('#/unknown')).toEqual({ kind: 'not-found' });
    expect(readStudioRoute('#/agents/%E0%A4%A')).toEqual({ kind: 'not-found' });
  });
});
