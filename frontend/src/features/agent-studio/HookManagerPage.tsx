import { useEffect, useRef, useState } from 'react';
import { Archive, ArrowLeft, Copy, FilePlus, MoreHorizontal, Plus, Save, Play, Square, X } from 'lucide-react';
import { apiJson, apiRequest } from '../../api/client';
import { ConnectionEditor } from './ConnectionEditor';
import { WorkingDirectoryPicker } from './WorkingDirectoryPicker';
import { useResourceSearch, hookKinds, type ResourcePageProps } from './resourceUi';

type Parameter = { type: 'string' | 'integer' | 'boolean'; required: boolean; choices: string[] };
type Definition = {
  name: string; description: string; plugin_type: 'prompt' | 'command' | 'http' | 'agent'; hook_type: string;
  order: number; timeout_seconds: number; on_error: string; content: string; argv: string[];
  url: string; credential_headers: Record<string, string>; applies_to_tools: string[]; parameters: Record<string, Parameter>;
};
type RecordInfo = { hook_id: string; name: string; description: string; visibility: string; archived: boolean; revision: string; version: string; version_number?: number; updated_at?: string | null; plugin_type: string; hook_type: string };
type Detail = RecordInfo & { definition: Definition; files: Record<string, string> };
type TestResult = { test_id: string; status: string; error_code?: string; result?: { stop: boolean; fail: boolean; human: boolean; context: string[] } };
const stages = ['session.before', 'loop.before', 'llm.before', 'llm.after', 'tool.before', 'tool.after', 'loop.after', 'session.after'];
const labels = ['本次执行开始', '本轮开始', '模型请求前', '模型响应后', '工具执行前', '工具执行后', '本轮结束', '本次执行结束'];
const empty = (): Definition => ({ name: '', description: '', plugin_type: 'prompt', hook_type: 'llm.before', order: 100, timeout_seconds: 30, on_error: 'fail_session', content: '', argv: [], url: '', credential_headers: {}, applies_to_tools: [], parameters: {} });
const encode = (text: string) => btoa(Array.from(new TextEncoder().encode(text), byte => String.fromCharCode(byte)).join(''));
const testErrors: Record<string, string> = { hook_outcome_uncertain: '外部执行结果不确定，可能已产生副作用；未自动重试', hook_error: '钩子执行异常', hook_stopped: '钩子要求停止', test_failed: '测试失败', service_restarted: '服务重启，测试未重放' };

