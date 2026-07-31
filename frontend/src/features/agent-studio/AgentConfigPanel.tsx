import { FormEvent, useEffect, useMemo, useState } from 'react';
import { Archive, Copy, RotateCcw, Save, Sparkles } from 'lucide-react';

import { apiJson, apiRequest } from '../../api/client';
import type { AgentCapabilities, AgentDetail, AgentSummary } from './types';

type FormState = {
  name: string;
  description: string;
  system_prompt: string;
  default_provider: string;
  default_model: string;
  default_thinking_value: string;
  tool_names: string[];
  mcp_server_names: string[];
};

const EMPTY_FORM: FormState = {
  name: '',
  description: '',
  system_prompt: '',
  default_provider: '',
  default_model: '',
  default_thinking_value: '',
  tool_names: [],
  mcp_server_names: [],
};

export function AgentConfigPanel({
  agent,
  activeRunCount,
  onChanged,
  onDirtyChange,
}: {
  agent: AgentSummary | null;
  activeRunCount: number;
  onChanged: (agent?: AgentSummary) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [capabilities, setCapabilities] = useState<AgentCapabilities | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [mode, setMode] = useState<'edit' | 'create' | 'copy'>(agent ? 'edit' : 'create');
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setDirty(false);
    setMode(agent ? 'edit' : 'create');
    const requests: [Promise<AgentCapabilities>, Promise<AgentDetail | null>] = [
      apiRequest<AgentCapabilities>('/api/agent-capabilities', { signal: controller.signal }),
      agent
        ? apiRequest<AgentDetail>(`/api/agents/${encodeURIComponent(agent.agent_id)}`, { signal: controller.signal })
        : Promise.resolve(null),
    ];
    void Promise.all(requests)
      .then(([nextCapabilities, nextDetail]) => {
        if (controller.signal.aborted) return;
        setCapabilities(nextCapabilities);
        setDetail(nextDetail);
        setForm(nextDetail ? toForm(nextDetail) : EMPTY_FORM);
        setLoading(false);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setLoading(false);
          setError(reason instanceof Error ? reason.message : '配置加载失败');
        }
      });
    return () => controller.abort();
  }, [agent?.agent_id]);

  const provider = capabilities?.providers.find((item) => item.provider === form.default_provider);
  const models = provider?.models || [];
  const thinking = provider?.model_capabilities?.[form.default_model]?.thinking;
  const readonly = mode === 'edit' && (
    detail?.source === 'builtin'
    || detail?.validation_status === 'invalid'
  );
  const toolGroups = useMemo(() => {
    const groups = new Map<string, AgentCapabilities['tools']>();
    for (const tool of capabilities?.tools || []) {
      const key = tool.side_effect || 'read_only';
      groups.set(key, [...(groups.get(key) || []), tool]);
    }
    return Array.from(groups.entries());
  }, [capabilities]);

  const patch = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setDirty(true);
    setError('');
  };
  const toggle = (key: 'tool_names' | 'mcp_server_names', value: string) => {
    patch(key, form[key].includes(value) ? form[key].filter((item) => item !== value) : [...form[key], value]);
  };

  const copy = () => {
    if (!detail) return;
    setMode('copy');
    setForm({
      ...toForm(detail),
      name: '',
      // 复制时移除不能通用分配的专属能力，避免提交后才失败。
      tool_names: (detail.tool_names || []).filter((name) => capabilities?.tools.find((tool) => tool.name === name)?.assignable),
    });
    setDirty(true);
    setError('');
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError('');
    try {
      const editing = mode === 'edit' && detail;
      const payload = editing ? { ...form, expected_revision_id: detail.revision_id } : form;
      const result = await apiJson<AgentDetail>(
        editing ? `/api/agents/${encodeURIComponent(detail.agent_id)}` : '/api/agents',
        editing ? 'PUT' : 'POST',
        payload,
      );
      setDetail(result);
      setForm(toForm(result));
      setMode('edit');
      setDirty(false);
      onChanged(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const archiveOrRestore = async () => {
    if (!detail || saving) return;
    setSaving(true);
    setError('');
    try {
      const action = detail.archived ? 'restore' : 'archive';
      const result = await apiJson<AgentDetail>(
        `/api/agents/${encodeURIComponent(detail.agent_id)}/${action}`,
        'POST',
      );
      setDetail(result);
      setForm(toForm(result));
      setDirty(false);
      onChanged(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <PanelNotice title="载入配置" detail="正在读取能力目录和 Agent revision…" />;
  return (
    <form className="studio-config" onSubmit={save}>
      <div className="studio-config-heading">
        <div>
          <span className="eyebrow">{mode === 'create' ? 'NEW PROFILE' : mode === 'copy' ? 'FORK PROFILE' : 'PROFILE REVISION'}</span>
          <strong>{detail?.name || '创建 Agent'}</strong>
          {detail?.revision_id ? <code>{detail.revision_id.slice(0, 12)}</code> : null}
        </div>
        <div className="inline-actions">
          {detail ? <button type="button" className="studio-icon-button" onClick={copy} title="复制为新 Agent"><Copy size={15} /></button> : null}
          {detail?.source === 'custom' ? (
            <button type="button" className="studio-icon-button" onClick={() => void archiveOrRestore()} title={detail.archived ? '恢复' : '归档'}>
              {detail.archived ? <RotateCcw size={15} /> : <Archive size={15} />}
            </button>
          ) : null}
        </div>
      </div>
      {activeRunCount > 0 ? (
        <div className="studio-notice tone-cyan">
          <Sparkles size={14} />
          <span>当前有 {activeRunCount} 个 Run。保存后仅下一轮使用新 revision，正在执行的 Run 保持不变。</span>
        </div>
      ) : null}
      {error ? <div className="studio-notice tone-danger" role="alert">{error}</div> : null}
      <label className="studio-field">
        <span>名称</span>
        <input value={form.name} disabled={mode === 'edit'} placeholder="agent-name" onChange={(event) => patch('name', event.target.value)} />
      </label>
      <label className="studio-field">
        <span>描述</span>
        <input value={form.description} disabled={readonly} onChange={(event) => patch('description', event.target.value)} />
      </label>
      <label className="studio-field">
        <span>System Prompt</span>
        <textarea rows={10} value={form.system_prompt} disabled={readonly} onChange={(event) => patch('system_prompt', event.target.value)} />
      </label>
      <div className="studio-field-grid">
        <label className="studio-field">
          <span>Provider</span>
          <select value={form.default_provider} disabled={readonly} onChange={(event) => {
            patch('default_provider', event.target.value);
            setForm((current) => ({ ...current, default_model: '', default_thinking_value: '' }));
          }}>
            <option value="">选择 Provider</option>
            {capabilities?.providers.map((item) => <option value={item.provider} key={item.provider}>{item.label}</option>)}
          </select>
        </label>
        <label className="studio-field">
          <span>Model</span>
          <select value={form.default_model} disabled={readonly || !form.default_provider} onChange={(event) => patch('default_model', event.target.value)}>
            <option value="">选择 Model</option>
            {models.map((model) => <option value={model} key={model}>{model}</option>)}
          </select>
        </label>
      </div>
      {thinking ? (
        <label className="studio-field">
          <span>默认思考参数</span>
          <select value={form.default_thinking_value} disabled={readonly} onChange={(event) => patch('default_thinking_value', event.target.value)}>
            <option value="">使用模型默认值</option>
            {thinking.allowed_values.map((value) => <option value={value} key={value}>{value}</option>)}
          </select>
        </label>
      ) : null}
      <section className="capability-section">
        <header><strong>本地 Tools</strong><small>按副作用分组</small></header>
        {toolGroups.map(([group, tools]) => (
          <details key={group} open={group === 'read_only'}>
            <summary>{sideEffectLabel(group)} <small>{tools.length}</small></summary>
            <div className="capability-list">
              {tools.map((tool) => (
                <label className={`capability-item ${tool.assignable ? '' : 'is-disabled'}`} key={tool.name}>
                  <input type="checkbox" checked={form.tool_names.includes(tool.name)} disabled={readonly || !tool.assignable} onChange={() => toggle('tool_names', tool.name)} />
                  <span><strong>{tool.name}</strong><small>{tool.assignable ? tool.description : tool.reason || '不可分配'}</small></span>
                  {tool.requires_approval ? <em>审批</em> : null}
                </label>
              ))}
            </div>
          </details>
        ))}
      </section>
      <section className="capability-section">
        <header><strong>MCP 服务</strong><small>外部副作用</small></header>
        <div className="capability-list">
          {capabilities?.mcp_servers.length ? capabilities.mcp_servers.map((mcp) => {
            const selected = form.mcp_server_names.includes(mcp.name);
            return (
              <label className={`capability-item mcp-${mcp.status}`} key={mcp.name}>
                <input type="checkbox" checked={selected} disabled={readonly || (mcp.status === 'disabled' && !selected)} onChange={() => toggle('mcp_server_names', mcp.name)} />
                <span><strong>{mcp.name}</strong><small>{mcp.description || mcp.status}</small></span>
                <em>{mcp.status}{mcp.requires_approval ? ' · 审批' : ''}</em>
              </label>
            );
          }) : <p className="studio-empty-copy">尚未配置 MCP 服务。</p>}
        </div>
      </section>
      <div className="studio-config-footer">
        <span>{dirty ? '存在未保存修改' : '配置已同步'}</span>
        <button type="submit" className="studio-button primary" disabled={readonly || saving || !dirty}>
          <Save size={15} />{saving ? '保存中…' : '保存 revision'}
        </button>
      </div>
    </form>
  );
}

function toForm(agent: AgentDetail): FormState {
  return {
    name: agent.name,
    description: agent.description || '',
    system_prompt: agent.system_prompt || '',
    default_provider: agent.default_provider || '',
    default_model: agent.default_model || '',
    default_thinking_value: agent.default_thinking_value || '',
    tool_names: agent.tool_names || [],
    mcp_server_names: agent.mcp_server_names || [],
  };
}

function sideEffectLabel(value: string) {
  return {
    read_only: '只读',
    workspace_mutation: '工作区变更',
    runtime_mutation: '运行态变更',
    external_mutation: '外部变更',
  }[value] || value;
}

function PanelNotice({ title, detail }: { title: string; detail: string }) {
  return <div className="studio-panel-loading"><Sparkles size={18} /><strong>{title}</strong><span>{detail}</span></div>;
}
