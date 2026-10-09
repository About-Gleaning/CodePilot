import { FormEvent, useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Pause, Pencil, Play, Plus, Save, Square, Trash2, X } from 'lucide-react';
import { apiJson, apiRequest } from '../../api/client';
import type { AgentSummary, ProviderCapability } from './types';
import { WorkingDirectoryPicker } from './WorkingDirectoryPicker';

type Trigger = { kind: string; interval_seconds?: number | null; run_at?: string | null; time_of_day?: string | null; day_of_week?: number | null; timezone?: string | null };
type Schedule = { id: string; name: string; prompt: string; agent_id: string; agent_name: string; provider: string; model: string; enabled: boolean; working_dir: string; follow_agent_model?: boolean; follow_agent_directory?: boolean; next_run_at?: string | null; trigger: Trigger };
type ScheduleRun = { id: string; task_name: string; agent_id: string; status: string; session_id?: string | null; scheduled_at: string; error?: string | null; stop_reason?: string | null };
const statuses: Record<string, string> = { pending: '等待容量', running: '运行中', stopping: '正在停止', completed: '已完成', failed: '失败', timeout: '执行超时', interrupted: '已中断', cancelled: '已停止', skipped: '已跳过' };
const initialForm = (agentId = '') => ({ id: '', name: '', prompt: '', agent_id: agentId, provider: '', model: '', follow_agent_model: true, follow_agent_directory: true, working_dir: null as string | null, enabled: true, kind: 'interval', interval: '3600', run_at: '', time: '09:00', weekday: '1', timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai' });

export function AutomationPanel({ active, agents, agentId, onOpenSession }: { active: boolean; agents: AgentSummary[]; agentId?: string | null; onOpenSession: (agentId: string, sessionId: string) => void }) {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [runs, setRuns] = useState<ScheduleRun[]>([]);
  const [providers, setProviders] = useState<ProviderCapability[]>([]);
  const [form, setForm] = useState(initialForm);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [runPage, setRunPage] = useState(0);
  const [runTotal, setRunTotal] = useState(0);
  const generation = useRef(0);
  const mutate = async (action: () => Promise<unknown>) => {
    const current = generation.current;
    setBusy(true); setError('');
    try { await action(); if (current === generation.current) setRevision((value) => value + 1); }
    catch (reason) { if (current === generation.current) setError(reason instanceof Error ? reason.message : '操作失败'); }
    finally { setBusy(false); }
  };
  useEffect(() => { setPage(0); setRunPage(0); setSchedules([]); setRuns([]); setTotal(0); setRunTotal(0); setError(''); }, [agentId]);
  useEffect(() => {
    const current = ++generation.current;
    if (!active) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const refresh = async () => {
      let delay = 30000;
      try {
        const filter = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
        const [tasks, result] = await Promise.all([
          apiRequest<{ schedules: Schedule[]; total: number }>(`/api/schedules?limit=20&offset=${page * 20}${filter}`, { signal: controller.signal }),
          apiRequest<{ active: ScheduleRun[]; recent: ScheduleRun[]; total: number }>(`/api/schedule-runs?limit=20&offset=${runPage * 20}${filter}`, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted || current !== generation.current) return;
        setSchedules(tasks.schedules); setTotal(tasks.total); setRunTotal(result.total);
        setRuns([...new Map([...result.active, ...result.recent].map((run) => [run.id, run])).values()]);
        delay = result.active.length ? 3000 : 30000;
      } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '自动化加载失败'); }
      if (!controller.signal.aborted) timer = window.setTimeout(() => void refresh(), delay);
    };
    void refresh();
    void apiRequest<{ providers: ProviderCapability[] }>('/api/agent-capabilities', { signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setProviders(value.providers);
    }).catch(() => {});
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [active, agentId, page, runPage, revision]);
  const edit = (task?: Schedule) => {
    if (showForm && !window.confirm('放弃当前定时任务草稿？')) return;
    setForm(task ? { ...initialForm(), ...task, working_dir: task.working_dir, follow_agent_model: !!task.follow_agent_model,
      follow_agent_directory: !!task.follow_agent_directory, kind: task.trigger.kind, interval: String(task.trigger.interval_seconds || 3600),
      run_at: task.trigger.run_at ? localInput(task.trigger.run_at) : '', time: task.trigger.time_of_day || '09:00',
      weekday: String(task.trigger.day_of_week || 1), timezone: task.trigger.timezone || initialForm().timezone } : initialForm(agentId || ''));
    setShowForm(true); setError('');
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const trigger: Trigger = { kind: form.kind, timezone: form.timezone };
    if (form.kind === 'once') {
      const instant = new Date(form.run_at);
      if (!Number.isFinite(instant.getTime())) { setError('请选择有效的执行时间。'); return; }
      trigger.run_at = instant.toISOString();
    }
    if (form.kind === 'interval') trigger.interval_seconds = Number(form.interval);
    if (['daily', 'weekly'].includes(form.kind)) trigger.time_of_day = form.time;
    if (form.kind === 'weekly') trigger.day_of_week = Number(form.weekday);
    const current = generation.current;
    await mutate(async () => {
      await apiJson(form.id ? `/api/schedules/${form.id}` : '/api/schedules', form.id ? 'PATCH' : 'POST', {
        name: form.name, prompt: form.prompt, agent_id: form.agent_id, provider: form.provider, model: form.model,
        follow_agent_model: form.follow_agent_model, follow_agent_directory: form.follow_agent_directory,
        working_dir: form.working_dir || '.', enabled: form.enabled, trigger,
      });
      if (current === generation.current) { setShowForm(false); setForm(initialForm(agentId || '')); }
    });
  };
  if (!active) return null;
  return <div className="automation-panel">
    <div className="inspector-heading"><strong>定时任务</strong><button type="button" className="studio-icon-button" title="新建定时任务" aria-label="新建定时任务" onClick={() => edit()}><Plus size={15} /></button></div>
    {error ? <div className="studio-notice tone-danger" role="alert">{error}</div> : null}
    {showForm ? <form className="automation-form" onSubmit={(event) => void submit(event)}>
      <strong>{form.id ? '编辑定时任务' : '新建定时任务'}</strong>
      <label className="studio-field"><span>任务名称</span><input required maxLength={200} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
      <label className="studio-field"><span>任务要求</span><textarea required rows={4} maxLength={32000} value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })} /></label>
      <label className="studio-field"><span>Agent</span><select required value={form.agent_id} onChange={(e) => setForm({ ...form, agent_id: e.target.value })}><option value="">选择 Agent</option>{agents.filter((a) => a.kind !== 'subagent' && !a.archived).map((a) => <option key={a.agent_id} value={a.agent_id}>{a.name}</option>)}</select></label>
      <label className="studio-field"><span>触发方式</span><select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}><option value="once">单次</option><option value="interval">按间隔</option><option value="daily">每天</option><option value="weekly">每周</option></select></label>
      {form.kind === 'once' ? <label className="studio-field"><span>执行时间（本机时区）</span><input required type="datetime-local" value={form.run_at} onChange={(e) => setForm({ ...form, run_at: e.target.value })} /></label> : null}
      {form.kind === 'interval' ? <label className="studio-field"><span>间隔（秒）</span><input required type="number" min="60" step="1" value={form.interval} onChange={(e) => setForm({ ...form, interval: e.target.value })} /></label> : null}
      {form.kind === 'weekly' ? <label className="studio-field"><span>星期</span><select value={form.weekday} onChange={(e) => setForm({ ...form, weekday: e.target.value })}>{['一', '二', '三', '四', '五', '六', '日'].map((v, i) => <option key={v} value={i + 1}>星期{v}</option>)}</select></label> : null}
      {['daily', 'weekly'].includes(form.kind) ? <><label className="studio-field"><span>执行时间</span><input required type="time" value={form.time} onChange={(e) => setForm({ ...form, time: e.target.value })} /></label><label className="studio-field"><span>时区</span><input required value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} /></label></> : null}
      <label><input type="checkbox" checked={form.follow_agent_model} onChange={(e) => setForm({ ...form, follow_agent_model: e.target.checked })} />跟随 Agent 默认模型</label>
      {!form.follow_agent_model ? <><label className="studio-field"><span>模型服务</span><select required value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value, model: '' })}><option value="">选择</option>{providers.map((p) => <option key={p.provider} value={p.provider}>{p.label}</option>)}</select></label><label className="studio-field"><span>模型</span><select required value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })}><option value="">选择</option>{providers.find((p) => p.provider === form.provider)?.models.map((m) => <option key={m}>{m}</option>)}</select></label></> : null}
      <label><input type="checkbox" checked={form.follow_agent_directory} onChange={(e) => setForm({ ...form, follow_agent_directory: e.target.checked })} />跟随 Agent 默认目录</label>
      {!form.follow_agent_directory ? <WorkingDirectoryPicker value={form.working_dir} disabled={busy} onChange={(path) => setForm({ ...form, working_dir: path })} /> : null}
      <div className="inline-actions"><button className="studio-button primary" disabled={busy} type="submit"><Save size={14} />保存任务</button><button className="studio-button" disabled={busy} type="button" onClick={() => { if (window.confirm('放弃当前定时任务草稿？')) setShowForm(false); }}><X size={14} />取消</button></div>
    </form> : null}
    <section className="automation-list">{schedules.map((task) => <article key={task.id}>
      <div><strong>{task.name}</strong><span>{task.agent_name} · {task.follow_agent_model ? '默认模型' : task.model}</span></div>
      <small>{task.enabled ? task.next_run_at ? `下次 ${formatDate(task.next_run_at)}` : '无后续执行' : '已停用'}</small>
      <div className="inline-actions"><button type="button" className="studio-icon-button" title={`编辑 ${task.name}`} disabled={busy} onClick={() => edit(task)}><Pencil size={14} /></button><button type="button" className="studio-icon-button" title={task.enabled ? '停用' : '启用'} disabled={busy} onClick={() => void mutate(() => apiJson(`/api/schedules/${task.id}`, 'PATCH', { enabled: !task.enabled }))}>{task.enabled ? <Pause size={14} /> : <Play size={14} />}</button><button type="button" className="studio-icon-button danger" title={`删除 ${task.name}`} disabled={busy} onClick={() => { if (window.confirm(`删除定时任务「${task.name}」？历史记录会保留。`)) void mutate(() => apiJson(`/api/schedules/${task.id}`, 'DELETE')); }}><Trash2 size={14} /></button></div>
    </article>)}{!schedules.length ? <p className="studio-empty-copy">尚无定时任务</p> : null}</section>
    <Pager page={page} total={total} onChange={setPage} label="任务" />
    <section className="automation-runs"><header><strong>最近执行</strong></header>{runs.map((run) => <div className="schedule-run-row" key={run.id}>
      <button type="button" disabled={!run.session_id} onClick={() => run.session_id && onOpenSession(run.agent_id, run.session_id)}><span><strong>{run.task_name}</strong><small>{formatDate(run.scheduled_at)}</small></span><em>{run.stop_reason === 'max_iterations' ? '达到轮数上限' : statuses[run.status] || run.status}</em></button>
      {['running', 'pending'].includes(run.status) ? <button type="button" className="studio-icon-button" title={`停止 ${run.task_name}`} disabled={busy} onClick={() => void mutate(() => apiJson(`/api/schedule-runs/${run.id}/stop`, 'POST'))}><Square size={14} /></button> : null}
      {run.error ? <small className="schedule-run-error">{run.error}</small> : null}
    </div>)}</section><Pager page={runPage} total={runTotal} onChange={setRunPage} label="执行" />
  </div>;
}
function Pager({ page, total, onChange, label }: { page: number; total: number; onChange: (page: number) => void; label: string }) {
  if (!(total > 20)) return null;
  return <div className="inline-actions"><button className="studio-icon-button" title={`${label}上一页`} disabled={!page} onClick={() => onChange(page - 1)}><ChevronLeft size={14} /></button><span>{page + 1} / {Math.ceil(total / 20)}</span><button className="studio-icon-button" title={`${label}下一页`} disabled={(page + 1) * 20 >= total} onClick={() => onChange(page + 1)}><ChevronRight size={14} /></button></div>;
}
function formatDate(value: string) { return new Date(value).toLocaleString('zh-CN'); }
function localInput(value: string) { const date = new Date(value); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16); }
