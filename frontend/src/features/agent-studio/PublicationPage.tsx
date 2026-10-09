import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Globe, Save, Play, Upload, Download, X } from 'lucide-react';
import { apiJson, apiRequest } from '../../api/client';
import type { ApiIssue, PublicationProgress } from '../../api/client';
import { WorkingDirectoryPicker } from './WorkingDirectoryPicker';
import { ConnectionEditor } from './ConnectionEditor';
import { hookKinds, hookStages, hookStageLabels, useResourceSearch } from './resourceUi';
import { parseSkillDocument, updateSkillDocument } from './skillDocument';
import { MarkdownContent } from '../../components/MessageContent';

type Profile = { name: string; description: string; system_prompt: string; kind: string; launch_modes?: Array<'direct' | 'delegated'>; allowed_tools: string[]; tool_ids?: string[]; default_provider: string; default_model: string; max_iterations: number; readonly: boolean; memory_enabled: boolean };
type Resource = { id: string; name: string; kind: 'skill' | 'hook'; payload?: { definition?: Record<string, unknown>; files: Record<string, string> } | null };
type Publication = { id: string; name: string; description: string; author: string; owned: boolean; status: string; number: number; revision: string; profiles: Record<string, Profile>; resources: Resource[]; source_agent_id?: string; can_moderate?: boolean };
type Usage = { revision: string | null; working_directory: string | null; connections: Record<string, string[]> };
type Dependency = { tool_id: string; name: string; action: 'publish' | 'restore'; call_name: string; version: string; definition: Record<string, unknown>; files: Record<string, string> };
type PublicationError = { message: string; code?: string; issues: ApiIssue[]; progress: PublicationProgress[]; retryable: boolean };
const publicationError = (reason: unknown): PublicationError => {
  const value = reason as Partial<PublicationError> | null;
  return { message: reason instanceof Error ? reason.message : String(reason), code: value?.code, issues: value?.issues || [], progress: value?.progress || [], retryable: value?.retryable ?? !value?.code };
};
type Preview = { candidate_id: string; digest: string; expected_revision: string | null; profiles: Record<string, Profile>; resources: Resource[]; excluded: string[]; dependencies?: Dependency[] };
const base = '/api/agent-publications';
const encode = (text: string) => btoa(Array.from(new TextEncoder().encode(text), byte => String.fromCharCode(byte)).join(''));
const decode = (text: string) => new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(atob(text), c => c.charCodeAt(0)));

