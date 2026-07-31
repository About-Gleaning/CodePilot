import { FormEvent, useEffect, useMemo, useState } from 'react';
import { CalendarClock, Pause, Play, Plus, Trash2 } from 'lucide-react';

import { apiJson, apiRequest } from '../../api/client';
import type { AgentSummary, ProviderCapability } from './types';

type Schedule = {
  id: string;
  name: string;
  prompt: string;
  agent_name: string;
  provider: string;
  model: string;
  enabled: boolean;
  working_dir: string;
  next_run_at?: string | null;
  trigger: { kind: string; interval_seconds?: number | null };
};

type ScheduleRun = {
  id: string;
  task_name: string;
  status: string;
  session_id?: string | null;
  scheduled_at: string;
};

export function AutomationPanel({
  active,
  agents,
  onOpenSession,
}: {
  active: boolean;
  agents: AgentSummary[];
  onOpenSession: (agentId: string, sessionId: string) => void;
}) {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [runs, setRuns] = useState<{ active: ScheduleRun[]; recent: ScheduleRun[] }>({ active: [], recent: [] });
  const [providers, setProviders] = useState<ProviderCapability[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState('');
  const [form, setForm] = useState({ name: '', prompt: '', agent_name: '', provider: '', model: '', interval: '3600' });

  const refresh = async () => {
    const [tasks, nextRuns] = await Promise.all([
      apiRequest<{ schedules: Schedule[] }>('/api/schedules'),
      apiRequest<{ active: ScheduleRun[]; recent: ScheduleRun[] }>('/api/schedule-runs'),
    ]);
    setSchedules(tasks.schedules);
    setRuns(nextRuns);
  };

  useEffect(() => {
    if (!active) return;
    let stopped = false;
    void Promise.all([
      refresh(),
      apiRequest<{ providers: ProviderCapability[] }>('/api/agent-capabilities').then((value) => setProviders(value.providers)),
    ]).catch((reason) => setError(reason instanceof Error ? reason.message : '自动化加载失败'));
    const timer = window.setInterval(() => {
      if (!stopped) void refresh().catch(() => setError('自动化状态刷新失败'));
    }, runs.active.length ? 3000 : 30000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [active, runs.active.length]);

  const selectedProvider = providers.find((item) => item.provider === form.provider);
  const models = selectedProvider?.models || [];
  const activeAgents = agents.filter((agent) => !agent.archived && agent.validation_status !== 'invalid');
  const agentIdByName = useMemo(
    () => Object.fromEntries(agents.map((agent) => [agent.name, agent.agent_id])),
    [agents],
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    try {
      await apiJson('/api/schedules', 'POST', {
        name: form.name,
        prompt: form.prompt,
        agent_name: form.agent_name,
        provider: form.provider,
        model: form.model,
        working_dir: '.',
        enabled: true,
        trigger: { kind: 'interval', interval_seconds: Number(form.interval) },
      });
      setForm({ name: '', prompt: '', agent_name: '', provider: '', model: '', interval: '3600' });
      setShowForm(false);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建定时任务失败');
    }
  };

  if (!active) return null;
  return (
    <div className="automation-panel">
      <div className="inspector-heading">
        <div><span className="eyebrow">AUTOMATION</span><strong>定时任务</strong></div>
        <button type="button" className="studio-icon-button" onClick={() => setShowForm((value) => !value)}><Plus size={15} /></button>
      </div>
      {error ? <div className="studio-notice tone-danger">{error}</div> : null}
      {showForm ? (
        <form className="automation-form" onSubmit={submit}>
          <label className="studio-field"><span>任务名</span><input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
          <label className="studio-field"><span>Prompt</span><textarea required rows={4} value={form.prompt} onChange={(event) => setForm({ ...form, prompt: event.target.value })} /></label>
          <label className="studio-field"><span>Agent</span><select required value={form.agent_name} onChange={(event) => setForm({ ...form, agent_name: event.target.value })}><option value="">选择</option>{activeAgents.map((agent) => <option key={agent.agent_id} value={agent.name}>{agent.name}</option>)}</select></label>
          <label className="studio-field"><span>Provider</span><select required value={form.provider} onChange={(event) => setForm({ ...form, provider: event.target.value, model: '' })}><option value="">选择</option>{providers.map((provider) => <option key={provider.provider} value={provider.provider}>{provider.label}</option>)}</select></label>
          <label className="studio-field"><span>Model</span><select required value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })}><option value="">选择</option>{models.map((model) => <option key={model}>{model}</option>)}</select></label>
          <label className="studio-field"><span>间隔（秒）</span><input type="number" min="60" value={form.interval} onChange={(event) => setForm({ ...form, interval: event.target.value })} /></label>
          <button className="studio-button primary" type="submit"><CalendarClock size={15} />创建自动化</button>
        </form>
      ) : null}
      <section className="automation-list">
        {schedules.map((task) => (
          <article key={task.id}>
            <div><strong>{task.name}</strong><span>{task.agent_name} · {task.model}</span></div>
            <small>{task.next_run_at ? `下次 ${formatDate(task.next_run_at)}` : '未安排'}</small>
            <div className="inline-actions">
              <button type="button" className="studio-icon-button" title={task.enabled ? '暂停' : '启用'} onClick={() => void apiJson(`/api/schedules/${task.id}`, 'PATCH', { enabled: !task.enabled }).then(refresh)}>
                {task.enabled ? <Pause size={13} /> : <Play size={13} />}
              </button>
              <button type="button" className="studio-icon-button danger" title="删除" onClick={() => {
                if (window.confirm(`删除自动化「${task.name}」？`)) void apiJson(`/api/schedules/${task.id}`, 'DELETE').then(refresh);
              }}><Trash2 size={13} /></button>
            </div>
          </article>
        ))}
        {!schedules.length ? <p className="studio-empty-copy">尚未创建自动化。</p> : null}
      </section>
      <section className="automation-runs">
        <header><strong>最近运行</strong><small>{runs.active.length} 活动</small></header>
        {[...runs.active, ...runs.recent].slice(0, 12).map((run) => (
          <button type="button" key={run.id} disabled={!run.session_id} onClick={() => {
            const task = schedules.find((item) => item.name === run.task_name);
            const agentId = task ? agentIdByName[task.agent_name] : '';
            if (agentId && run.session_id) onOpenSession(agentId, run.session_id);
          }}>
            <span className={`runtime-dot state-${run.status.toLowerCase()}`} />
            <span><strong>{run.task_name}</strong><small>{formatDate(run.scheduled_at)}</small></span>
            <em>{run.status}</em>
          </button>
        ))}
      </section>
    </div>
  );
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
