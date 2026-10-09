import { useMemo, useState } from 'react';
import { ArrowRight, Bot, Clock3, Play, Plus, Search, Settings2 } from 'lucide-react';

import type { AgentRuntime, AgentSummary, SessionSummary } from './types';

type Props = {
  agents: AgentSummary[];
  runtimes: Record<string, AgentRuntime>;
  recentSessions: Record<string, SessionSummary>;
  mutatingAgentIds: Set<string>;
  onCreate: () => void;
  onOpen: (agentId: string, sessionId: string | null) => void;
  onConfigure: (agentId: string) => void;
  onStart: (agentId: string) => void;
};

type AgentTone = 'waiting' | 'running' | 'error' | 'idle' | 'stopped' | 'delegated';

export function AgentTaskBoard({
  agents,
  runtimes,
  recentSessions,
  mutatingAgentIds,
  onCreate,
  onOpen,
  onConfigure,
  onStart,
}: Props) {
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<'active' | 'archived' | 'all'>('active');
  const directAgents = useMemo(() => agents.filter(supportsDirect), [agents]);
  const visibleAgents = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase('zh-CN');
    return agents
      .filter((agent) => scope === 'all' || (scope === 'archived' ? agent.archived : !agent.archived))
      .filter((agent) => !normalized || `${agent.name} ${agent.description || ''}`.toLocaleLowerCase('zh-CN').includes(normalized))
      .sort((left, right) => {
        const modePriority = Number(!supportsDirect(left)) - Number(!supportsDirect(right));
        if (modePriority) return modePriority;
        const priority = tonePriority(agentTone(left, runtimes[left.agent_id])) - tonePriority(agentTone(right, runtimes[right.agent_id]));
        if (priority) return priority;
        const leftTime = recentSessions[left.agent_id]?.updated_at || '';
        const rightTime = recentSessions[right.agent_id]?.updated_at || '';
        return rightTime.localeCompare(leftTime) || left.name.localeCompare(right.name, 'zh-CN');
      });
  }, [agents, query, recentSessions, runtimes, scope]);

  const waiting = directAgents.filter((agent) => runtimes[agent.agent_id]?.waiting_human_count).length;
  const running = directAgents.filter((agent) => runtimes[agent.agent_id]?.active_run_count).length;

  return (
    <section className="agent-overview" aria-label="Agent 总览">
      <header className="agent-overview-header">
        <div>
          <span className="eyebrow">AGENT DIRECTORY</span>
          <h1>Agent</h1>
          <p><strong>{agents.length}</strong> 个 Agent · <b>{waiting}</b> 个等待处理 · <b>{running}</b> 个正在执行</p>
        </div>
        <button type="button" className="studio-button primary" onClick={onCreate}><Plus size={15} />新建 Agent</button>
      </header>

      <div className="agent-overview-toolbar">
        <label className="agent-overview-search">
          <Search size={15} aria-hidden="true" />
          <span className="sr-only">搜索 Agent</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或职责" />
        </label>
        <label className="agent-overview-scope">
          <span>显示</span>
          <select aria-label="Agent 范围" value={scope} onChange={(event) => setScope(event.target.value as typeof scope)}>
            <option value="active">当前 Agent</option>
            <option value="archived">已归档</option>
            <option value="all">全部</option>
          </select>
        </label>
      </div>

      <div className="agent-module-grid">
        {visibleAgents.map((agent) => {
          const direct = supportsDirect(agent);
          const runtime = direct ? runtimes[agent.agent_id] || stoppedRuntime(agent.agent_id) : undefined;
          const session = direct ? recentSessions[agent.agent_id] : undefined;
          return (
            <AgentModule
              key={agent.agent_id}
              agent={agent}
              runtime={runtime}
              session={session}
              mutating={mutatingAgentIds.has(agent.agent_id)}
              onOpen={() => runtime && onOpen(agent.agent_id, session?.session_id || runtime.recent_session_id)}
              onConfigure={() => onConfigure(agent.agent_id)}
              onStart={() => onStart(agent.agent_id)}
            />
          );
        })}
      </div>
      {!visibleAgents.length ? <div className="agent-overview-empty"><Bot size={24} /><strong>没有符合条件的 Agent</strong><span>调整搜索或显示范围。</span></div> : null}
    </section>
  );
}

