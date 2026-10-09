export type StudioRoute =
  | { kind: 'agents' }
  | { kind: 'agent'; agentId: string; sessionId: string | null; newSession: boolean }
  | { kind: 'agent-config'; agentId: string | null }
  | { kind: 'resource'; resource: 'skills' | 'hooks' | 'publications' | 'tools' }
  | { kind: 'not-found' };

export function readStudioRoute(hash = window.location.hash): StudioRoute {
  const path = hash.replace(/^#/, '').replace(/\/+$/, '') || '/agents';
  if (path === '/studio' || path === '/agents') return { kind: 'agents' };
  if (path === '/agents/new') return { kind: 'agent-config', agentId: null };
  if (path === '/skills' || path === '/hooks' || path === '/publications' || path === '/tools') {
    return { kind: 'resource', resource: path.slice(1) as 'skills' | 'hooks' | 'publications' | 'tools' };
  }
  const parts = path.split('/').filter(Boolean);
  if (parts[0] !== 'agents' || !parts[1]) return { kind: 'not-found' };
  const agentId = decode(parts[1]);
  if (!agentId) return { kind: 'not-found' };
  if (parts.length === 2) return { kind: 'agent', agentId, sessionId: null, newSession: false };
  if (parts.length === 3 && parts[2] === 'new') return { kind: 'agent', agentId, sessionId: null, newSession: true };
  if (parts.length === 3 && parts[2] === 'config') return { kind: 'agent-config', agentId };
  if (parts.length === 4 && parts[2] === 'sessions') {
    const sessionId = decode(parts[3]);
    return sessionId ? { kind: 'agent', agentId, sessionId, newSession: false } : { kind: 'not-found' };
  }
  return { kind: 'not-found' };
}

export function routeHash(route: Exclude<StudioRoute, { kind: 'not-found' }>) {
  if (route.kind === 'agents') return '#/agents';
  if (route.kind === 'resource') return `#/${route.resource}`;
  if (route.kind === 'agent-config') return route.agentId ? `#/agents/${encodeURIComponent(route.agentId)}/config` : '#/agents/new';
  if (route.newSession) return `#/agents/${encodeURIComponent(route.agentId)}/new`;
  if (route.sessionId) return `#/agents/${encodeURIComponent(route.agentId)}/sessions/${encodeURIComponent(route.sessionId)}`;
  return `#/agents/${encodeURIComponent(route.agentId)}`;
}

export function navigateStudio(route: Exclude<StudioRoute, { kind: 'not-found' }>, replace = false) {
  const hash = routeHash(route);
  if (window.location.hash === hash) return;
  if (replace) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${hash}`);
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    window.location.hash = hash.slice(1);
  }
}

function decode(value: string) {
  try { return decodeURIComponent(value); } catch { return ''; }
}
