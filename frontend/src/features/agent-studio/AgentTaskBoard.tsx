import { FormEvent, useEffect, useRef, useState } from 'react';
import { Activity, Bot, Eye, Pin, Play, Send, Square, X } from 'lucide-react';

import { apiRequest } from '../../api/client';
import type { MessageRecord } from '../../types';
import type { AgentRuntime, AgentSummary, ReplayResponse, SessionSummary } from './types';

export type CardSnapshot = {
  sessionId: string | null;
  result: string;
  lastTool: string | null;
  updatedAt: string | null;
};

type Props = {
  agents: AgentSummary[];
  runtimes: Record<string, AgentRuntime>;
  snapshots: Record<string, CardSnapshot>;
  pinnedAgentIds: string[];
  sendingAgentIds: Set<string>;
  onOpen: (agentId: string, sessionId: string | null) => void;
  onTogglePin: (agentId: string) => void;
  onSend: (agentId: string, content: string) => Promise<void>;
  onStart: (agentId: string) => void;
  onStop: (agent: AgentSummary) => void;
};

const LANES = [
  { id: 'waiting', label: '等待处理', description: '需要你的审批或回答' },
  { id: 'running', label: '执行中', description: 'Agent 正在推进任务' },
  { id: 'idle', label: '空闲', description: '可立即接收新任务' },
  { id: 'stopped', label: '异常 / 已停止', description: '需要启动或检查配置' },
] as const;

export function AgentTaskBoard(props: Props) {
  const ordered = [...props.agents].sort((left, right) => {
    const pin = Number(props.pinnedAgentIds.includes(right.agent_id)) - Number(props.pinnedAgentIds.includes(left.agent_id));
    return pin || left.name.localeCompare(right.name, 'zh-CN');
  });
  return (
    <section className="task-board" aria-label="Agent 任务看板">
      <header className="task-board-header">
        <div>
          <span className="eyebrow">MULTI-AGENT COMMAND</span>
          <h1>任务看板</h1>
          <p>在卡片内独立派发任务，打开详情查看实时过程与人工交互。</p>
        </div>
        <div className="task-board-legend" aria-label="看板说明">
          <span><Activity size={13} />全局状态实时同步</span>
          <span><Eye size={13} />详情保留唯一实时流</span>
        </div>
      </header>
      <div className="task-board-lanes">
        {LANES.map((lane) => {
          const agents = ordered.filter((agent) => laneFor(props.runtimes[agent.agent_id]) === lane.id);
          return (
            <section className={`task-lane lane-${lane.id} ${agents.length ? '' : 'is-empty'}`} key={lane.id} aria-label={lane.label}>
              <header><div><span className="eyebrow">{String(agents.length).padStart(2, '0')}</span><h2>{lane.label}</h2></div><small>{lane.description}</small></header>
              <div className="task-lane-cards">
                {agents.map((agent) => <AgentWorkCard
                  key={agent.agent_id}
                  agent={agent}
                  runtime={props.runtimes[agent.agent_id] || stoppedRuntime(agent.agent_id)}
                  snapshot={props.snapshots[agent.agent_id]}
                  pinned={props.pinnedAgentIds.includes(agent.agent_id)}
                  sending={props.sendingAgentIds.has(agent.agent_id)}
                  onOpen={() => props.onOpen(agent.agent_id, props.snapshots[agent.agent_id]?.sessionId || props.runtimes[agent.agent_id]?.recent_session_id || null)}
                  onTogglePin={() => props.onTogglePin(agent.agent_id)}
                  onSend={(content) => props.onSend(agent.agent_id, content)}
                  onStart={() => props.onStart(agent.agent_id)}
                  onStop={() => props.onStop(agent)}
                />)}
                {!agents.length ? <p className="task-lane-empty">暂无 Agent</p> : null}
              </div>
            </section>
          );
        })}
      </div>
    </section>
  );
}