export function PublicationPage({ sourceAgentId, onUse, active = true }: { sourceAgentId?: string | null; active?: boolean; onUse: (identity: string) => Promise<void> }) {
  const [items, setItems] = useState<Publication[]>([]);
  const [query, setQuery] = useState('');
  const search = useResourceSearch(query);
  const [page, setPage] = useState(0);
  const [detail, setDetail] = useState<Publication | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [usage, setUsage] = useState<Usage>({ revision: null, working_directory: null, connections: {} });
  const [tab, setTab] = useState('overview');
  const [resource, setResource] = useState<Resource | null>(null);
  const [dirty, setDirty] = useState(false);
  const [resourceDirty, setResourceDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<PublicationError | null>(null);
  const [publishSource, setPublishSource] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  const requestId = useRef(crypto.randomUUID());
  const activeRef = useRef(active);
  const sourceSeen = useRef<string | null>(null);
  activeRef.current = active;
  useEffect(() => { if (!active) { generation.current++; setBusy(false); } return () => { generation.current++; }; }, [active]);
  const discard = () => !(dirty || resourceDirty) || window.confirm('有未保存修改，放弃修改？');
  useEffect(() => {
    const controller = new AbortController();
    const token = generation.current;
    void apiRequest<{ publications: Publication[] }>(`${base}?${new URLSearchParams({ q: search, offset: String(page * 50) })}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setItems(value.publications); })
      .catch(reason => { if (!controller.signal.aborted && generation.current === token && activeRef.current) setError(publicationError(reason)); });
    return () => controller.abort();
  }, [search, page, refresh]);
  const requestPreview = async (source: string, includeDependencies = false) => {
    const token = ++generation.current;
    setPublishSource(source); setPreview(null); setDetail(null); setResource(null);
    setDirty(false); setResourceDirty(false); setBusy(true); setError(null); setNotice('');
    try {
      const value = await apiJson<Preview>(`${base}/preview`, 'POST', { source_agent_id: source, include_dependencies: includeDependencies });
      if (generation.current !== token || !activeRef.current) return;
      setPreview(value); requestId.current = crypto.randomUUID();
    } catch (reason) { if (generation.current === token && activeRef.current) setError(publicationError(reason)); }
    finally { if (generation.current === token) setBusy(false); }
  };
  useEffect(() => {
    if (!sourceAgentId || !active) return;
    if (sourceSeen.current === sourceAgentId && (preview || detail || error)) return;
    if ((dirty || resourceDirty) && !window.confirm('有未保存修改，放弃并打开发布预览？')) return;
    sourceSeen.current = sourceAgentId;
    void requestPreview(sourceAgentId.split(':')[0]);
    return () => { generation.current++; };
  }, [sourceAgentId, active]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => { if (dirty || resourceDirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty, resourceDirty]);
  const act = async (action: (token: number) => Promise<void>) => { const token = generation.current; setBusy(true); setError(null); setNotice(''); try { await action(token); } catch (reason) { if (generation.current === token && activeRef.current) setError(publicationError(reason)); } finally { if (generation.current === token) setBusy(false); } };
  const open = async (identity: string) => {
    if (!discard()) return;
    const token = ++generation.current;
    await act(async () => {
      const [value, settings] = await Promise.all([apiRequest<Publication>(`${base}/${identity}`), apiRequest<Usage>(`${base}/${identity}/usage`)]);
      if (generation.current !== token) return;
      setDetail(value); setUsage(settings); setPublishSource(null); setPreview(null); setResource(null); setTab('overview'); setDirty(false); setResourceDirty(false);
    });
  };
  const saveUsage = async () => {
    if (!detail) return;
    const saved = await apiJson<Usage>(`${base}/${detail.id}/usage`, 'PUT', { ...usage, expected_revision: usage.revision, revision: undefined });
    setUsage(saved); setDirty(false); setNotice('使用设置已保存');
  };
  return <section className="resource-manager redesigned-resource publication-page">
    <header className="resource-heading"><div><span className="resource-eyebrow">公共 Agent</span><h1>{preview ? '发布预览' : detail?.name || '公共 Agent'}</h1></div>
      {(detail || preview || publishSource) && <button className="studio-button" disabled={busy} onClick={() => { if (discard()) { generation.current++; setDetail(null); setPreview(null); setPublishSource(null); setError(null); setBusy(false); setResource(null); setDirty(false); } }}><ArrowLeft size={15} />公共列表</button>}
    </header>
    {error && <div role="alert" className="studio-notice tone-danger publication-errors"><strong>{error.message}</strong>
      {error.issues.length > 0 && <ul>{error.issues.map((issue, index) => <li key={`${issue.code}:${index}`}>
        <strong>{issue.resource_name || (issue.resource_kind === 'tool' ? '不可用工具' : '配置或资源')}</strong>：{issue.message}
        {issue.referenced_by?.length ? <p>引用 Agent：{issue.referenced_by.map(agent => agent.name).join('、')}</p> : null}
        {issue.suggestion && <p>{issue.suggestion}</p>}
        {!issue.action && issue.resource_kind && <a href={issue.resource_kind === 'tool' ? '#/tools' : issue.resource_kind === 'skill' ? '#/skills' : issue.resource_kind === 'hook' ? '#/hooks' : `#/agents/${encodeURIComponent(issue.referenced_by?.[0]?.agent_id || publishSource || '')}/config`}>查看{issue.resource_kind === 'agent' ? 'Agent 配置' : '资源管理'}</a>}
      </li>)}</ul>}
      {error.progress.length > 0 && <><p>已成功的依赖不会自动撤回；后续状态变化以资源管理页为准。本次 Agent 尚未发布，原公共 Agent 保持原样。</p><ul>{error.progress.map(item => <li key={item.resource_id}>{item.resource_name}：{item.action === 'restore' ? '恢复' : '发布'} · {item.status === 'completed' ? '已成功' : item.status === 'failed' ? '失败' : '未执行'}</li>)}</ul></>}
      {error.code && <details><summary>技术详情</summary><code>{error.code}</code></details>}
      {publishSource && <div className="inline-actions">
        {error.issues.some(issue => issue.action) && <button className="studio-button" disabled={busy} onClick={() => void requestPreview(publishSource, true)}>查看并一并发布</button>}
        <button className="studio-button" disabled={busy} onClick={() => void requestPreview(publishSource, !!preview?.dependencies?.length)}>重新检查</button>
      </div>}
    </div>}{notice && <p role="status">{notice}</p>}
    {preview ? <>
      <ProfileList profiles={preview.profiles} />
      {!!preview.dependencies?.length && <section className="resource-section"><h3>一并处理的代码工具</h3><p>确认后依次处理以下依赖，再发布 Agent。中途失败时，已成功的依赖仍然公开。</p>
        {preview.dependencies.map(item => <details key={item.tool_id}><summary>{item.name} · {item.action === 'restore' ? '恢复原公共版本' : '发布私人版本'} · {item.call_name}</summary>
          {item.action === 'restore' && <p>恢复影响所有引用此工具的 Agent，不会公开当前私人修改。</p>}
          <p>引用 Agent：{Object.values(preview.profiles).filter(profile => profile.tool_ids?.includes(item.tool_id)).map(profile => profile.name).join('、')}</p>
          <pre>{JSON.stringify(item.definition, null, 2)}</pre>{Object.entries(item.files).map(([name, content]) => <details key={name}><summary>{name}</summary><pre>{content}</pre></details>)}
        </details>)}
      </section>}
      <section className="resource-section"><h3>附属资源</h3>{preview.resources.map(item => <details key={item.id}><summary>{item.kind === 'skill' ? '技能' : '执行钩子'} · {item.name}</summary>{item.payload ? <>{item.payload.definition && <pre>{JSON.stringify(item.payload.definition, null, 2)}</pre>}{Object.entries(item.payload.files).map(([name, content]) => { let text = '二进制附件'; try { text = decode(content); } catch { /* 二进制文件只展示名称。 */ } return <details key={name}><summary>{name}</summary><pre>{text}</pre></details>; })}</> : <p>保留当前公共资源内容</p>}</details>)}</section>
      <section className="resource-section"><h3>不包含</h3><p>{preview.excluded.join('、')}</p></section>
      <button className="studio-button resource-primary" disabled={busy || !!error && !error.retryable} onClick={() => void act(async token => {
        const result = await apiJson<{ id: string }>(`${base}/confirm`, 'POST', { candidate_id: preview.candidate_id, digest: preview.digest, expected_revision: preview.expected_revision, client_request_id: requestId.current });
        if (generation.current !== token || !activeRef.current) return;
        // 公开已成功，详情加载失败不能误报“发布失败”或再次发布。
        setPreview(null); setPublishSource(null); setRefresh(n => n + 1); setNotice('已发布');
        const [value, settings] = await Promise.all([apiRequest<Publication>(`${base}/${result.id}`), apiRequest<Usage>(`${base}/${result.id}/usage`)]);
        if (generation.current !== token || !activeRef.current) return;
        setDetail(value); setUsage(settings); setDirty(false);
      })}><Upload size={16} />{busy ? '正在处理…' : error?.progress.length && error.retryable ? '重试发布' : preview.dependencies?.length ? '发布依赖并发布 Agent' : '确认发布'}</button>
    </> : detail ? <>
      <div className="resource-editor-bar"><span>{detail.author} · 第 {detail.number} 次发布 · {detail.status === 'published' ? '已公开' : detail.status === 'blocked' ? '已下架' : '已撤回'}</span><div className="inline-actions">
        {detail.owned && detail.source_agent_id && detail.status !== 'blocked' && <button className="studio-button" disabled={busy || dirty || resourceDirty} onClick={() => void requestPreview(detail.source_agent_id!)}><Upload size={15} />重新发布</button>}
        {detail.can_moderate && detail.status === 'published' && <button className="studio-button" disabled={busy} onClick={() => { if (window.confirm('下架后将阻止新执行，确认下架？')) void act(async () => { await apiJson(`${base}/${detail.id}/block`, 'POST', { expected_revision: detail.revision }); setDetail(null); setRefresh(n => n + 1); }); }}>管理员下架</button>}
        {detail.status === 'published' && <button className="studio-button resource-primary" disabled={busy || resourceDirty} onClick={() => void act(async () => { await saveUsage(); await onUse(detail.id); })}><Play size={15} />使用 Agent</button>}
        {detail.owned && detail.status === 'published' && <button className="studio-button" disabled={busy} onClick={() => {
          if (window.confirm('撤回后不再接受新执行，已有执行正常收尾。确认撤回？')) void act(async () => { await apiJson(`${base}/${detail.id}/withdraw`, 'POST', { expected_revision: detail.revision }); await open(detail.id); setRefresh(n => n + 1); });
        }}>撤回发布</button>}
      </div></div>
      <nav className="resource-tabs" aria-label="公共 Agent 详情">{[['overview', '概览'], ['resources', '配置与资源'], ['usage', '我的使用设置']].map(([id, label]) => <button key={id} aria-current={tab === id ? 'page' : undefined} onClick={() => setTab(id)}>{label}</button>)}</nav>
      {tab === 'overview' && <section className="resource-section"><p>{detail.description}</p><p>作者：{detail.author}</p></section>}
      <div hidden={tab !== 'resources'}><ProfileList profiles={detail.profiles} /><section className="resource-section"><h3>附属资源</h3>{detail.resources.map(item => <button className="resource-row" key={item.id} onClick={() => { if (!resourceDirty || window.confirm('资源有未保存修改，放弃修改？')) { setResource(item); setResourceDirty(false); } }}><strong>{item.name}</strong><span>{item.kind === 'skill' ? '技能' : '执行钩子'}</span></button>)}</section>
        {resource && <PublicResourceEditor key={`${detail.id}:${resource.id}`} publication={detail.id} resource={resource} editable={detail.owned && detail.status !== 'blocked'} onDirty={setResourceDirty} />}</div>
      {tab === 'usage' && <><WorkingDirectoryPicker value={usage.working_directory} disabled={busy} onChange={value => { setUsage({ ...usage, working_directory: value }); setDirty(true); }} />
        {Object.entries(detail.profiles).map(([id, profile]) => <section className="resource-section" key={id}><h3>{profile.name} · 连接</h3><ConnectionEditor publicationId={detail.id} selected={usage.connections[id] || []} disabled={busy} onChange={ids => { setUsage({ ...usage, connections: { ...usage.connections, [id]: ids || [] } }); setDirty(true); }} /></section>)}
        <button className="studio-button resource-primary" disabled={busy} onClick={() => void act(saveUsage)}><Save size={15} />保存使用设置</button></>}
    </> : publishSource ? <p role="status">{busy ? '正在检查发布内容…' : '请处理上述问题后重新检查。'}</p> : <><div className="resource-toolbar"><input aria-label="搜索公共 Agent" placeholder="搜索名称或用途" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} /></div>
      <div className="resource-list">{items.map(item => <button key={item.id} className="resource-row" disabled={busy} onClick={() => void open(item.id)}><Globe size={18} /><div><strong>{item.name}</strong><p>{item.description}</p></div><span>{item.author} · 第 {item.number} 次发布</span></button>)}{!items.length && <p className="resource-empty">暂无公共 Agent</p>}</div>
      <div className="resource-pagination"><button className="studio-button" disabled={!page || busy} onClick={() => setPage(n => n - 1)}>上一页</button><span>第 {page + 1} 页</span><button className="studio-button" disabled={items.length < 50 || busy} onClick={() => setPage(n => n + 1)}>下一页</button></div></>}
  </section>;
}

