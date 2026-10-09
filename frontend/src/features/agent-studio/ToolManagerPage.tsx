import { useEffect, useRef, useState } from 'react';
import { Code2, Plus, Save, Play, Upload } from 'lucide-react';
import { apiJson, apiRequest } from '../../api/client';
import { ConnectionEditor } from './ConnectionEditor';
import { useResourceSearch } from './resourceUi';
import type { CodeToolSummary } from './CodeToolPicker';

type Definition = { name: string; call_name?: string; description: string; input_schema: Record<string, unknown>; entrypoint: string; timeout_seconds: number; credential_fields: string[] };
type Detail = CodeToolSummary & { can_manage?: boolean; definition: Definition; files: Record<string, string> };
type Preview = { token: string; tool_id: string; call_name: string; definition: Definition; files: Record<string, string> };
type TestResult = { test_id: string; status: string; result?: unknown };
const initial = (): Definition => ({ name: '', call_name: '', description: '', input_schema: { type: 'object', properties: {}, additionalProperties: false }, entrypoint: 'main.py', timeout_seconds: 30, credential_fields: [] });
const example = 'def execute(arguments: dict) -> dict:\n    # 在这里执行文件操作、网络请求或其他业务逻辑。\n    return {"result": arguments}\n';
const labels: Record<string, string> = { running: '执行中', completed: '成功', failed: '失败', cancelled: '已取消', interrupted: '服务重启中断' };