export function HookManagerPage({ onReturn, active = true }: ResourcePageProps = {}) {
  const [records, setRecords] = useState<RecordInfo[]>([]);
  const [query, setQuery] = useState('');
  const search = useResourceSearch(query);
  const [step, setStep] = useState(0);
  const [tab, setTab] = useState('config');
  const [notice, setNotice] = useState('');
  const [activeFile, setActiveFile] = useState('hook.py');
  const [status, setStatus] = useState('active');
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [record, setRecord] = useState<Detail | null>(null);
  const [definition, setDefinition] = useState<Definition>(empty);
  const [files, setFiles] = useState<Record<string, string>>({});
  const [open, setOpen] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [references, setReferences] = useState<{ agent_id: string; name: string }[]>([]);
  const [fileName, setFileName] = useState('hook.py');
  const [parameterName, setParameterName] = useState('');
  const [headerName, setHeaderName] = useState('Authorization');
  const [credentialName, setCredentialName] = useState('auth');
  const [parameters, setParameters] = useState<Record<string, string | number | boolean>>({});
  const [directory, setDirectory] = useState<string | null>(null);
  const [connections, setConnections] = useState<string[] | null>([]);
  const [toolName, setToolName] = useState('test_tool');
  const [test, setTest] = useState<TestResult | null>(null);
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  const testRequest = useRef<{ id: string; payload: object } | null>(null);
  const readonly = record?.visibility === 'platform' || !!record?.archived;
  useEffect(() => {
    if (!active || tab !== 'agents' || !record) return;
    const controller = new AbortController();
    void apiRequest<{ agents: { agent_id: string; name: string }[] }>(`/api/hooks/${record.hook_id}/references`, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setReferences(value.agents); }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [active, tab, record?.hook_id]);
  const patch = <K extends keyof Definition>(key: K, value: Definition[K]) => { setDefinition(current => ({ ...current, [key]: value })); setDirty(true); setNotice(''); testRequest.current = null; };
  const act = async (fn: () => Promise<void>) => { setBusy(true); setError(''); try { await fn(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setBusy(false); } };
  const discard = () => !dirty || window.confirm('当前 Hook 有未保存修改，放弃这些修改？');

  useEffect(() => {
    const controller = new AbortController();
    void apiRequest<{ hooks: RecordInfo[]; total: number }>(`/api/hooks?${new URLSearchParams({ q: search, status, offset: String(page * 50) })}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setRecords(value.hooks); setTotal(value.total); } })
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [search, status, page, refresh]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);
  useEffect(() => {
    if (!test || test.status !== 'running') return;
    const controller = new AbortController();
    let timer: number;
    // 串行轮询，迟到的运行态不能覆盖取消或其他终态。
    const poll = async () => {
      try {
        const value = await apiRequest<TestResult>(`/api/hook-tests/${test.test_id}`, { signal: controller.signal });
        if (!controller.signal.aborted) setTest(current => current?.test_id === value.test_id && current.status === 'running' ? value : current);
      } catch (reason) { if (!controller.signal.aborted) setError(String(reason)); }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 700);
    };
    timer = window.setTimeout(poll, 700);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [test?.test_id, test?.status]);

  const load = (item: RecordInfo) => {
    if (!discard()) return;
    const token = ++generation.current;
    void act(async () => {
      const detail = await apiRequest<Detail>(`/api/hooks/${item.hook_id}`);
      const entries = await Promise.all(Object.keys(detail.files).map(async path => {
        const response = await fetch(`/api/hooks/${item.hook_id}/files?${new URLSearchParams({ version: detail.version, path })}`, { credentials: 'same-origin' });
        if (!response.ok) throw new Error('脚本读取失败');
        return [path, await response.text()] as const;
      }));
      const refs = await apiRequest<{ agents: { agent_id: string; name: string }[] }>(`/api/hooks/${item.hook_id}/references`);
      if (generation.current !== token) return;
      setRecord(detail); setDefinition({ ...empty(), ...detail.definition }); setFiles(Object.fromEntries(entries));
      setReferences(refs.agents); setOpen(true); setDirty(false); setTest(null); setParameters({}); setConnections([]); setTab('config'); setNotice(''); setActiveFile(Object.keys(detail.files)[0] || 'hook.py'); testRequest.current = null;
    });
  };
  const save = () => void act(async () => {
    if (!definition.name.trim()) throw new Error('请填写名称。');
    if (definition.plugin_type === 'prompt' && !definition.content.trim()) throw new Error('请填写提示正文。');
    if (definition.plugin_type === 'http') {
      try { const target = new URL(definition.url); if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password) throw new Error(); }
      catch { throw new Error('请填写有效的 HTTP 服务地址，凭证请单独配置。'); }
    }
    if (definition.plugin_type === 'command' && (!definition.argv[0] || (definition.argv[1]?.startsWith('@file:') && !(definition.argv[1].slice(6) in files)))) throw new Error('请选择解释器和已存在的入口文件。');
    // 类型切换保留本地草稿，但保存版本只包含当前方式实际使用的字段。
    const { content, argv, url, credential_headers, ...common } = definition;
    const savedDefinition = { ...common, ...(definition.plugin_type === 'prompt' ? { content } : definition.plugin_type === 'command' ? { argv } : { url, credential_headers }) };
    const saved = await apiJson<RecordInfo>(record ? `/api/hooks/${record.hook_id}` : '/api/hooks', record ? 'PUT' : 'POST', {
      definition: savedDefinition, files: definition.plugin_type === 'command' ? Object.fromEntries(Object.entries(files).map(([name, content]) => [name, encode(content)])) : {}, expected_revision: record?.revision,
    });
    setRecord({ ...saved, definition, files: {} }); setDirty(false); setNotice('已保存'); setRefresh(value => value + 1); testRequest.current = null;
    window.dispatchEvent(new Event('resources-changed'));
  });

  return <section className="resource-manager redesigned-resource">
    <header className="resource-heading"><div><span className="resource-eyebrow">执行钩子</span><h1>{open ? record?.name || '新建执行钩子' : '执行钩子'}</h1></div><div className="inline-actions">{onReturn && <button className="studio-button" onClick={onReturn}><ArrowLeft size={15} />返回 Agent 配置</button>}{open ? <button className="studio-button" disabled={busy} onClick={() => { if (discard()) { setOpen(false); setDirty(false); } }}><ArrowLeft size={15} />钩子列表</button> : <button className="studio-button resource-primary" disabled={busy} onClick={() => {
      if (!discard()) return; ++generation.current; setRecord(null); setDefinition(empty()); setFiles({}); setReferences([]); setOpen(true); setDirty(false); setTest(null); setStep(0); setTab('config'); setNotice(''); setError(''); setConnections([]);
    }}><Plus size={16} />新建执行钩子</button>}</div></header>
    {!open && <>
    <div className="resource-toolbar"><input aria-label="搜索 Hook" placeholder="搜索 Hook" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} />
      <select aria-label="Hook 状态" value={status} onChange={event => { setStatus(event.target.value); setPage(0); }}><option value="active">活动</option><option value="archived">归档</option><option value="all">全部</option></select></div>
    {error && <p role="alert" className="studio-notice tone-danger">{error}</p>}
    <div className="resource-list">{records.map(item => <button key={item.hook_id} className="resource-row" disabled={busy} onClick={() => load(item)}><div><strong>{item.name}</strong><p>{item.description}</p></div><span>{hookKinds[item.plugin_type]} · {labels[stages.indexOf(item.hook_type)] || item.hook_type}<br />{item.visibility === 'platform' ? '平台 · 只读' : '个人'} · {item.archived ? '已归档' : '使用中'}</span></button>)}{!records.length && <div className="resource-empty">暂无执行钩子</div>}</div>
    <div className="inline-actions"><button className="studio-button" disabled={!page} onClick={() => setPage(value => value - 1)}>上一页</button><span>{page + 1}</span><button className="studio-button" disabled={(page + 1) * 50 >= total} onClick={() => setPage(value => value + 1)}>下一页</button></div>
    </>}
    {open && <div className="resource-detail">
      {error && <p role="alert" className="studio-notice tone-danger">{error}</p>}
      <header className="resource-editor-bar"><span role="status">{dirty ? '未保存' : notice || (readonly ? '只读' : record ? '已保存' : '新建')}</span><div className="inline-actions">
        {!readonly && (record || step === 2) && <button className="studio-button resource-primary" disabled={busy || definition.plugin_type === 'agent'} onClick={save}><Save size={16} />保存钩子</button>}
        {record && <details className="resource-menu"><summary aria-label="钩子更多操作"><MoreHorizontal size={18} /></summary><div>
        {record && <button disabled={busy || definition.plugin_type === 'agent'} onClick={() => { setRecord(null); setDefinition(value => ({ ...value, name: value.name + ' 副本', credential_headers: {} })); setDirty(true); setReferences([]); setConnections([]); setTest(null); setStep(0); setTab('config'); }}><Copy size={16} />复制为我的钩子</button>}
        {record && !readonly && <button disabled={busy} onClick={() => { if (window.confirm(`归档此 Hook？${references.length} 个 Agent 引用了它。`)) void act(async () => { await apiJson(`/api/hooks/${record.hook_id}/archive`, 'POST', { expected_revision: record.revision }); setOpen(false); setDirty(false); setRefresh(value => value + 1); window.dispatchEvent(new Event('resources-changed')); }); }}><Archive size={16} />归档</button>}
        </div></details>}
      </div></header>
      {!record && <ol className="resource-steps">{['执行方式', '触发时机', '执行内容'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined}><span>{index + 1}</span>{label}</li>)}</ol>}
      {record && <nav className="resource-tabs" aria-label="钩子详情">{[['config', '配置'], ['test', '测试'], ['agents', '引用情况']].map(([id, label]) => <button key={id} aria-current={tab === id ? 'page' : undefined} onClick={() => setTab(id)}>{label}</button>)}</nav>}
      {tab === 'config' && <>
      <fieldset disabled={busy || readonly} className="resource-fields">
        {(record || step === 0) && <>
        <label className="studio-field">名称<input value={definition.name} maxLength={100} onChange={event => patch('name', event.target.value)} /></label>
        <label className="studio-field">用途<input value={definition.description} maxLength={500} onChange={event => patch('description', event.target.value)} /></label>
        <label className="studio-field resource-wide">执行方式<select value={definition.plugin_type} onChange={event => { patch('plugin_type', event.target.value as Definition['plugin_type']); patch('hook_type', 'llm.before'); if (event.target.value === 'command' && !definition.argv.length) { patch('argv', ['python3', '@file:hook.py']); } if (event.target.value === 'command' && !definition.argv.length && !Object.keys(files).length) { setFiles({ 'hook.py': 'import json\nimport sys\n\n# 读取本次节点输入，返回继续执行。\nrequest = json.load(sys.stdin)\nprint(json.dumps({"protocol_version": 1, "action": "continue"}))\n' }); setActiveFile('hook.py'); } }}>{Object.entries(hookKinds).map(([value, label]) => <option key={value} value={value} disabled={value === 'agent'}>{label}</option>)}</select></label>
        </>}
        {(record || step === 1) && <>
        <label className="studio-field resource-wide">触发时机<select value={definition.hook_type} onChange={event => patch('hook_type', event.target.value)}>{stages.map((stage, index) => definition.plugin_type === 'prompt' && index > 2 ? null : <option key={stage} value={stage}>{labels[index]}</option>)}</select></label>
        {definition.hook_type.startsWith('tool.') && <label className="studio-field resource-wide">工具范围（每行一个，留空表示全部）<textarea value={definition.applies_to_tools.join('\n')} onChange={event => patch('applies_to_tools', event.target.value.split('\n').filter(Boolean))} /></label>}
        </>}
        {(record || step === 2) && <>
        <details className="resource-advanced resource-wide"><summary>高级设置</summary><div className="resource-fields">
        <label className="studio-field">顺序<input type="number" min={0} max={10000} value={definition.order} onChange={event => patch('order', Number(event.target.value))} /></label>
        <label className="studio-field">超时（秒）<input type="number" min={1} max={300} value={definition.timeout_seconds} onChange={event => patch('timeout_seconds', Number(event.target.value))} /></label>
        <label className="studio-field">异常策略<select value={definition.on_error} onChange={event => patch('on_error', event.target.value)}><option value="fail_session">失败结束</option><option value="break_loop">停止执行</option><option value="require_human">人工确认</option><option value="continue">继续</option></select></label>
        </div></details>
        {definition.plugin_type === 'prompt' && <label className="studio-field resource-wide">提示正文<textarea rows={8} value={definition.content} onChange={event => patch('content', event.target.value)} /></label>}
        {definition.plugin_type === 'command' && <>
          {(definition.argv[1]?.startsWith('@file:')) && <><label className="studio-field">解释器<input value={definition.argv[0]} onChange={event => patch('argv', [event.target.value, ...definition.argv.slice(1)])} /></label><label className="studio-field">入口文件<select value={definition.argv[1].slice(6)} onChange={event => patch('argv', [definition.argv[0], `@file:${event.target.value}`, ...definition.argv.slice(2)])}>{Object.keys(files).map(name => <option key={name}>{name}</option>)}</select></label><label className="studio-field resource-wide">脚本参数（每行一个）<textarea rows={2} value={definition.argv.slice(2).join('\n')} onChange={event => patch('argv', [...definition.argv.slice(0, 2), ...(event.target.value ? event.target.value.split('\n') : [])])} /></label></>}
          <details className="resource-advanced resource-wide" open={!definition.argv[1]?.startsWith('@file:')}><summary>高级命令参数</summary><label className="studio-field">命令及参数（每行一个）<textarea rows={5} value={definition.argv.join('\n')} onChange={event => patch('argv', event.target.value.split('\n'))} /></label></details>
          <div className="resource-wide"><div className="resource-toolbar"><input aria-label="脚本相对路径" value={fileName} onChange={event => setFileName(event.target.value)} /><button className="studio-button" onClick={() => { if (fileName && !(fileName in files)) { setFiles(value => ({ ...value, [fileName]: '' })); setDirty(true); } }}><FilePlus size={16} />新增脚本</button>
            <label className="studio-button">上传脚本<input aria-label="上传脚本" type="file" multiple onChange={event => void act(async () => { const additions: Record<string, string> = {}; for (const file of Array.from(event.target.files || [])) { if (file.size > 1024 * 1024) throw new Error('单文件最多 1 MiB'); additions[file.name] = await file.text(); } setFiles(value => ({ ...value, ...additions })); setDirty(true); })} /></label></div>
            <div className="resource-script-layout"><div className="resource-file-list">{Object.keys(files).map(name => <button aria-current={activeFile === name ? 'page' : undefined} key={name} onClick={() => setActiveFile(name)}>{name}</button>)}</div>{activeFile in files && <label className="studio-field"><span>{activeFile}<button className="studio-icon-button" title={`移除 ${activeFile}`} onClick={() => { setFiles(value => Object.fromEntries(Object.entries(value).filter(([key]) => key !== activeFile))); setActiveFile(Object.keys(files).find(name => name !== activeFile) || ''); setDirty(true); }}><X size={14} /></button></span><textarea className="resource-code-editor" aria-label={`脚本 ${activeFile}`} rows={16} value={files[activeFile]} onChange={event => { setFiles(value => ({ ...value, [activeFile]: event.target.value })); setDirty(true); }} /></label>}</div></div></>}
        {definition.plugin_type === 'http' && <><label className="studio-field resource-wide">HTTP 地址<input type="url" value={definition.url} onChange={event => patch('url', event.target.value)} /></label><details className="resource-advanced resource-wide"><summary>认证字段设置（可选）</summary><div className="resource-toolbar"><label className="studio-field">请求头名称<input value={headerName} onChange={event => setHeaderName(event.target.value)} /></label><label className="studio-field">凭证字段名称<input value={credentialName} onChange={event => setCredentialName(event.target.value)} /></label><button className="studio-button" disabled={!headerName.trim() || !credentialName.trim()} onClick={() => patch('credential_headers', { ...definition.credential_headers, [headerName]: credentialName })}><Plus size={16} />添加映射</button></div>{Object.entries(definition.credential_headers).map(([header, field]) => <div className="inline-actions" key={header}><span>{header} → {field}</span><button className="studio-icon-button" title={`移除 ${header}`} onClick={() => patch('credential_headers', Object.fromEntries(Object.entries(definition.credential_headers).filter(([key]) => key !== header)))}><X size={14} /></button></div>)}</details></>}
        <details className="resource-advanced resource-wide"><summary>高级参数定义</summary><div className="resource-toolbar"><input aria-label="新参数名称" value={parameterName} onChange={event => setParameterName(event.target.value)} /><button className="studio-button" disabled={!parameterName} onClick={() => { patch('parameters', { ...definition.parameters, [parameterName]: { type: 'string', required: false, choices: [] } }); setParameterName(''); }}><Plus size={16} />添加参数</button></div>
          {Object.entries(definition.parameters).map(([name, parameter]) => <div className="resource-toolbar" key={name}><strong>{name}</strong><select aria-label={`${name} 类型`} value={parameter.type} onChange={event => patch('parameters', { ...definition.parameters, [name]: { ...parameter, type: event.target.value as Parameter['type'] } })}><option value="string">文本</option><option value="integer">整数</option><option value="boolean">布尔</option></select><label><input type="checkbox" checked={parameter.required} onChange={event => patch('parameters', { ...definition.parameters, [name]: { ...parameter, required: event.target.checked } })} />必填</label><input aria-label={`${name} 枚举`} placeholder="枚举值，逗号分隔" value={parameter.choices.join(',')} onChange={event => patch('parameters', { ...definition.parameters, [name]: { ...parameter, choices: event.target.value.split(',').filter(Boolean) } })} /><button className="studio-icon-button" title={`移除参数 ${name}`} onClick={() => patch('parameters', Object.fromEntries(Object.entries(definition.parameters).filter(([key]) => key !== name)))}><X size={14} /></button></div>)}</details>
        </>}
      </fieldset>
      {record?.visibility === 'private' && definition.plugin_type === 'http' && <section className="resource-section"><h3>认证连接</h3><ConnectionEditor key={record.version} targetFilter={`hook:${record.hook_id}`} selected={connections} disabled={busy || dirty || readonly} onChange={setConnections} /></section>}
      {record?.version && <p>第 {record.version_number ?? 1} 版{record.updated_at ? ` · 更新于 ${new Date(record.updated_at).toLocaleString('zh-CN')}` : ''}</p>}
      {record && <details className="resource-advanced"><summary>技术详情</summary><p>节点：{definition.hook_type}</p>{record.version && <p>内容标识：{record.version.slice(0, 8)} <button className="studio-icon-button" title="复制完整内容标识" aria-label="复制完整内容标识" onClick={() => void act(async () => { await navigator.clipboard.writeText(record.version); setNotice('内容标识已复制'); })}><Copy size={15} /></button></p>}</details>}
      </>}
      {!record && <footer className="resource-wizard-footer"><button className="studio-button" disabled={step === 0 || busy} onClick={() => { setStep(value => value - 1); setError(''); }}><ArrowLeft size={15} />上一步</button>{step < 2 && <button className="studio-button resource-primary" disabled={busy} onClick={() => { if (!definition.name.trim()) { setError('请填写名称。'); return; } setError(''); setStep(value => value + 1); }}>下一步</button>}</footer>}
      {tab === 'agents' && record && <section className="resource-section">{references.length ? references.map(agent => <div className="resource-association" key={agent.agent_id}><strong>{agent.name}</strong></div>) : <p className="resource-muted">暂无 Agent 引用</p>}</section>}
      {tab === 'test' && dirty && <p className="studio-notice">存在未保存修改，请先在配置页保存。</p>}
      {tab === 'test' && (readonly || record?.visibility !== 'private') && <p className="resource-muted">此资源不可直接测试，请复制为个人钩子后保存。</p>}
      {tab === 'test' && record?.visibility === 'private' && !record.archived && <section className="resource-test"><h3>测试已保存版本</h3>
        <WorkingDirectoryPicker value={directory} disabled={busy} onChange={setDirectory} />
        {Object.entries(definition.parameters).map(([name, parameter]) => <label className="studio-field" key={name}>{name}{parameter.type === 'boolean' ? <input type="checkbox" checked={!!parameters[name]} onChange={event => setParameters(value => ({ ...value, [name]: event.target.checked }))} /> : <input type={parameter.type === 'integer' ? 'number' : 'text'} value={String(parameters[name] ?? '')} onChange={event => setParameters(value => ({ ...value, [name]: parameter.type === 'integer' ? Number(event.target.value) : event.target.value }))} />}</label>)}
        {definition.hook_type.startsWith('tool.') && <label className="studio-field">测试工具名<input value={toolName} onChange={event => setToolName(event.target.value)} /></label>}
        {definition.plugin_type === 'http' && <ConnectionEditor key={record.version} targetFilter={`hook:${record.hook_id}`} selected={connections} disabled={busy} onChange={setConnections} />}
        <div className="inline-actions"><button className="studio-button" disabled={busy || dirty || test?.status === 'running'} onClick={() => void act(async () => {
          if (definition.plugin_type !== 'prompt' && !window.confirm('测试将实际执行命令或发送 HTTP 请求，可能产生副作用。继续？')) return;
          if (!testRequest.current) testRequest.current = { id: record.hook_id, payload: { version: record.version, client_request_id: crypto.randomUUID(), parameters, working_directory: directory, connection_id: connections?.[0] || null, tool_name: toolName } };
          setTest(await apiJson<TestResult>(`/api/hooks/${testRequest.current.id}/tests`, 'POST', testRequest.current.payload));
          testRequest.current = null;
        })}><Play size={16} />运行测试</button>{test?.status === 'running' && <button className="studio-icon-button" title="取消测试" aria-label="取消测试" onClick={() => void act(async () => setTest(await apiJson<TestResult>(`/api/hook-tests/${test.test_id}/cancel`, 'POST')))}><Square size={16} /></button>}</div>
        {test && <div role="status"><p>状态：{({ running: '执行中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '重启后中断' } as Record<string, string>)[test.status] || test.status}{test.error_code ? ` · ${testErrors[test.error_code] || test.error_code}` : ''}</p>{test.result && <><p>动作：{test.result.fail ? '失败' : test.result.stop ? '停止' : test.result.human ? '请求人工确认' : '继续'}</p>{test.result.context.map((text, index) => <pre key={index}>{text}</pre>)}</>}</div>}
      </section>}
    </div>}
  </section>;
}
