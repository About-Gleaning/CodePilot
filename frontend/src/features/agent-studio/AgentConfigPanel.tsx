import { FormEvent, useEffect, useMemo, useState } from 'react';
import {
  Archive, ArrowLeft, Brain, Cable, CircleAlert, Copy, FileText,
  Gauge, MoreHorizontal, RotateCcw, Save, Settings2, Sparkles, Wrench,
} from 'lucide-react';

import { ApiError, apiJson, apiRequest } from '../../api/client';
import { AgentErrorDetails } from './AgentErrorDetails';
import type { AgentCapabilities, AgentDetail, AgentSummary } from './types';
import { AgentCapabilityPicker, type CapabilityPickerItem } from './AgentCapabilityPicker';
import { CodeToolPicker } from './CodeToolPicker';
import { MemoryEditor } from './MemoryEditor';
import { SkillEditor } from './SkillEditor';
import { ConnectionEditor } from './ConnectionEditor';
import { WorkingDirectoryPicker } from './WorkingDirectoryPicker';
import { hookStages, hookStageLabels } from './resourceUi';

type FormState = {
  launch_modes: Array<'direct' | 'delegated'>;
  can_delegate: boolean;
  max_iterations: number;
  readonly: boolean;
  memory_enabled: boolean;
  hook_ids: string[] | null;
  hook_parameters: Record<string, Record<string, string | number | boolean>>;
  delegate_agent_ids: string[] | null;
  skill_ids: string[] | null;
  working_directory: string | null;
  connection_ids: string[] | null;
  name: string;
  description: string;
  system_prompt: string;
  default_provider: string;
  default_model: string;
  default_thinking_value: string;
  tool_ids: string[];
  tool_names: string[];
  mcp_server_names: string[];
};

type SectionId = 'overview' | 'instructions' | 'capabilities' | 'runtime' | 'memory' | 'connections';

const SECTION_META: Array<{ id: SectionId; label: string; description: string; icon: typeof Settings2 }> = [
  { id: 'overview', label: '概览', description: '身份与模型', icon: Settings2 },
  { id: 'instructions', label: '指令', description: 'System Prompt', icon: FileText },
  { id: 'capabilities', label: '能力', description: '工具与资源', icon: Wrench },
  { id: 'runtime', label: '运行', description: '轮数与目录', icon: Gauge },
  { id: 'memory', label: '记忆', description: '开关与内容', icon: Brain },
  { id: 'connections', label: '连接', description: '身份与凭证', icon: Cable },
];

const EMPTY_FORM: FormState = {
  launch_modes: ['direct'], can_delegate: false, max_iterations: 50, readonly: false, memory_enabled: true,
  hook_ids: null, hook_parameters: {}, delegate_agent_ids: null, skill_ids: null,
  working_directory: null, connection_ids: null, name: '', description: '', system_prompt: '',
  default_provider: '', default_model: '', default_thinking_value: '', tool_ids: [], tool_names: [], mcp_server_names: [],
};

