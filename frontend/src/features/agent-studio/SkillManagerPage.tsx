import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Archive, Copy, Download, FilePlus, MoreHorizontal, Plus, Save, X } from 'lucide-react';
import { apiJson, apiRequest } from '../../api/client';
import { MarkdownContent } from '../../components/MessageContent';
import { useResourceSearch, type ResourceAgent, type ResourcePageProps } from './resourceUi';
import { parseSkillDocument, updateSkillDocument } from './skillDocument';
import type { SkillRecord } from './SkillEditor';

const encode = (value: string) => btoa(Array.from(new TextEncoder().encode(value), byte => String.fromCharCode(byte)).join(''));
const decode = (value: string) => new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(atob(value), char => char.charCodeAt(0)));
const initialSource = '---\nname: ""\ndescription: ""\n---\n';

export function SkillManagerPage({ onReturn, active = true }: ResourcePageProps) {
  const [records, setRecords] = useState<SkillRecord[]>([]);
  const [query, setQuery] = useState('');
  const search = useResourceSearch(query);
  const [status, setStatus] = useState('active');
  const [page, setPage] = useState(0);
  const [more, setMore] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [record, setRecord] = useState<SkillRecord | null>(null);
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState(initialSource);
  const [files, setFiles] = useState<Record<string, string>>({});
  const [references, setReferences] = useState<ResourceAgent[]>([]);
  const [tab, setTab] = useState('content');
  const [preview, setPreview] = useState(false);
  const [raw, setRaw] = useState(false);
  const [prefix, setPrefix] = useState('');
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const readonly = record?.visibility === 'shared' || !!record?.archived;
  let parsed: ReturnType<typeof parseSkillDocument> | undefined;
  let parseError = '';
  try { parsed = parseSkillDocument(source); } catch (reason) { parseError = String((reason as Error).message); }
  const discard = () => !dirty || window.confirm('技能有未保存修改，放弃这些修改？');
  const changed = () => { setDirty(true); setNotice(''); };
  useEffect(() => {
    if (!active || tab !== 'agents' || !record) return;
    const controller = new AbortController();
    void apiRequest<{ agents: ResourceAgent[] }>(`/api/skills/${record.skill_id}/references`, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setReferences(value.agents); }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [active, tab, record?.skill_id]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); if (dirty) event.returnValue = ''; };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);
  useEffect(() => {
    const controller = new AbortController();
    void apiRequest<{ skills: SkillRecord[]; shared: SkillRecord[] }>(`/api/skills?${new URLSearchParams({ q: search, status, offset: String(page * 50) })}`, { signal: controller.signal }).then(response => {
      if (controller.signal.aborted) return;
      setRecords([...response.shared, ...response.skills]); setMore(response.skills.length === 50 || response.shared.length === 50);
    }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [search, status, page, refresh]);

  const load = async (item: SkillRecord) => {
    if (!discard()) return;
    const token = ++generation.current; setBusy(true); setError(''); setNotice('');
    try {
      const detail = await apiRequest<SkillRecord>(`/api/skills/${item.skill_id}`);
      const entries = await Promise.all((detail.files || []).map(async path => {
        const response = await fetch(`/api/skills/${item.skill_id}/files?${new URLSearchParams({ revision: detail.revision || '', path })}`, { credentials: 'same-origin' });
        if (!response.ok) throw new Error('技能文件读取失败');
        return [path, btoa(Array.from(new Uint8Array(await response.arrayBuffer()), byte => String.fromCharCode(byte)).join(''))] as const;
      }));
      const refs = await apiRequest<{ agents: ResourceAgent[] }>(`/api/skills/${item.skill_id}/references`);
      if (token !== generation.current) return;
      const loaded = Object.fromEntries(entries);
      setSource(decode(loaded['SKILL.md'])); setFiles(loaded); setReferences(refs.agents); setRecord(detail); setOpen(true); setDirty(false); setRaw(false); setTab('content');
    } catch (reason) { if (token === generation.current) setError(String(reason)); }
    finally { if (token === generation.current) setBusy(false); }
  };
  const save = async () => {
    if (!parsed || !parsed.name.trim() || !parsed.description.trim()) { setError(parseError || '请填写技能名称和用途。'); return; }
    setBusy(true); setError('');
    try {
      const payload = { ...files, 'SKILL.md': encode(source) };
      const saved = await apiJson<SkillRecord>(record ? `/api/skills/${record.skill_id}` : '/api/skills', record ? 'PUT' : 'POST', { expected_revision: record?.revision, files: payload });
      setRecord({ ...saved, files: Object.keys(payload) }); setFiles(payload); setDirty(false); setNotice('已保存'); setRefresh(value => value + 1);
      window.dispatchEvent(new Event('resources-changed'));
    } catch (reason) { setError(String((reason as Error).message)); } finally { setBusy(false); }
  };
  const field = (key: 'name' | 'description' | 'body', value: string) => {
    if (!parsed) return;
    setSource(updateSkillDocument(source, { name: parsed.name, description: parsed.description, body: parsed.body, [key]: value })); changed();
  };
  return <section className="resource-manager redesigned-resource">
    <header className="resource-heading"><div><span className="resource-eyebrow">技能库</span><h1>{open ? record?.name || '新建技能' : '技能'}</h1></div><div className="inline-actions">
      {onReturn && <button className="studio-button" onClick={onReturn}><ArrowLeft size={15} />返回 Agent 配置</button>}
      {!open && <button className="studio-button resource-primary" disabled={busy} onClick={() => { setOpen(true); setRecord(null); setSource(initialSource); setFiles({}); setReferences([]); setDirty(false); setError(''); setNotice(''); setRaw(false); setPreview(false); setTab('content'); }}><Plus size={16} />新建技能</button>}
      {open && <button className="studio-button" disabled={busy} onClick={() => { if (discard()) { setOpen(false); setDirty(false); } }}><ArrowLeft size={15} />技能列表</button>}
    </div></header>
    {error && <p role="alert" className="studio-notice tone-danger">{error}</p>}
    {!open ? <>
      <div className="resource-toolbar"><input aria-label="搜索技能" placeholder="搜索名称或用途" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} /><select aria-label="技能状态" value={status} onChange={event => { setStatus(event.target.value); setPage(0); }}><option value="active">使用中</option><option value="archived">已归档</option><option value="all">全部状态</option></select></div>
      <div className="resource-list">{records.map(item => <button className="resource-row" key={item.skill_id} disabled={busy} onClick={() => void load(item)}><div><strong>{item.name}</strong><p>{item.description}</p></div><span>{item.visibility === 'shared' ? '共享 · 只读' : '个人'} · {item.archived ? '已归档' : '使用中'}</span></button>)}{!records.length && <div className="resource-empty">暂无技能</div>}</div>
      <div className="resource-pagination"><button className="studio-button" disabled={!page || busy} onClick={() => setPage(value => value - 1)}>上一页</button><span>第 {page + 1} 页</span><button className="studio-button" disabled={!more || busy} onClick={() => setPage(value => value + 1)}>下一页</button></div>
    </> : <>
      <div className="resource-editor-bar"><span role="status">{dirty ? '未保存' : notice || (readonly ? '只读' : record ? '已保存' : '新建')}</span><div className="inline-actions">{!readonly && <button className="studio-button resource-primary" disabled={busy || !!parseError} onClick={() => void save()}><Save size={15} />保存技能</button>}{record && <details className="resource-menu"><summary aria-label="技能更多操作"><MoreHorizontal size={18} /></summary><div><button disabled={busy} onClick={() => { setRecord(null); setReferences([]); changed(); }}><Copy size={15} />复制为我的技能</button>{!readonly && <button disabled={busy} onClick={async () => {
        if (!window.confirm(`归档技能？${references.length} 个 Agent 的新执行可能受影响。`)) return;
        setBusy(true); try { await apiJson(`/api/skills/${record.skill_id}/archive`, 'POST', { expected_revision: record.revision }); setOpen(false); setDirty(false); setRefresh(value => value + 1); window.dispatchEvent(new Event('resources-changed')); } catch (reason) { setError(String(reason)); } finally { setBusy(false); }
      }}><Archive size={15} />归档</button>}</div></details>}</div></div>
      <nav className="resource-tabs" aria-label="技能详情">{[['content', '技能内容'], ['files', '附带文件'], ['agents', '引用情况']].map(([id, label]) => <button key={id} aria-current={tab === id ? 'page' : undefined} onClick={() => setTab(id)}>{label}</button>)}</nav>
      {tab === 'content' && <>
        <section className="resource-section"><h3>基本信息</h3><fieldset disabled={busy || readonly || !parsed} className="resource-fields"><label className="studio-field">名称<input value={parsed?.name || ''} onChange={event => field('name', event.target.value)} /></label><label className="studio-field">用途<input value={parsed?.description || ''} onChange={event => field('description', event.target.value)} /></label></fieldset></section>
        {parseError && <p role="alert" className="studio-notice tone-danger">{parseError}</p>}
        <section className="resource-section"><div className="resource-heading"><h3>技能内容</h3><div className="resource-tabs"><button aria-pressed={!preview} onClick={() => setPreview(false)}>编辑</button><button aria-pressed={preview} onClick={() => setPreview(true)}>预览</button></div></div>{preview ? <MarkdownContent className="resource-preview" text={parsed?.body || ''} /> : <textarea className="resource-content-editor" aria-label="技能正文" rows={16} disabled={readonly || busy || !parsed} value={parsed?.body || ''} onChange={event => field('body', event.target.value)} />}</section>
        <details className="resource-advanced" open={raw || !!parseError} onToggle={event => setRaw(event.currentTarget.open)}><summary>高级：SKILL.md 源码</summary><textarea className="resource-content-editor" aria-label="SKILL.md 源码" rows={16} value={source} disabled={readonly || busy} onChange={event => { setSource(event.target.value); changed(); }} /></details>
      </>}
      {tab === 'files' && <section className="resource-section"><h3>附带文件</h3>{Object.keys(files).filter(path => path !== 'SKILL.md').map(path => <div className="resource-association" key={path}><span>{path}</span><div className="inline-actions">{record?.files?.includes(path) && <a title={`下载 ${path}`} className="studio-icon-button" href={`/api/skills/${record.skill_id}/files?${new URLSearchParams({ revision: record.revision || '', path })}`} download><Download size={16} /></a>}<button className="studio-icon-button" title={`移除 ${path}`} disabled={readonly || busy} onClick={() => { setFiles(current => Object.fromEntries(Object.entries(current).filter(([key]) => key !== path))); changed(); }}><X size={16} /></button></div></div>)}
        {!readonly && <><label className="studio-button resource-upload"><FilePlus size={15} />上传文件<input aria-label="上传技能文件" type="file" multiple disabled={busy} onChange={async event => {
          const uploaded = Array.from(event.target.files || []); setBusy(true); setError('');
          try {
            const additions: Record<string, string> = {};
            for (const file of uploaded) { if (file.size > 1024 * 1024 || file.name === 'SKILL.md') throw new Error('附件最多 1 MiB，SKILL.md 请在内容页编辑。'); const bytes = new Uint8Array(await file.arrayBuffer()); additions[prefix ? `${prefix}/${file.name}` : file.name] = btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join('')); }
            const next = { ...files, ...additions, 'SKILL.md': encode(source) };
            if (Object.keys(next).length > 100 || Object.values(next).reduce((sum, item) => sum + atob(item).length, 0) > 4 * 1024 * 1024) throw new Error('技能最多 100 个文件、4 MiB。');
            setFiles(next); changed();
          } catch (reason) { setError(String(reason)); } finally { setBusy(false); }
        }} /></label><details className="resource-advanced"><summary>上传目录（可选）</summary><label className="studio-field">相对目录<input value={prefix} onChange={event => setPrefix(event.target.value)} /></label></details></>}
      </section>}
      {tab === 'agents' && <section className="resource-section">{references.length ? references.map(agent => <div className="resource-association" key={agent.agent_id}><strong>{agent.name}</strong></div>) : <p className="resource-muted">暂无 Agent 引用</p>}</section>}
    </>}
  </section>;
}