function AgentModule({ agent, runtime, session, mutating, onOpen, onConfigure, onStart }: {
  agent: AgentSummary;
  runtime?: AgentRuntime;
  session?: SessionSummary;
  mutating: boolean;
  onOpen: () => void;
  onConfigure: () => void;
  onStart: () => void;
}) {
  const direct = supportsDirect(agent);
  const tone = agentTone(agent, runtime);
  const waiting = Boolean(runtime?.waiting_human_count);
  const canStart = direct && !agent.archived && runtime?.lifecycle_state !== 'RUNNING' && runtime?.lifecycle_state !== 'STARTING';
  const configReadonly = isConfigReadonly(agent);
  const configAction = configReadonly ? '查看' : '配置';
  return (
    <article className={`agent-module tone-${tone}`}>
      <button type="button" className="agent-module-main" onClick={direct ? onOpen : onConfigure} aria-label={direct ? `打开 ${agent.name}` : `打开 ${agent.name} 配置`}>
        <header>
          <span className="agent-module-index" aria-hidden="true">{agent.name.slice(0, 2).toUpperCase()}</span>
          <span className="agent-module-identity">
            <strong>{agent.name}</strong>
            <small>{agent.description || '未填写 Agent 职责'}</small>
          </span>
          <span className={`agent-status-label tone-${tone}`}><i />{agentStatusLabel(agent, runtime)}</span>
        </header>
        {direct && runtime ? <div className="agent-module-task">
          <span className="agent-module-kicker">最新任务</span>
          {session ? (
            <>
              <strong>{session.title || session.preview || '未命名任务'}</strong>
              <p>{session.preview || '该任务暂无目标摘要。'}</p>
              <footer>
                <span>{taskStatusLabel(session, runtime)}</span>
                <span><Clock3 size={12} />{formatDate(session.updated_at)}</span>
                <span>{session.source === 'schedule' ? '自动化' : '网页'}</span>
              </footer>
            </>
          ) : (
            <div className="agent-module-no-task"><strong>暂无任务</strong><span>进入 Agent 创建第一个会话。</span></div>
          )}
        </div> : <div className="agent-module-task agent-module-delegated">
          <span className="agent-module-kicker">运行配置</span>
          <strong>Agent 委派</strong>
          <p>{agent.default_provider || '-'} / {agent.default_model || '-'}</p>
          <footer><span>{agent.readonly ? '只读执行' : '按授权执行'}</span><span>独立上下文</span></footer>
        </div>}
      </button>
      <footer className="agent-module-actions">
        <span>{agent.visibility === 'shared' ? '共享' : agent.source === 'builtin' ? '内置' : '个人'} · {agent.validation_status === 'valid' ? '配置可用' : '需要检查'}</span>
        <div>
          {direct ? <>
            <button type="button" className="studio-icon-button" onClick={onConfigure} aria-label={`配置 ${agent.name}`} title="配置 Agent"><Settings2 size={14} /></button>
            {canStart ? <button type="button" className="agent-module-action" disabled={mutating} onClick={onStart}><Play size={13} />{mutating ? '启动中' : '启动'}</button> : null}
            <button type="button" className={`agent-module-action ${waiting ? 'is-attention' : ''}`} onClick={onOpen}>{waiting ? '处理' : session ? '进入' : '新任务'}<ArrowRight size={13} /></button>
          </> : <button type="button" className="agent-module-action" onClick={onConfigure} aria-label={`${configAction} ${agent.name}`}><Settings2 size={13} />{configAction}</button>}
        </div>
      </footer>
    </article>
  );
}

function agentTone(agent: AgentSummary, runtime?: AgentRuntime): AgentTone {
  if (agent.validation_status === 'invalid' || agent.validation_status === 'needs_configuration' || runtime?.lifecycle_state === 'ERROR') return 'error';
  if (!supportsDirect(agent)) return 'delegated';
  if (runtime?.waiting_human_count) return 'waiting';
  if (runtime?.active_run_count || runtime?.lifecycle_state === 'STARTING') return 'running';
  if (runtime?.lifecycle_state === 'RUNNING') return 'idle';
  return 'stopped';
}

function tonePriority(tone: AgentTone) {
  return { waiting: 0, running: 1, error: 2, idle: 3, stopped: 4, delegated: 5 }[tone];
}

function agentStatusLabel(agent: AgentSummary, runtime?: AgentRuntime) {
  if (agent.validation_status === 'invalid' || agent.validation_status === 'needs_configuration') return '配置异常';
  if (!supportsDirect(agent)) return '仅委派';
  if (runtime?.lifecycle_state === 'ERROR') return '运行异常';
  if (runtime?.waiting_human_count) return '等待处理';
  if (runtime?.active_run_count) return '正在执行';
  if (runtime?.lifecycle_state === 'STARTING') return '启动中';
  if (runtime?.lifecycle_state === 'RUNNING') return '可用';
  if (runtime?.lifecycle_state === 'STOPPING') return '正在停止';
  return '已停止';
}

function supportsDirect(agent: AgentSummary) {
  return (agent.launch_modes || (agent.kind === 'subagent' ? ['delegated'] : ['direct'])).includes('direct');
}

function isConfigReadonly(agent: AgentSummary) {
  return (agent.visibility || (agent.source === 'builtin' ? 'builtin' : 'private')) !== 'private';
}

function taskStatusLabel(session: SessionSummary, runtime: AgentRuntime) {
  if (runtime.recent_session_id === session.session_id) {
    if (runtime.waiting_human_count) return '任务等待处理';
    if (runtime.active_run_count) return '任务执行中';
  }
  const labels: Record<string, string> = {
    STARTING: '任务准备中', RUNNING: '任务执行中', WAITING_HUMAN: '任务等待处理',
    COMPLETED: '任务已完成', FAILED: '任务失败', CANCELLED: '任务已停止', IDLE: '任务空闲',
  };
  return labels[session.status] || session.status || '状态未知';
}

function formatDate(value: string) {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function stoppedRuntime(agentId: string): AgentRuntime {
  return { agent_id: agentId, desired_state: 'STOPPED', lifecycle_state: 'STOPPED', recent_session_id: null, active_run_count: 0, waiting_human_count: 0, error_code: null };
}
