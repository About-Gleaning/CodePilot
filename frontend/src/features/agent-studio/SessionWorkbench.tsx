import { useState } from 'react';
import { ArrowDown, Download, Square } from 'lucide-react';
import { MarkdownContent } from '../../components/MessageContent';
import type { MessageRecord, StreamEvent } from '../../types';
import type { SessionRuntime } from './types';

const labels: Record<string, string> = { STARTING: '准备中', RUNNING: '运行中', STOPPING: '正在停止', CANCELLING: '正在停止', WAITING_HUMAN: '等待处理', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已停止', IDLE: '空闲' };
const phases: Record<string, string> = { pending: '等待容量', starting: '准备执行', model: '模型请求', tool: '执行工具', decision: '准备下一轮', stopping: '正在停止', finished: '执行结束' };
const textOf = (message?: MessageRecord) => (message?.parts || []).filter((p) => p.type === 'text' && !p.synthetic).map((p) => String(p.text || '')).join('\n');

export function SessionWorkbench({ runtime, messages, events, onStop }: { runtime: SessionRuntime | null; messages: MessageRecord[]; events: StreamEvent[]; onStop: () => void }) {
  const [tab, setTab] = useState('overview');
  if (!runtime) return null;
  const runId = runtime.active_run?.run_id || runtime.last_run?.ref?.run_id || runtime.last_run?.run_id || events[events.length - 1]?.run_id;
  const current = runId ? messages.filter((m) => m.info?.run_id === runId) : [];
  const currentEvents = events.filter((event) => event.run_id === runId);
  const mainEvents = currentEvents.filter((event) => event.data.agent_kind !== 'subagent');
  const iteration = runtime.iteration || [...mainEvents].reverse().find((event) => event.event_type === 'loop_iteration_started')?.data.iteration;
  const maximum = runtime.max_iterations || mainEvents.find((event) => event.event_type === 'loop_started')?.data.max_iterations;
  const reply = [...current].reverse().find((m) => m.info?.role === 'assistant' && m.info.agent_kind !== 'subagent' && textOf(m));
  const groups = new Map<string, MessageRecord[]>();
  for (const message of current) {
    if (message.info?.agent_kind === 'subagent') {
      const key = message.info.context_id || message.info.parent_call_id || '';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(message);
    }
  }
  const files = current.flatMap((m) => (m.parts || []).filter((p) => p.artifact).map((p) => p.artifact as { name: string; path: string; url: string }));
  const outputFiles = [...new Map(files.map((file) => [file.path, file])).values()];
  const status = runtime.stop_reason === 'max_iterations' ? '已停止：达到轮数上限' : labels[runtime.status] || runtime.status;
  return <section className="session-workbench" aria-label="会话工作台">
    <div className="workbench-tabs" role="tablist" aria-label="执行信息">
      {([['overview', '执行概览'], ['results', '成果'], ['children', '子 Agent']] as const).map(([key, name]) => <button type="button" role="tab" aria-selected={tab === key} key={key} onClick={() => setTab(key)}>{name}{key === 'children' && groups.size ? ` (${groups.size})` : ''}</button>)}
    </div>
    <div className="workbench-body" role="tabpanel">
      {tab === 'overview' ? <>
        <dl className="workbench-facts"><div><dt>来源</dt><dd>{runtime.source === 'schedule' ? runtime.schedule_task_name || '定时任务' : '网页执行'}</dd></div><div><dt>状态</dt><dd>{status}</dd></div><div><dt>轮数</dt><dd>{iteration ? String(iteration) : '—'}{maximum ? ` / ${maximum}` : ''}</dd></div><div><dt>开始时间</dt><dd>{date(runtime.started_at || runtime.active_run?.started_at || runtime.last_run?.started_at)}</dd></div></dl>
        {runtime.phase ? <p>{phases[runtime.phase] || runtime.phase}{runtime.worker_exited === false && !['RUNNING', 'STARTING', 'STOPPING'].includes(runtime.status) ? ' · 正在收尾' : ''}</p> : null}
        {runtime.error_summary ? <p className="workbench-error">{runtime.error_summary}</p> : null}
        {runtime.pending_interaction ? <p>待处理：{runtime.pending_interaction.kind === 'approval' ? '操作审批' : '补充信息'}</p> : null}
        {runtime.source === 'schedule' && runtime.can_stop ? <button type="button" className="studio-button" disabled={runtime.status === 'STOPPING'} onClick={onStop}><Square size={13} />停止本轮</button> : null}
      </> : null}
      {tab === 'results' ? <>
        <strong>{runtime.status === 'COMPLETED' ? '最终回复' : '已有输出'}</strong>
        {reply ? <MarkdownContent className="workbench-output" text={textOf(reply)} /> : <p className="studio-empty-copy">暂无输出</p>}
        {outputFiles.length ? <ul className="workbench-files">{outputFiles.map((file) => <li key={file.path}><span>{file.path}</span><a href={file.url} download title={`下载 ${file.name}`} aria-label={`下载 ${file.name}`}><Download size={16} /></a></li>)}</ul> : null}
      </> : null}
      {tab === 'children' ? <>
        {[...groups].map(([context, items]) => {
          const first = items[0];
          const task = current.flatMap((m) => m.parts || []).find((p) => p.type === 'tool' && p.call_id === first.info?.parent_call_id);
          const state = task?.state as { status?: string; output?: { status?: string }; error?: { message?: string } } | undefined;
          const failed = state?.status === 'error' || state?.output?.status === 'error';
          const done = state?.status === 'completed' && !failed;
          const childLoop = [...currentEvents].reverse().find((event) => event.data.context_id === context && event.event_type === 'loop_iteration_started');
          const childResult = [...items].reverse().find((message) => message.info?.role === 'assistant' && textOf(message));
          return <article className="workbench-child" key={context}><header><strong>{first.info?.agent || '子 Agent'}</strong><span>{failed ? '失败／已停止' : done ? '已完成' : runtime.active_run ? '执行中' : '已结束'}{childLoop ? ` · 第 ${childLoop.data.iteration} 轮` : ''}</span><button type="button" className="studio-icon-button" title="查看子 Agent 消息" onClick={() => document.getElementById(`message-${first.info?.id}`)?.scrollIntoView({ block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })}><ArrowDown size={14} /></button></header><p>{textOf(first)}</p>{childResult ? <MarkdownContent className="workbench-output" text={textOf(childResult)} /> : null}{state?.error?.message ? <p className="workbench-error">{state.error.message}</p> : null}</article>;
        })}
        {!groups.size ? <p className="studio-empty-copy">本轮未委派子 Agent</p> : null}
      </> : null}
    </div>
  </section>;
}
function date(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN') : '—'; }
