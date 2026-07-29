import { FormEvent, useEffect, useMemo, useState } from 'react';

type Agent = { agent_id: string; revision_id: string; name: string; description?: string; source: string; archived: boolean; validation_status: string; system_prompt?: string; default_provider?: string; default_model?: string; default_thinking_value?: string; tool_names?: string[]; mcp_server_names?: string[] };
type Capability = { providers: Array<{ provider: string; label: string; models: string[] }>; tools: Array<{ name: string; description: string; assignable: boolean; reason?: string | null }>; mcp_servers: Array<{ name: string; status: string; requires_approval: boolean }> };

const emptyForm = () => ({ name: '', description: '', system_prompt: '', default_provider: '', default_model: '', default_thinking_value: '', tool_names: [] as string[], mcp_server_names: [] as string[] });

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: { message?: string } } | null;
    throw new Error(body?.detail?.message || '配置请求失败');
  }
  return response.json() as Promise<T>;
}

export function AgentConfigDialog({ open, onClose, onChanged }: { open: boolean; onClose: () => void; onChanged: () => void }) {
  const [agents, setAgents] = useState<Agent[]>([]); const [capabilities, setCapabilities] = useState<Capability | null>(null);
  const [selected, setSelected] = useState<Agent | null>(null); const [form, setForm] = useState(emptyForm); const [error, setError] = useState('');
  const [dirty, setDirty] = useState(false);
  const refresh = async () => { const [list, caps] = await Promise.all([api<{ agents: Agent[] }>('/api/agents?status=all'), api<Capability>('/api/agent-capabilities')]); setAgents(list.agents); setCapabilities(caps); };
  useEffect(() => { if (open) void refresh().catch((e) => setError(e.message)); }, [open]);
  useEffect(() => { const handler = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } }; window.addEventListener('beforeunload', handler); return () => window.removeEventListener('beforeunload', handler); }, [dirty]);
  const models = useMemo(() => capabilities?.providers.find((p) => p.provider === form.default_provider)?.models || [], [capabilities, form.default_provider]);
  if (!open) return null;
  const select = async (agent: Agent) => { if (dirty && !window.confirm('存在未保存修改，仍要切换吗？')) return; const detail = await api<Agent>(`/api/agents/${agent.agent_id}`); setSelected(detail); setForm({ name: detail.name, description: detail.description || '', system_prompt: detail.system_prompt || '', default_provider: detail.default_provider || '', default_model: detail.default_model || '', default_thinking_value: detail.default_thinking_value || '', tool_names: detail.tool_names || [], mcp_server_names: detail.mcp_server_names || [] }); setDirty(false); setError(''); };
  const update = (key: string, value: unknown) => { setForm((old) => ({ ...old, [key]: value })); setDirty(true); };
  const toggle = (key: 'tool_names' | 'mcp_server_names', value: string) => update(key, form[key].includes(value) ? form[key].filter((v) => v !== value) : [...form[key], value]);
  const save = async (event: FormEvent) => { event.preventDefault(); try { const payload = selected ? { ...form, expected_revision_id: selected.revision_id } : form; const result = await api<Agent>(selected ? `/api/agents/${selected.agent_id}` : '/api/agents', { method: selected ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); setSelected(result); setDirty(false); await refresh(); onChanged(); } catch (e) { setError(e instanceof Error ? e.message : '保存失败'); } };
  const mutate = async (action: 'archive' | 'restore') => { if (!selected) return; try { const result = await api<Agent>(`/api/agents/${selected.agent_id}/${action}`, { method: 'POST' }); setSelected(result); await refresh(); onChanged(); } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); } };
  const readonly = selected?.source === 'builtin' || selected?.validation_status === 'invalid';
  return <div className="agent-config-backdrop" role="presentation" onMouseDown={() => { if (!dirty || window.confirm('存在未保存修改，关闭吗？')) onClose(); }}><section className="agent-config-dialog" role="dialog" aria-modal="true" aria-label="Agent 配置中心" onMouseDown={(e) => e.stopPropagation()}>
    <header><div><strong>Agent 配置中心</strong><span>Markdown 配置与能力目录</span></div><button type="button" onClick={onClose}>关闭</button></header>
    {error ? <p className="agent-config-error">{error}</p> : null}
    <div className="agent-config-body"><aside><button type="button" onClick={() => { setSelected(null); setForm(emptyForm()); setDirty(false); }}>+ 新建 Agent</button>{agents.map((agent) => <button type="button" className={selected?.agent_id === agent.agent_id ? 'is-selected' : ''} key={agent.agent_id} onClick={() => void select(agent)}>{agent.name}<small>{agent.archived ? '已归档' : agent.source === 'builtin' ? '内置' : agent.validation_status}</small></button>)}</aside>
    <form onSubmit={save}><label>名称<input value={form.name} disabled={Boolean(selected)} onChange={(e) => update('name', e.target.value)} /></label><label>描述<input value={form.description} disabled={readonly} onChange={(e) => update('description', e.target.value)} /></label><label>System Prompt<textarea rows={7} value={form.system_prompt} disabled={readonly} onChange={(e) => update('system_prompt', e.target.value)} /></label>
      <div className="agent-config-grid"><label>Provider<select value={form.default_provider} disabled={readonly} onChange={(e) => { update('default_provider', e.target.value); update('default_model', ''); }}><option value="">请选择</option>{capabilities?.providers.map((p) => <option key={p.provider} value={p.provider}>{p.label}</option>)}</select></label><label>Model<select value={form.default_model} disabled={readonly || !form.default_provider} onChange={(e) => update('default_model', e.target.value)}><option value="">请选择</option>{models.map((m) => <option key={m}>{m}</option>)}</select></label></div>
      <fieldset disabled={readonly}><legend>Tools</legend>{capabilities?.tools.map((tool) => <label className="agent-check" key={tool.name}><input type="checkbox" checked={form.tool_names.includes(tool.name)} disabled={!tool.assignable} onChange={() => toggle('tool_names', tool.name)} />{tool.name}<small>{tool.assignable ? tool.description : tool.reason}</small></label>)}</fieldset>
      <fieldset disabled={readonly}><legend>MCP 服务</legend>{capabilities?.mcp_servers.map((mcp) => <label className="agent-check" key={mcp.name}><input type="checkbox" checked={form.mcp_server_names.includes(mcp.name)} disabled={mcp.status === 'disabled' && !form.mcp_server_names.includes(mcp.name)} onChange={() => toggle('mcp_server_names', mcp.name)} />{mcp.name}<small>{mcp.status}{mcp.requires_approval ? ' · 需审批' : ''}</small></label>)}</fieldset>
      <footer>{selected?.source === 'custom' ? <>{selected.archived ? <button type="button" onClick={() => void mutate('restore')}>恢复</button> : <button type="button" onClick={() => void mutate('archive')}>归档</button>}</> : null}<button type="submit" disabled={readonly}>保存</button></footer>
    </form></div>
  </section></div>;
}