function ProfileList({ profiles }: { profiles: Record<string, Profile> }) {
  return <>{Object.entries(profiles).map(([id, profile]) => <section className="resource-section" key={id}><h3>{profile.name} · {(profile.launch_modes || (profile.kind === 'agent' ? ['direct'] : ['delegated'])).map(mode => mode === 'direct' ? '可直接启动' : '可被委派').join(' / ')}</h3><p>{profile.description}</p><p>{profile.default_provider} / {profile.default_model} · {profile.max_iterations} 轮 · {profile.readonly ? '只读' : '可写'}</p><p>工具：{[...profile.allowed_tools, ...(profile.tool_ids || [])].join('、') || '无'}</p><details><summary>指令</summary><MarkdownContent className="resource-preview" text={profile.system_prompt} /></details></section>)}</>;
}

type HookDefinition = { name: string; description: string; plugin_type: string; hook_type: string; content: string; argv: string[]; url: string; order: number; timeout_seconds: number; on_error: string; applies_to_tools: string[]; parameters: Record<string, unknown>; credential_headers: Record<string, string> };
function PublicResourceEditor({ publication, resource, editable, onDirty }: { publication: string; resource: Resource; editable: boolean; onDirty: (dirty: boolean) => void }) {
  const url = `${base}/${publication}/resources/${resource.id}`;
  const [revision, setRevision] = useState('');
  const [definition, setDefinition] = useState<HookDefinition | null>(null);
  const [files, setFiles] = useState<Record<string, string>>({});
  const [active, setActive] = useState(resource.kind === 'skill' ? 'SKILL.md' : '');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [invalidFields, setInvalidFields] = useState<string[]>([]);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const detail = await apiRequest<{ revision: string; version?: string; definition?: HookDefinition; files: string[] | Record<string, string> }>(url, { signal: controller.signal });
        const names = Array.isArray(detail.files) ? detail.files : Object.keys(detail.files);
        const entries = await Promise.all(names.map(async path => { const response = await fetch(`${url}/files?${new URLSearchParams({ path, revision: detail.revision, ...(detail.version ? { version: detail.version } : {}) })}`, { signal: controller.signal, credentials: 'same-origin' }); if (!response.ok) throw new Error('文件读取失败或资源已变化，请重新打开资源'); return [path, btoa(Array.from(new Uint8Array(await response.arrayBuffer()), byte => String.fromCharCode(byte)).join(''))] as const; }));
        if (!controller.signal.aborted) { setRevision(detail.revision); setDefinition(detail.definition || null); setFiles(Object.fromEntries(entries)); setActive(resource.kind === 'skill' ? 'SKILL.md' : names[0] || ''); }
      } catch (reason) { if (!controller.signal.aborted) setError(String(reason)); }
      finally { if (!controller.signal.aborted) setBusy(false); }
    })();
    return () => controller.abort();
  }, [url, resource.kind]);
  const changed = () => { onDirty(true); setNotice('未保存'); };
  let text = ''; let binary = false;
  try { text = files[active] ? decode(files[active]) : ''; } catch { binary = true; }
  let skill: ReturnType<typeof parseSkillDocument> | null = null;
  try { if (resource.kind === 'skill' && files['SKILL.md']) skill = parseSkillDocument(decode(files['SKILL.md'])); } catch { /* 源码编辑保留解析失败的原文。 */ }
  const skillField = (key: 'name' | 'description' | 'body', value: string) => { if (!skill) return; setFiles({ ...files, 'SKILL.md': encode(updateSkillDocument(decode(files['SKILL.md']), { name: skill.name, description: skill.description, body: skill.body, [key]: value })) }); changed(); };
  const patch = (value: Partial<HookDefinition>) => { if (definition) { setDefinition({ ...definition, ...value }); changed(); } };
  return <section className="resource-section public-resource-editor"><header className="resource-heading"><h3>{resource.name} · 公共附属资源</h3>{editable && <button className="studio-button resource-primary" disabled={busy || invalidFields.length > 0} onClick={async () => {
    setBusy(true); setError(''); try { const result = await apiJson<{ revision: string }>(`${base}/${publication}/${resource.kind === 'skill' ? 'skills' : 'hooks'}/${resource.id}`, 'PUT', { expected_revision: revision, files, ...(definition ? { definition } : {}) }); if (!mounted.current) return; setRevision(result.revision); onDirty(false); setNotice(resource.kind === 'skill' ? '已保存到公共资源，下次加载生效' : '已保存到公共资源，新 Run 生效'); } catch (reason) { if (mounted.current) setError(String(reason)); } finally { if (mounted.current) setBusy(false); }
  }}><Save size={15} />保存到公共资源</button>}</header>
    {error && <p role="alert" className="studio-notice tone-danger">{error}</p>}<p role="status">{notice}</p>
    <fieldset disabled={!editable || busy} className="resource-fields">
      {skill && <><label className="studio-field">名称<input value={skill.name} onChange={e => skillField('name', e.target.value)} /></label><label className="studio-field">用途<input value={skill.description} onChange={e => skillField('description', e.target.value)} /></label><label className="studio-field">技能内容<textarea rows={12} value={skill.body} onChange={e => skillField('body', e.target.value)} /></label></>}
      {definition && <><label className="studio-field">名称<input value={definition.name} onChange={e => patch({ name: e.target.value })} /></label><label className="studio-field">用途<input value={definition.description} onChange={e => patch({ description: e.target.value })} /></label><p>{hookKinds[definition.plugin_type]}</p><label className="studio-field">触发时机<select value={definition.hook_type} onChange={e => patch({ hook_type: e.target.value })}>{hookStages.filter(stage => definition.plugin_type !== 'prompt' || ['session.before', 'loop.before', 'llm.before'].includes(stage)).map(stage => <option key={stage} value={stage}>{hookStageLabels[hookStages.indexOf(stage)]}</option>)}</select></label>
        {definition.plugin_type === 'prompt' && <label className="studio-field">提示正文<textarea rows={10} value={definition.content} onChange={e => patch({ content: e.target.value })} /></label>}
        {definition.plugin_type === 'command' && <label className="studio-field">命令与参数（每行一个参数）<textarea rows={5} value={definition.argv.join('\n')} onChange={e => patch({ argv: e.target.value.split('\n') })} /></label>}
        {definition.plugin_type === 'http' && <label className="studio-field">服务地址<input value={definition.url} onChange={e => patch({ url: e.target.value })} /></label>}
        <details><summary>高级设置</summary><label className="studio-field">顺序<input type="number" value={definition.order} onChange={e => patch({ order: Number(e.target.value) })} /></label><label className="studio-field">超时（秒）<input type="number" value={definition.timeout_seconds} onChange={e => patch({ timeout_seconds: Number(e.target.value) })} /></label><label className="studio-field">异常策略<select value={definition.on_error} onChange={e => patch({ on_error: e.target.value })}><option value="fail_session">终止并报错</option><option value="continue">继续</option><option value="break_loop">停止本轮</option><option value="require_human">请求人工确认</option></select></label>
          <label className="studio-field">工具范围（每行一个工具名）<textarea value={definition.applies_to_tools.join('\n')} onChange={e => patch({ applies_to_tools: e.target.value.split('\n').filter(Boolean) })} /></label>
          {(['parameters', ...(definition.plugin_type === 'http' ? ['credential_headers'] : [])] as Array<'parameters' | 'credential_headers'>).map(key => <label className="studio-field" key={key}>{key === 'parameters' ? '参数定义（JSON）' : '认证字段映射（JSON，不填写凭证）'}<textarea rows={5} defaultValue={JSON.stringify(definition[key], null, 2)} onChange={e => {
            changed(); try { const value = JSON.parse(e.target.value); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(); patch({ [key]: value }); setInvalidFields(fields => fields.filter(field => field !== key)); } catch { setInvalidFields(fields => [...new Set([...fields, key])]); }
          }} />{invalidFields.includes(key) && <span role="alert">请输入有效的 JSON 对象</span>}</label>)}
        </details></>}
    </fieldset>
    <h4>文件</h4><div className="resource-tabs">{Object.keys(files).map(path => <button key={path} aria-pressed={active === path} onClick={() => setActive(path)}>{path}</button>)}</div>
    {active && <><div className="inline-actions"><a className="studio-icon-button" title="下载文件" href={`${url}/files?${new URLSearchParams({ path: active })}`} download><Download size={16} /></a>{editable && active !== 'SKILL.md' && <button className="studio-icon-button" title="移除文件" onClick={() => { setFiles(Object.fromEntries(Object.entries(files).filter(([key]) => key !== active))); setActive(''); changed(); }}><X size={16} /></button>}</div>{!binary && <textarea className="resource-content-editor" rows={12} aria-label="文件源码" readOnly={!editable || busy} value={text} onChange={e => { setFiles({ ...files, [active]: encode(e.target.value) }); changed(); }} />}</>}
    {editable && <label className="studio-button"><Upload size={15} />上传文件<input type="file" multiple disabled={busy} onChange={async e => { try { const next = { ...files }; for (const file of Array.from(e.target.files || [])) { if (file.size > 1024 * 1024) throw new Error('单文件最多 1 MiB'); next[file.name] = btoa(Array.from(new Uint8Array(await file.arrayBuffer()), b => String.fromCharCode(b)).join('')); } setFiles(next); changed(); } catch (reason) { setError(String(reason)); } }} /></label>}
  </section>;
}