function AgentWorkCard({ agent, runtime, snapshot, pinned, sending, onOpen, onTogglePin, onSend, onStart, onStop }: {
  agent: AgentSummary;
  runtime: AgentRuntime;
  snapshot?: CardSnapshot;
  pinned: boolean;
  sending: boolean;
  onOpen: () => void;
  onTogglePin: () => void;
  onSend: (content: string) => Promise<void>;
  onStart: () => void;
  onStop: () => void;
}) {
  const [content, setContent] = useState('');
  const [error, setError] = useState('');
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!content.trim()) return;
    setError('');
    try { await onSend(content.trim()); setContent(''); } catch (reason) { setError(reason instanceof Error ? reason.message : '发送失败'); }
  };
  const canSend = runtime.lifecycle_state === 'RUNNING' && !agent.archived;
  return (
    <article className={`agent-work-card state-${cardTone(runtime)} ${pinned ? 'is-pinned' : ''}`}>
      <header className="agent-work-card-header">
        <button type="button" className="card-open-button" onClick={onOpen} aria-label="查看详情">
          <span className={`runtime-dot state-${cardTone(runtime)}`} />
          <span><strong>{agent.name}</strong><small>{agent.description || '未填写描述'}</small></span>
        </button>
        <button type="button" className={`card-pin-button ${pinned ? 'is-pinned' : ''}`} onClick={onTogglePin} aria-label={pinned ? '取消固定观察' : '固定观察'} title={pinned ? '取消固定' : '固定观察'}><Pin size={14} /></button>
      </header>
      <div className="card-runtime-line"><span>{runtimeLabel(runtime)}</span>{runtime.active_run_count ? <b>{runtime.active_run_count} Run</b> : null}{runtime.waiting_human_count ? <b>等待你</b> : null}</div>
      <button type="button" className="card-snapshot" onClick={onOpen}>
        <span><Bot size={13} />{snapshot?.lastTool ? `最后工具：${snapshot.lastTool}` : '暂无可用工具摘要'}</span>
        <strong>{snapshot?.result || '尚无已完成结果。打开详情可查看会话与执行过程。'}</strong>
      </button>
      <form className="card-composer" onSubmit={submit}>
        <textarea rows={2} value={content} onChange={(event) => setContent(event.target.value)} disabled={!canSend || sending} placeholder={canSend ? `向 ${agent.name} 下达任务` : '启动 Agent 后可派发任务'} />
        {error ? <p role="alert">{error}</p> : null}
        <footer>
          <button type="button" className="card-detail-link" onClick={onOpen}><Eye size={13} />详情</button>
          {canSend ? <button type="submit" className="card-send-button" disabled={!content.trim() || sending}><Send size={13} />{sending ? '发送中' : '派发'}</button> : runtime.lifecycle_state === 'RUNNING' ? null : <button type="button" className="card-send-button" disabled={agent.archived} onClick={onStart}><Play size={13} />启动</button>}
          {runtime.lifecycle_state === 'RUNNING' ? <button type="button" className="card-stop-button" onClick={onStop} aria-label="关闭 Agent" title="关闭 Agent"><X size={14} /></button> : null}
        </footer>
      </form>
    </article>
  );
}

export function useCardSnapshots(agents: AgentSummary[], runtimes: Record<string, AgentRuntime>) {
  const [snapshots, setSnapshots] = useState<Record<string, CardSnapshot>>({});
  const requested = useRef(new Set<string>());
  const runtimeSignature = agents.map((agent) => {
    const runtime = runtimes[agent.agent_id];
    return `${agent.agent_id}:${runtime?.recent_session_id || ''}:${runtime?.active_run_count || 0}:${runtime?.waiting_human_count || 0}:${runtime?.lifecycle_state || ''}`;
  }).join('|');

  useEffect(() => {
    agents.forEach((agent) => {
      const sessionId = runtimes[agent.agent_id]?.recent_session_id;
      if (!sessionId) return;
      const key = `${agent.agent_id}:${sessionId}:${runtimes[agent.agent_id]?.active_run_count || 0}:${runtimes[agent.agent_id]?.waiting_human_count || 0}`;
      if (requested.current.has(key)) return;
      requested.current.add(key);
      void apiRequest<ReplayResponse>(`/api/agents/${encodeURIComponent(agent.agent_id)}/sessions/${encodeURIComponent(sessionId)}/replay`)
        .then((replay) => setSnapshots((current) => ({ ...current, [agent.agent_id]: snapshotFromReplay(sessionId, replay.messages) })))
        .catch(() => undefined);
    });
  }, [agents, runtimeSignature, runtimes]);

  return snapshots;
}

function snapshotFromReplay(sessionId: string, messages: MessageRecord[]): CardSnapshot {
  let result = '';
  let lastTool: string | null = null;
  for (const message of messages) {
    for (const part of message.parts || []) {
      if (part.type === 'tool' && part.tool) lastTool = String(part.tool);
      if (message.info?.role === 'assistant' && part.type === 'text' && part.text) result = String(part.text);
    }
  }
  return { sessionId, result: compact(result, 240), lastTool, updatedAt: null };
}

function compact(value: string, limit: number) {
  const normalized = value.replace(/\s+/g, ' ').trim();
  return normalized.length > limit ? `${normalized.slice(0, limit)}...` : normalized;
}

function laneFor(runtime: AgentRuntime | undefined) {
  if (!runtime || runtime.lifecycle_state === 'STOPPED' || runtime.lifecycle_state === 'ERROR' || runtime.lifecycle_state === 'STOPPING') return 'stopped';
  if (runtime.waiting_human_count > 0) return 'waiting';
  if (runtime.active_run_count > 0 || runtime.lifecycle_state === 'STARTING') return 'running';
  return 'idle';
}

function cardTone(runtime: AgentRuntime) {
  if (runtime.lifecycle_state === 'ERROR') return 'error';
  if (runtime.waiting_human_count) return 'waiting';
  if (runtime.active_run_count || runtime.lifecycle_state === 'STARTING') return 'running';
  return runtime.lifecycle_state.toLowerCase();
}

function runtimeLabel(runtime: AgentRuntime) {
  if (runtime.lifecycle_state === 'ERROR') return '运行异常';
  if (runtime.waiting_human_count) return '等待人工处理';
  if (runtime.active_run_count) return '正在执行';
  return runtime.lifecycle_state === 'RUNNING' ? '空闲可用' : runtime.lifecycle_state === 'STARTING' ? '启动中' : '已停止';
}

function stoppedRuntime(agentId: string): AgentRuntime {
  return { agent_id: agentId, desired_state: 'STOPPED', lifecycle_state: 'STOPPED', recent_session_id: null, active_run_count: 0, waiting_human_count: 0, error_code: null };
}