export function AgentConfigPanel({
  agent, activeRunCount, onBack, onSaved, onDirtyChange, onPublish,
}: {
  agent: AgentSummary | null;
  onPublish?: (identity: string) => void;
  activeRunCount: number;
  onBack: () => void;
  onSaved: (agent: AgentSummary, created: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [capabilities, setCapabilities] = useState<AgentCapabilities | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [mode, setMode] = useState<'edit' | 'create' | 'copy'>(agent ? 'edit' : 'create');
  const [activeSection, setActiveSection] = useState<SectionId>('overview');
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saveFailure, setSaveFailure] = useState<ApiError | null>(null);

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => { void apiRequest<AgentCapabilities>('/api/agent-capabilities', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setCapabilities(value); }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); }); };
    window.addEventListener('resources-changed', refresh);
    return () => { controller.abort(); window.removeEventListener('resources-changed', refresh); };
  }, []);
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
    setLoading(true); setError(''); setSaveFailure(null); setDirty(false); setActiveSection('overview'); setMode(agent ? 'edit' : 'create');
    const requests: [Promise<AgentCapabilities>, Promise<AgentDetail | null>] = [
      apiRequest<AgentCapabilities>('/api/agent-capabilities', { signal: controller.signal }),
      agent ? apiRequest<AgentDetail>(`/api/agents/${encodeURIComponent(agent.agent_id)}`, { signal: controller.signal }) : Promise.resolve(null),
    ];
    void Promise.all(requests).then(([nextCapabilities, nextDetail]) => {
      if (controller.signal.aborted) return;
      setCapabilities(nextCapabilities); setDetail(nextDetail); setForm(nextDetail ? toForm(nextDetail) : EMPTY_FORM); setLoading(false);
    }).catch((reason) => {
      if (!controller.signal.aborted) { setLoading(false); setError(reason instanceof Error ? reason.message : '配置加载失败'); }
    });
    return () => controller.abort();
  }, [agent?.agent_id]);

  const provider = capabilities?.providers.find((item) => item.provider === form.default_provider);
  const models = provider?.models || [];
  const thinking = provider?.model_capabilities?.[form.default_model]?.thinking;
  const readonly = mode === 'edit' && ((detail?.visibility || (detail?.source === 'builtin' ? 'builtin' : 'private')) !== 'private' || detail?.validation_status === 'invalid');
  const issuesBySection = useMemo(() => {
    const result = Object.fromEntries(SECTION_META.map((section) => [section.id, 0])) as Record<SectionId, number>;
    for (const issue of detail?.validation_issues || []) result[issueSection(issue.field)] += 1;
    return result;
  }, [detail?.validation_issues]);

  const patch = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value })); setDirty(true); setError('');
  };
  const toggle = (key: 'tool_names' | 'mcp_server_names', value: string) => {
    const selected = form[key].includes(value);
    patch(key, selected ? form[key].filter((item) => item !== value) : [...form[key], value]);
    if (key === 'tool_names' && value === 'task' && selected) setForm((current) => ({ ...current, can_delegate: false }));
  };
  const copy = () => {
    if (!detail) return;
    setMode('copy'); setActiveSection('overview');
    setForm({ ...toForm(detail), name: '', connection_ids: detail.connection_ids ? detail.connection_ids.filter((id) => id.startsWith('team:')) : null, tool_names: [...(detail.tool_names || [])] });
    setDirty(true); setError('');
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true); setError(''); setSaveFailure(null);
    try {
      const editing = mode === 'edit' && detail;
      const result = await apiJson<AgentDetail>(editing ? `/api/agents/${encodeURIComponent(detail.agent_id)}` : '/api/agents', editing ? 'PUT' : 'POST', editing ? { ...form, expected_revision_id: detail.revision_id } : form);
      setDetail(result); setForm(toForm(result)); setMode('edit'); setDirty(false); onSaved(result, !editing);
    } catch (reason) { if (reason instanceof ApiError) setSaveFailure(reason); else setError(reason instanceof Error ? reason.message : '保存失败'); }
    finally { setSaving(false); }
  };
  const archiveOrRestore = async () => {
    if (!detail || saving) return;
    setSaving(true); setError('');
    try {
      const action = detail.archived ? 'restore' : 'archive';
      const result = await apiJson<AgentDetail>(`/api/agents/${encodeURIComponent(detail.agent_id)}/${action}`, 'POST');
      setDetail(result); setForm(toForm(result)); setDirty(false); onSaved(result, false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败'); }
    finally { setSaving(false); }
  };

  if (loading) return <PanelNotice title="载入配置" detail="正在读取能力目录和 Agent revision…" />;

  const hookItems: CapabilityPickerItem[] = [
    ...(capabilities?.hooks || []).map((hook) => ({
      id: hook.hook_id, title: hook.name || hook.hook_id, description: hookStageLabels[hookStages.indexOf(hook.hook_type)] || hook.hook_type,
      group: hook.hook_id.startsWith('personal:') ? '个人' : '平台', status: hook.plugin_type === 'agent' ? '尚不支持' : hook.available ? undefined : '待配置',
      selected: form.hook_ids === null ? hook.available && !hook.hook_id.startsWith('personal:') : form.hook_ids.includes(hook.hook_id),
      disabled: form.hook_ids === null || hook.plugin_type === 'agent', attention: !hook.available || hook.plugin_type === 'agent',
    })),
    ...(form.hook_ids || []).filter((id) => !capabilities?.hooks?.some((hook) => hook.hook_id === id)).map((id) => ({ id, title: id, status: '待配置', selected: true, attention: true })),
  ];
  const toolItems: CapabilityPickerItem[] = [
    ...(capabilities?.tools || []).map((tool) => ({
      id: tool.name, title: tool.name, description: tool.assignable ? tool.description : tool.reason || '不可分配', group: sideEffectLabel(tool.side_effect || 'read_only'),
      selected: form.tool_names.includes(tool.name), disabled: !tool.assignable && !form.tool_names.includes(tool.name),
      attention: !tool.assignable, approval: tool.requires_approval,
    })),
    ...form.tool_names.filter((name) => !capabilities?.tools.some((tool) => tool.name === name)).map((name) => ({ id: name, title: name, status: '待配置', selected: true, attention: true })),
  ];
  const mcpItems: CapabilityPickerItem[] = [
    ...(capabilities?.mcp_servers || []).map((mcp) => ({
      id: mcp.name, title: mcp.name, description: mcp.description || '外部 MCP 服务', status: mcp.status,
      selected: form.mcp_server_names.includes(mcp.name), disabled: mcp.status === 'disabled' && !form.mcp_server_names.includes(mcp.name),
      attention: mcp.status !== 'available', approval: mcp.requires_approval,
    })),
    ...form.mcp_server_names.filter((name) => !capabilities?.mcp_servers.some((server) => server.name === name)).map((name) => ({ id: name, title: name, status: '待配置', selected: true, attention: true })),
  ];
  const delegates = capabilities?.delegates || capabilities?.subagents || [];
  const subagentItems: CapabilityPickerItem[] = [
    ...delegates.map((child) => ({ id: child.agent_id, title: child.name, description: child.description, selected: form.delegate_agent_ids === null || form.delegate_agent_ids.includes(child.agent_id), disabled: form.delegate_agent_ids === null })),
    ...(form.delegate_agent_ids || []).filter((id) => !delegates.some((child) => child.agent_id === id)).map((id) => ({ id, title: id, status: '待配置', selected: true, attention: true })),
  ];

  return (
    <form className="studio-config config-console" onSubmit={save}>
      <header className="studio-config-heading config-console-heading">
        <div className="config-heading-copy">
          <button type="button" className="studio-icon-button config-back-button" onClick={onBack} aria-label="返回会话" title="返回会话"><ArrowLeft size={17} /></button>
          <div className="config-title-block">
            <span className="config-kicker">{mode === 'create' ? '新建配置' : mode === 'copy' ? '创建副本' : 'Agent 配置'}</span>
            <h1>{detail?.name || '创建 Agent'}</h1>
            <div className="config-title-meta">
              {detail?.revision_id ? <code>{detail.revision_id.slice(0, 12)}</code> : <span>尚未保存</span>}
              <span className={`config-state state-${detail?.validation_status || 'draft'}`}>{validationLabel(detail?.validation_status)}</span>
              {dirty ? <span className="config-dirty-dot">未保存</span> : null}
            </div>
          </div>
        </div>
        <div className="config-heading-actions">
          {detail?.visibility === 'private' && form.launch_modes.includes('direct') && !detail.archived && onPublish && <button type="button" className="studio-button" disabled={dirty || saving} onClick={() => onPublish(detail.agent_id)}>发布</button>}
          {detail ? <details className="config-action-menu"><summary role="button" className="studio-icon-button" aria-label="更多 Agent 操作" title="更多操作"><MoreHorizontal size={17} /></summary><div>
            {!detail.publication_id && <button type="button" onClick={copy}><Copy size={15} />复制为新 Agent</button>}
            {detail.visibility === 'private' ? <button type="button" onClick={() => void archiveOrRestore()}>{detail.archived ? <RotateCcw size={15} /> : <Archive size={15} />}{detail.archived ? '恢复 Agent' : '归档 Agent'}</button> : null}
          </div></details> : null}
          <button type="submit" className="studio-button primary config-primary-save" disabled={readonly || saving || !dirty}><Save size={15} />{saving ? '保存中…' : mode === 'edit' ? '保存 revision' : '保存 Agent'}</button>
        </div>
      </header>

      <div className="config-console-body">
        <aside className="config-section-rail" aria-label="Agent 配置分区">
          <div className="config-rail-progress"><span>配置进度</span><strong>{configuredSections(form, detail)}/6</strong><i style={{ '--config-progress': `${configuredSections(form, detail) / 6 * 100}%` } as React.CSSProperties} /></div>
          <nav>{SECTION_META.map((section, index) => {
            const Icon = section.icon;
            const status = sectionStatus(section.id, form, detail, issuesBySection[section.id]);
            return <button type="button" className={activeSection === section.id ? 'is-active' : ''} aria-current={activeSection === section.id ? 'step' : undefined} onClick={() => setActiveSection(section.id)} key={section.id}>
              <span className="config-step-index">{String(index + 1).padStart(2, '0')}</span><Icon size={16} /><span><strong>{section.label}</strong><small>{section.description}</small></span><em className={`status-${status.tone}`}>{status.label}</em>
            </button>;
          })}</nav>
          <footer><span>AGENT REVISION</span><small>{dirty ? '修改尚未写入配置文件' : '当前表单已与服务端同步'}</small></footer>
        </aside>

        <div className="config-mobile-section-select"><label><span>配置分区</span><select value={activeSection} onChange={(event) => setActiveSection(event.target.value as SectionId)}>{SECTION_META.map((section) => <option value={section.id} key={section.id}>{section.label} · {section.description}</option>)}</select></label></div>

        <main className="config-panel-scroll"><div className="config-panel-content">
          {activeRunCount > 0 ? <div className="studio-notice tone-cyan"><Sparkles size={14} /><span>当前有 {activeRunCount} 个 Run。保存后仅新 Run 使用新 revision，正在执行的 Run 保持不变。</span></div> : null}
          {error ? <div className="studio-notice tone-danger" role="alert">{error}</div> : null}
          {saveFailure ? <div className="studio-notice tone-danger" role="alert"><AgentErrorDetails message={`无法保存 Agent：${saveFailure.message}`} code={saveFailure.code} issues={saveFailure.issues} /></div> : null}
          {(detail?.validation_issues || []).filter((issue) => issueSection(issue.field) === activeSection).map((issue) => <div className="studio-notice tone-danger" key={`${issue.code}:${issue.message}`}><CircleAlert size={14} /><AgentErrorDetails message={issue.message} code={issue.code} issues={issue.suggestion ? [{ ...issue, message: issue.suggestion, suggestion: undefined }] : []} /></div>)}

          {activeSection === 'overview' ? <ConfigPanel eyebrow="01 / OVERVIEW" title="定义这个 Agent" description="设置启动方式、稳定身份和默认推理模型。">
            <div className="config-field-grid">
              <div className="config-mode-switches field-span-2" aria-label="启动方式">
                {([['direct', '可直接启动'], ['delegated', '可被委派']] as const).map(([modeId, label]) => <label key={modeId}><input type="checkbox" disabled={readonly} checked={form.launch_modes.includes(modeId)} onChange={(event) => {
                  const next = event.target.checked ? [...form.launch_modes, modeId] : form.launch_modes.filter((value) => value !== modeId);
                  if (next.length) patch('launch_modes', next);
                }} /><span><strong>{label}</strong><small>{modeId === 'direct' ? '可从 Agent 首页开启 Run' : '可作为 task 的目标'}</small></span></label>)}
              </div>
              <label className="studio-field"><span>名称</span><input value={form.name} disabled={mode === 'edit'} placeholder="agent-name" onChange={(event) => patch('name', event.target.value)} /></label>
              <label className="studio-field field-span-2"><span>描述</span><input aria-label="描述" value={form.description} disabled={readonly} onChange={(event) => patch('description', event.target.value)} /><small>用于 Agent 列表和委派目录</small></label>
            </div>
            <div className="config-subsection"><header><strong>默认模型</strong><span>新 Run 启动时固定到 revision</span></header><div className="config-field-grid">
              <label className="studio-field"><span>Provider</span><select value={form.default_provider} disabled={readonly} onChange={(event) => { patch('default_provider', event.target.value); setForm((current) => ({ ...current, default_model: '', default_thinking_value: '' })); }}><option value="">选择 Provider</option>{capabilities?.providers.map((item) => <option value={item.provider} key={item.provider}>{item.label}</option>)}</select></label>
              <label className="studio-field"><span>Model</span><select value={form.default_model} disabled={readonly || !form.default_provider} onChange={(event) => patch('default_model', event.target.value)}><option value="">选择 Model</option>{models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
              {thinking ? <label className="studio-field field-span-2"><span>默认思考参数</span><select value={form.default_thinking_value} disabled={readonly} onChange={(event) => patch('default_thinking_value', event.target.value)}><option value="">使用模型默认值</option>{thinking.allowed_values.map((value) => <option value={value} key={value}>{value}</option>)}</select></label> : null}
            </div></div>
          </ConfigPanel> : null}

          {activeSection === 'instructions' ? <ConfigPanel eyebrow="02 / INSTRUCTIONS" title="工作指令" description="System Prompt 决定 Agent 的职责、边界和交付方式。">
            <label className="studio-field config-prompt-field"><span>System Prompt</span><textarea rows={18} value={form.system_prompt} disabled={readonly} onChange={(event) => patch('system_prompt', event.target.value)} /><small>{form.system_prompt.length.toLocaleString()} 字符</small></label>
          </ConfigPanel> : null}

          {activeSection === 'capabilities' ? <ConfigPanel eyebrow="03 / CAPABILITIES" title="能力边界" description="按最小权限原则组装工具和资源，不可用项目保留用于修复配置。">
            <div className="config-mode-switches"><label><input type="checkbox" checked={form.readonly} disabled={readonly} onChange={(event) => patch('readonly', event.target.checked)} /><span><strong>只读模式</strong><small>限制工作区写入能力</small></span></label></div>
            <CodeToolPicker selected={form.tool_ids} disabled={readonly} connections={form.connection_ids} onChange={ids => patch('tool_ids', ids)} onConnections={ids => patch('connection_ids', ids)} />
            <SkillEditor selected={form.skill_ids} disabled={readonly} onChange={(ids) => patch('skill_ids', ids)} />
            <section className="config-module" id="agent-hooks"><header className="config-module-heading"><div><strong>执行钩子</strong><span>在模型、工具和循环节点注入受控动作</span></div><a href="#/hooks">管理执行钩子</a></header>
              <label className="config-inherit-toggle"><input type="checkbox" disabled={readonly} checked={form.hook_ids === null} onChange={(event) => patch('hook_ids', event.target.checked ? null : [])} /><span><strong>沿用平台 Hook</strong><small>个人 Hook 仍需显式选择</small></span></label>
              <AgentCapabilityPicker title="执行钩子" items={hookItems} disabled={readonly} emptyText="没有符合条件的执行钩子。" onToggle={(id) => patch('hook_ids', form.hook_ids?.includes(id) ? form.hook_ids.filter((value) => value !== id) : [...(form.hook_ids || []), id])} />
              <HookParameters form={form} capabilities={capabilities} readonly={readonly} patch={patch} />
            </section>
            <section className="config-module"><header className="config-module-heading"><div><strong>本地工具</strong><span>工具执行时仍会再次校验能力快照</span></div></header><AgentCapabilityPicker title="本地工具" items={toolItems} disabled={readonly} emptyText="没有符合条件的本地工具。" onToggle={(id) => toggle('tool_names', id)} /></section>
            <section className="config-module"><header className="config-module-heading"><div><strong>MCP 服务</strong><span>外部服务默认保留审批和身份边界</span></div></header><AgentCapabilityPicker title="MCP 服务" items={mcpItems} disabled={readonly} emptyText="尚未配置 MCP 服务。" onToggle={(id) => toggle('mcp_server_names', id)} /></section>
            {form.tool_names.includes('task') ? <section className="config-module"><header className="config-module-heading"><div><strong>Agent 委派</strong><span>仅根执行可委派，最多一层</span></div></header>
              <label className="config-inherit-toggle"><input type="checkbox" disabled={readonly} checked={form.can_delegate} onChange={(event) => patch('can_delegate', event.target.checked)} /><span><strong>允许发起委派</strong><small>还需 task 工具与目标清单同时授权</small></span></label>
              <label className="config-inherit-toggle"><input type="checkbox" disabled={readonly || !form.can_delegate} checked={form.delegate_agent_ids === null} onChange={(event) => patch('delegate_agent_ids', event.target.checked ? null : [])} /><span><strong>沿用可委派目录</strong><small>自动跟随当前用户授权目录</small></span></label>
              <AgentCapabilityPicker title="可委派 Agent" items={subagentItems} disabled={readonly || !form.can_delegate} emptyText="没有允许被委派的 Agent。" onToggle={(id) => patch('delegate_agent_ids', form.delegate_agent_ids?.includes(id) ? form.delegate_agent_ids.filter((value) => value !== id) : [...(form.delegate_agent_ids || []), id])} />
            </section> : null}
          </ConfigPanel> : null}

          {activeSection === 'runtime' ? <ConfigPanel eyebrow="04 / RUNTIME" title="运行设置" description="控制每次执行的上限和新会话默认工作目录。">
            <div className="config-field-grid"><label className="studio-field"><span>执行轮数上限</span><input type="number" min={1} max={10000} required value={form.max_iterations} disabled={readonly} onChange={(event) => patch('max_iterations', Number(event.target.value))} /><small>达到上限时 Run 以停止状态结束</small></label></div>
            {form.launch_modes.includes('direct') ? <WorkingDirectoryPicker value={form.working_directory} disabled={readonly} onChange={(path) => patch('working_directory', path)} /> : <div className="config-empty-state"><Gauge size={18} /><strong>继承父任务目录</strong><span>仅委派启动的 Agent 不单独选择工作目录。</span></div>}
          </ConfigPanel> : null}

          {activeSection === 'memory' ? <ConfigPanel eyebrow="05 / MEMORY" title="长期记忆" description="启用状态随 Agent revision 保存，记忆正文使用独立版本和保存操作。">
            <div className="config-mode-switches"><label><input type="checkbox" checked={form.memory_enabled} disabled={readonly} onChange={(event) => patch('memory_enabled', event.target.checked)} /><span><strong>为此 Agent 启用长期记忆</strong><small>关闭后不注入记忆，也不允许运行时写入</small></span></label></div>
            {detail && mode === 'edit' ? <MemoryEditor key={detail.agent_id} agentId={detail.agent_id} /> : <div className="config-empty-state"><Brain size={18} /><strong>保存 Agent 后可编辑记忆正文</strong><span>记忆内容不会复制到新 Agent。</span></div>}
          </ConfigPanel> : null}

          {activeSection === 'connections' ? <ConfigPanel eyebrow="06 / CONNECTIONS" title="连接身份" description="身份选择随 Agent revision 保存；凭证始终通过独立操作加密写入。"><ConnectionEditor selected={form.connection_ids} disabled={readonly} onChange={(ids) => patch('connection_ids', ids)} /></ConfigPanel> : null}
        </div></main>
      </div>
    </form>
  );
}

function ConfigPanel({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children: React.ReactNode }) {
  return <section className="config-panel"><header><span className="config-panel-eyebrow">{eyebrow}</span><h2>{title}</h2><p>{description}</p></header><div className="config-panel-fields">{children}</div></section>;
}

function HookParameters({ form, capabilities, readonly, patch }: {
  form: FormState; capabilities: AgentCapabilities | null; readonly: boolean;
  patch: <K extends keyof FormState>(key: K, value: FormState[K]) => void;
}) {
  const selected = capabilities?.hooks?.filter((hook) => form.hook_ids === null ? !hook.hook_id.startsWith('personal:') : form.hook_ids.includes(hook.hook_id)) || [];
  const parameters = selected.flatMap((hook) => Object.entries(hook.parameters || {}).map(([name, parameter]) => ({ hook, name, parameter })));
  if (!parameters.length) return null;
  return <div className="config-hook-parameters"><header><strong>Hook 参数</strong><span>参数随 Agent revision 保存</span></header><div className="config-field-grid">{parameters.map(({ hook, name, parameter }) => {
    const value = form.hook_parameters[hook.hook_id]?.[name];
    const change = (next: string | number | boolean) => patch('hook_parameters', { ...form.hook_parameters, [hook.hook_id]: { ...form.hook_parameters[hook.hook_id], [name]: next } });
    return <label className="studio-field" key={`${hook.hook_id}:${name}`}><span>{hook.name || hook.hook_id} · {name}</span>{parameter.type === 'boolean' ? <input type="checkbox" disabled={readonly} checked={value === true} onChange={(event) => change(event.target.checked)} /> : parameter.choices.length ? <select disabled={readonly} value={String(value ?? '')} onChange={(event) => change(event.target.value)}><option value="">选择参数</option>{parameter.choices.map((choice) => <option key={choice}>{choice}</option>)}</select> : <input disabled={readonly} type={parameter.type === 'integer' ? 'number' : 'text'} maxLength={2000} value={String(value ?? '')} onChange={(event) => change(parameter.type === 'integer' ? Number(event.target.value) : event.target.value)} />}</label>;
  })}</div></div>;
}

function toForm(agent: AgentDetail): FormState {
  return {
    launch_modes: agent.launch_modes || (agent.kind === 'subagent' ? ['delegated'] : ['direct']),
    can_delegate: agent.can_delegate ?? (agent.tool_names || []).includes('task'), max_iterations: agent.max_iterations || 50, readonly: agent.readonly || false,
    memory_enabled: agent.memory_enabled !== false, hook_ids: agent.hook_ids ?? null, hook_parameters: agent.hook_parameters || {},
    delegate_agent_ids: agent.delegate_agent_ids ?? agent.subagent_ids ?? null, skill_ids: agent.skill_ids ?? null, working_directory: agent.working_directory ?? null,
    connection_ids: agent.connection_ids ?? null, name: agent.name, description: agent.description || '', system_prompt: agent.system_prompt || '',
    default_provider: agent.default_provider || '', default_model: agent.default_model || '', default_thinking_value: agent.default_thinking_value || '',
    tool_ids: agent.tool_ids || [], tool_names: agent.tool_names || [], mcp_server_names: agent.mcp_server_names || [],
  };
}

function issueSection(field?: string | null): SectionId {
  if (!field) return 'overview';
  if (field.includes('prompt')) return 'instructions';
  if (/tool|skill|hook|mcp|subagent|readonly/.test(field)) return 'capabilities';
  if (/iteration|directory/.test(field)) return 'runtime';
  if (field.includes('memory')) return 'memory';
  if (field.includes('connection')) return 'connections';
  return 'overview';
}

function sectionStatus(id: SectionId, form: FormState, detail: AgentDetail | null, issues: number) {
  if (issues) return { label: String(issues), tone: 'danger' };
  const configured = {
    overview: Boolean(form.name && form.default_provider && form.default_model),
    instructions: Boolean(form.system_prompt.trim()),
    capabilities: form.skill_ids === null || form.hook_ids === null || form.tool_names.length + form.mcp_server_names.length > 0,
    runtime: form.max_iterations > 0,
    memory: !form.memory_enabled || Boolean(detail),
    connections: form.connection_ids === null || form.connection_ids.length > 0,
  }[id];
  return configured ? { label: '就绪', tone: 'ready' } : { label: '待完善', tone: 'muted' };
}

function configuredSections(form: FormState, detail: AgentDetail | null) {
  return SECTION_META.filter((section) => sectionStatus(section.id, form, detail, 0).tone === 'ready').length;
}

function sideEffectLabel(value: string) {
  return { read_only: '只读', workspace_mutation: '工作区变更', runtime_mutation: '运行态变更', external_mutation: '外部变更' }[value] || value;
}

function validationLabel(status?: AgentDetail['validation_status']) {
  if (!status) return '草稿';
  return { valid: '配置有效', legacy_warning: '兼容配置', invalid: '配置无效', needs_configuration: '待配置' }[status];
}

function PanelNotice({ title, detail }: { title: string; detail: string }) {
  return <div className="studio-panel-loading"><Sparkles size={18} /><strong>{title}</strong><span>{detail}</span></div>;
}