export function ToolManagerPage({ active = true, admin = false }: { active?: boolean; admin?: boolean }) {
  const [scope, setScope] = useState('private');
  const [query, setQuery] = useState('');
  const search = useResourceSearch(query);
  const [page, setPage] = useState(0);
  const [rows, setRows] = useState<CodeToolSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [record, setRecord] = useState<Detail | null>(null);
  const [editing, setEditing] = useState(false);
  const [definition, setDefinition] = useState(initial);
  const [schema, setSchema] = useState(JSON.stringify(initial().input_schema, null, 2));
  const [files, setFiles] = useState<Record<string, string>>({ 'main.py': example });
  const [file, setFile] = useState('main.py');
  const [newFile, setNewFile] = useState('');
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [references, setReferences] = useState<{ agent_id: string; name: string }[] | null>(null);
  const [argumentsText, setArguments] = useState('{}');
  const [directory, setDirectory] = useState('');
  const [connections, setConnections] = useState<string[] | null>([]);
  const [test, setTest] = useState<TestResult | null>(null);
  const selection = useRef(0);
  const testRequest = useRef<{ identity: string; payload: object } | null>(null);
  const readonly = scope === 'platform' || record?.visibility === 'public' || !!record?.archived || !!record?.disabled;
  const running = test?.status === 'running';
  const canLeave = () => !dirty || window.confirm('工具有未保存修改，放弃修改？');
  const patch = (value: Partial<Definition>) => { setDefinition(current => ({ ...current, ...value })); setDirty(true); setPreview(null); };
  const action = async (fn: () => Promise<void>) => { setBusy(true); setError(''); setNotice(''); try { await fn(); } catch (reason) { setError(reason instanceof Error ? reason.message : '工具操作失败'); } finally { setBusy(false); } };
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void apiRequest<{ tools: CodeToolSummary[]; total: number }>(`/api/tools?${new URLSearchParams({ scope, q: search, offset: String(page * 50) })}`, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) { setRows(value.tools); setTotal(value.total); } }).catch(() => { if (!controller.signal.aborted) setError('工具目录加载失败'); });
    return () => controller.abort();
  }, [active, scope, search, page, refresh]);
  useEffect(() => {
    if (!active || !test || test.status !== 'running') return;
    const controller = new AbortController();
    let timer = 0;
    const poll = async () => {
      try {
        const result = await apiRequest<TestResult>(`/api/tool-tests/${test.test_id}`, { signal: controller.signal });
        if (!controller.signal.aborted) { setTest(current => current?.test_id === result.test_id && current.status === 'running' ? result : current); if (result.status === 'running') timer = window.setTimeout(poll, 700); else testRequest.current = null; }
      } catch { if (!controller.signal.aborted) setError('测试状态读取失败，可重新查看'); }
    };
    timer = window.setTimeout(poll, 700);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [active, test?.test_id, test?.status]);
  const load = async (item: CodeToolSummary) => {
    if (!canLeave()) return;
    const generation = ++selection.current;
    if (item.visibility === 'platform') { setRecord(null); setEditing(false); setNotice(`${item.name}：${item.description}。平台工具由代码维护，可从 Agent 配置选择。`); return; }
    await action(async () => {
      const detail = await apiRequest<Detail>(`/api/tools/${item.tool_id}`);
      if (generation !== selection.current) return;
      setRecord(detail); setDefinition(detail.definition); setSchema(JSON.stringify(detail.definition.input_schema, null, 2)); setFiles(detail.files); setFile(detail.definition.entrypoint); setDirty(false); setEditing(true); setPreview(null); setTest(null); setReferences(null); setConnections([]); testRequest.current = null;
    });
  };
  const state = (key: string, value: boolean) => void action(async () => {
    if (!record || !window.confirm('确认修改该工具的可用状态？')) return;
    const updated = await apiJson<CodeToolSummary>(`/api/tools/${record.tool_id}/state`, 'POST', { expected_revision: record.revision, [key]: value });
    setRecord(current => current ? { ...current, ...updated } : current); setRefresh(value => value + 1);
  });
  const save = () => void action(async () => {
    // 旧工具未填写时省略新字段；新建或主动修改必须满足调用命名规则。
    if ((!record || definition.call_name !== undefined) && !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(definition.call_name || '')) throw new Error('调用名称必须为 1—64 个英文、数字或下划线，且不能以数字开头。');
    if (definition.call_name?.startsWith('mcp__') || definition.call_name?.startsWith('code_tool_')) throw new Error('调用名称不能使用 mcp__ 或 code_tool_ 保留前缀。');
    const savedDefinition = { ...definition, input_schema: JSON.parse(schema) };
    const saved = await apiJson<CodeToolSummary>(record ? `/api/tools/${record.tool_id}` : '/api/tools', record ? 'PUT' : 'POST', { definition: savedDefinition, files, expected_revision: record?.revision });
    setRecord({ ...saved, definition: savedDefinition, files }); setDirty(false); setRefresh(value => value + 1); setNotice('工具已保存；新执行使用已保存的调用名称，公共工具需重新发布。'); setTest(null); testRequest.current = null;
  });
  return <section className="resource-manager code-tool-manager" aria-label="工具管理">
    <header className="resource-page-header"><div><span className="resource-eyebrow">能力 / 工具</span><h1><Code2 size={26} />工具</h1><p>把 Python 代码变成 Agent 可以调用、团队可以复用的能力。</p></div><button className="studio-button primary" disabled={busy || running} onClick={() => { if (!canLeave()) return; ++selection.current; setScope('private'); setRecord(null); setEditing(true); setDefinition(initial()); setSchema(JSON.stringify(initial().input_schema, null, 2)); setFiles({ 'main.py': example }); setFile('main.py'); setDirty(true); setPreview(null); setTest(null); setReferences(null); setConnections([]); }}><Plus size={16} />新建工具</button></header>
    <div className="resource-toolbar"><select aria-label="工具来源" value={scope} disabled={busy || running} onChange={event => { if (!canLeave()) return; ++selection.current; setScope(event.target.value); setPage(0); setEditing(false); setDirty(false); setPreview(null); }}><option value="private">我的工具</option><option value="public">公共工具</option><option value="platform">平台工具</option></select><input aria-label="搜索工具" placeholder="搜索名称或用途" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} /></div>
    {error && <div role="alert" className="studio-notice tone-danger">{error}</div>}{notice && <div role="status" className="studio-notice">{notice}</div>}
    <div className="code-tool-workspace"><aside className="code-tool-list" aria-label="工具目录">{rows.map(item => <button className={`code-tool-card ${record?.tool_id === item.tool_id ? 'is-active' : ''}`} disabled={busy || running} key={item.tool_id} onClick={() => void load(item)}><strong>{item.name}</strong><span>{item.description}</span><small>{item.disabled ? '已禁用' : item.withdrawn ? '已撤回' : item.archived ? '已归档' : item.visibility === 'platform' ? '平台只读' : item.visibility === 'public' ? '公共发布' : '私人'}</small></button>)}{!rows.length && <p>暂无工具</p>}<div className="inline-actions"><button className="studio-button" disabled={!page} onClick={() => setPage(page - 1)}>上一页</button><span>{page + 1} 页 · {total} 个</span><button className="studio-button" disabled={(page + 1) * 50 >= total} onClick={() => setPage(page + 1)}>下一页</button></div></aside>
    {editing ? <main className="code-tool-editor"><div className="config-field-grid">
      <label className="studio-field">工具名称<input value={definition.name} maxLength={100} disabled={readonly || busy} onChange={e => patch({ name: e.target.value })} /></label>
      <label className="studio-field">调用名称<input aria-label="调用名称" value={definition.call_name || ''} maxLength={64} placeholder="test_tool" disabled={readonly || busy} onChange={e => patch({ call_name: e.target.value })} /><small>{definition.call_name ? 'Agent 使用此名称调用；同一 Agent 内不能与其他工具重名。' : record ? `当前调用名称：${record.call_name || '旧版 ID 派生名称'}；填写并保存后供新执行使用。` : '必填；英文、数字、下划线，不以数字开头。'}</small></label>
      <label className="studio-field">执行超时（秒）<input type="number" min={1} max={300} value={definition.timeout_seconds} disabled={readonly || busy} onChange={e => patch({ timeout_seconds: Number(e.target.value) })} /></label>
      <label className="studio-field resource-wide">用途与调用说明<textarea rows={3} value={definition.description} maxLength={2000} disabled={readonly || busy} onChange={e => patch({ description: e.target.value })} /></label>
      <label className="studio-field">Python 入口<select value={definition.entrypoint} disabled={readonly || busy} onChange={e => patch({ entrypoint: e.target.value })}>{Object.keys(files).map(name => <option key={name}>{name}</option>)}</select></label>
      <label className="studio-field">凭证字段（每行一个）<textarea rows={2} value={definition.credential_fields.join('\n')} disabled={readonly || busy} onChange={e => patch({ credential_fields: e.target.value.split('\n').filter(Boolean) })} /><small>代码通过 CODEPILOT_TOOL_CREDENTIAL_字段名 读取；值由使用者单独配置。</small></label>
    </div>
    <ParameterEditor schema={schema} disabled={readonly || busy} onChange={value => { setSchema(value); setDirty(true); setPreview(null); }} />
    <section className="config-module"><header className="config-module-heading"><strong>Python 文件</strong><select aria-label="当前代码文件" value={file} onChange={e => setFile(e.target.value)}>{Object.keys(files).map(name => <option key={name}>{name}</option>)}</select></header>
      {!readonly && <div className="resource-toolbar"><input aria-label="新文件路径" placeholder="helpers.py" value={newFile} onChange={e => setNewFile(e.target.value)} /><button className="studio-button" disabled={busy || !newFile.endsWith('.py') || newFile in files} onClick={() => { setFiles(value => ({ ...value, [newFile]: '' })); setFile(newFile); setNewFile(''); setDirty(true); }}>添加文件</button><label className="studio-button"><Upload size={14} />上传 Python<input type="file" accept=".py" multiple hidden disabled={busy} onChange={event => { const uploaded = Array.from(event.target.files || []); void action(async () => { const incoming: Record<string, string> = {}; for (const item of uploaded) { if (item.size > 1024 * 1024) throw new Error('单文件最多 1 MiB'); incoming[item.name] = await item.text(); } setFiles(current => ({ ...current, ...incoming })); setDirty(true); setPreview(null); }); event.target.value = ''; }} /></label><button className="studio-button" disabled={busy || file === definition.entrypoint} onClick={() => { setFiles(current => { const next = { ...current }; delete next[file]; return next; }); setFile(definition.entrypoint); setDirty(true); setPreview(null); }}>删除当前文件</button></div>}
      <textarea className="code-tool-source" aria-label="Python 代码" spellCheck={false} rows={16} value={files[file] || ''} disabled={readonly || busy} onChange={event => { setFiles(current => ({ ...current, [file]: event.target.value })); setDirty(true); setPreview(null); }} />
    </section>
    <div className="inline-actions">{!readonly && <button className="studio-button primary" disabled={busy || running} onClick={save}><Save size={15} />保存工具</button>}
      {record?.visibility === 'private' && !readonly && <><button className="studio-button" disabled={dirty || busy || running} onClick={() => void action(async () => setPreview(await apiJson<Preview>(`/api/tools/${record.tool_id}/publication-preview`, 'POST', { expected_revision: record.revision }))) }>预览发布</button><button className="studio-button" disabled={busy || running} onClick={() => state('archived', true)}>归档工具</button></>}
      {record?.visibility === 'public' && record.can_manage && <button className="studio-button" disabled={busy || running} onClick={() => state('withdrawn', !record.withdrawn)}>{record.withdrawn ? '恢复公开' : '撤回发布'}</button>}
      {admin && record && <button className="studio-button" disabled={busy || running} onClick={() => state('disabled', !record.disabled)}>{record.disabled ? '解除禁用' : '管理员禁用'}</button>}
      {record && <button className="studio-button" disabled={busy} onClick={() => void action(async () => { const result = await apiRequest<{ agents: { agent_id: string; name: string }[] }>(`/api/tools/${record.tool_id}/references`); setReferences(result.agents); })}>查看引用</button>}
    </div>
    {references && <section className="config-module"><strong>当前用户可见的 Agent 引用</strong>{references.map(agent => <p key={agent.agent_id}><a href={`#/agents/${encodeURIComponent(agent.agent_id)}/config`}>{agent.name}</a></p>)}{!references.length && <p>暂无引用。请从 Agent 配置绑定工具。</p>}</section>}
    {preview && <section className="config-module"><h2>发布预览</h2><p>调用名称：{preview.call_name}</p><p>确认公开以下代码、调用说明、参数和凭证字段名称。私人凭证与执行数据不发布。</p><pre className="code-tool-preview">{JSON.stringify({ definition: preview.definition, files: preview.files }, null, 2)}</pre><button className="studio-button primary" disabled={busy || dirty} onClick={() => void action(async () => { const published = await apiJson<CodeToolSummary>(`/api/tools/${record!.tool_id}/publish`, 'POST', { token: preview.token }); setPreview(null); setNotice(`已发布 ${published.name}。其他人可在 Agent 配置中选择公共工具。`); })}>确认发布</button></section>}
    {record && !record.archived && !record.withdrawn && !record.disabled && <section className="config-module"><h2>测试已保存版本</h2><p>测试会真实执行代码；请先确认参数和工作目录。</p><label className="studio-field">测试参数 JSON<textarea rows={4} value={argumentsText} disabled={running} onChange={e => { setArguments(e.target.value); testRequest.current = null; }} /></label><label className="studio-field">测试工作目录<input value={directory} disabled={running} placeholder="留空使用平台默认目录" onChange={e => { setDirectory(e.target.value); testRequest.current = null; }} /></label>
      {definition.credential_fields.length > 0 && <ConnectionEditor key={record.tool_id} targetFilter={`tool:${record.tool_id}`} selected={connections} disabled={!!running} onChange={value => { setConnections(value); testRequest.current = null; }} />}
      <div className="inline-actions"><button className="studio-button" disabled={busy || dirty || running} onClick={() => void action(async () => { if (!testRequest.current) testRequest.current = { identity: record.tool_id, payload: { version: record.version, client_request_id: crypto.randomUUID(), arguments: JSON.parse(argumentsText), working_directory: directory || null, connection_id: connections?.[0] || null } }; const result = await apiJson<TestResult>(`/api/tools/${testRequest.current.identity}/tests`, 'POST', testRequest.current.payload); setTest(result); if (result.status !== 'running') testRequest.current = null; })}><Play size={15} />运行测试</button>{running && <button className="studio-button" onClick={() => void action(async () => { setTest(await apiJson<TestResult>(`/api/tool-tests/${test!.test_id}/cancel`, 'POST')); testRequest.current = null; })}>取消测试</button>}</div>
      {test && <div role="status"><p>测试：{labels[test.status] || test.status}</p>{test.result != null && <pre className="code-tool-preview">{JSON.stringify(test.result, null, 2)}</pre>}</div>}
    </section>}
    </main> : <div className="code-tool-empty"><Code2 size={40} /><h2>为 Agent 增加一项能力</h2><p>选择工具查看详情，或编写一个新的 Python 工具。</p><p>MCP 服务继续从 Agent 配置中按服务选择。</p></div>}
    </div>
  </section>;
}

function ParameterEditor({ schema, disabled, onChange }: { schema: string; disabled: boolean; onChange: (value: string) => void }) {
  const [name, setName] = useState('');
  const [type, setType] = useState('string');
  const [required, setRequired] = useState(false);
  const [description, setDescription] = useState('');
  let parsed: Record<string, any> | null = null;
  try { parsed = JSON.parse(schema); } catch { /* 高级编辑允许暂时无效的 JSON，保存时再校验。 */ }
  return <section className="config-module"><h2>输入参数</h2>{!disabled && <div className="resource-toolbar"><input aria-label="参数名称" placeholder="参数名称" value={name} onChange={e => setName(e.target.value)} /><select aria-label="参数类型" value={type} onChange={e => setType(e.target.value)}>{['string', 'integer', 'number', 'boolean', 'object', 'array'].map(value => <option key={value}>{value}</option>)}</select><input aria-label="参数说明" placeholder="参数说明" value={description} onChange={e => setDescription(e.target.value)} /><label><input type="checkbox" checked={required} onChange={e => setRequired(e.target.checked)} />必填</label><button className="studio-button" disabled={!parsed || !/^[A-Za-z][A-Za-z0-9_]*$/.test(name)} onClick={() => { const next = { ...parsed, type: 'object', properties: { ...(parsed?.properties || {}), [name]: { type, description } }, required: Array.from(new Set([...(parsed?.required || []).filter((key: string) => key !== name), ...(required ? [name] : [])])) }; onChange(JSON.stringify(next, null, 2)); setName(''); setDescription(''); }}>添加参数</button></div>}
    <details><summary>高级 JSON Schema</summary><textarea className="code-tool-source" aria-label="输入参数 Schema" rows={10} value={schema} disabled={disabled} onChange={e => onChange(e.target.value)} /></details><pre className="code-tool-preview">{parsed ? JSON.stringify(parsed.properties || {}, null, 2) : 'JSON 尚未完成'}</pre>
  </section>;
}
