import { useEffect, useState } from 'react';
import { Check, Plus, Save, Trash2, X } from 'lucide-react';
import { apiJson, apiRequest } from '../../api/client';

type Entry = { target: string; fields: string[]; kind: string; personal?: boolean };
type Connection = { connection_id: string; target: string; revision: string; revoked: boolean };

export function ConnectionEditor({ selected, disabled, onChange, targetFilter, publicationId }: { selected: string[] | null; disabled: boolean; onChange: (ids: string[] | null) => void; targetFilter?: string; publicationId?: string }) {
  const params = new URLSearchParams();
  if (publicationId) params.set('publication', publicationId);
  if (targetFilter?.startsWith('tool:')) params.set('tool', targetFilter.slice(5));
  const catalogUrl = `/api/connections${params.size ? `?${params}` : ''}`;
  const [catalog, setCatalog] = useState<Entry[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [editing, setEditing] = useState<Connection | null>(null);
  const [target, setTarget] = useState('');
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [valid, setValid] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [more, setMore] = useState(false);
  const refresh = async () => {
    const value = await apiRequest<{ catalog: Entry[]; connections: Connection[] }>(catalogUrl);
    setCatalog(value.catalog); setConnections(value.connections);
    setOffset(value.connections.length); setMore(value.connections.length === 50);
  };
  useEffect(() => {
    const controller = new AbortController();
    void apiRequest<{ catalog: Entry[]; connections: Connection[] }>(catalogUrl, { signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) {
        setCatalog(value.catalog); setConnections(value.connections);
        setOffset(value.connections.length); setMore(value.connections.length === 50);
      }
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [catalogUrl]);

  const action = async (callback: () => Promise<void>) => {
    setBusy(true); setError(''); setValid(null);
    try { await callback(); } catch (reason) { setError(reason instanceof Error ? reason.message : '连接操作失败'); }
    finally { setBusy(false); }
  };
  const choose = (target: string, id: string) => {
    const sameTarget = new Set(connections.filter((item) => item.target === target).map((item) => item.connection_id));
    sameTarget.add(`team:${target}`);
    onChange([...(selected || []).filter((item) => !sameTarget.has(item)), ...(id ? [id] : [])]);
  };

  return <section className="config-module config-connection-module">
    <header className="config-module-heading"><div><strong>身份分配</strong><span>选择结果随 Agent revision 保存</span></div><button type="button" className="studio-icon-button" title="新增个人连接" aria-label="新增个人连接" disabled={disabled || busy} onClick={() => {
      setEditing(null); setTarget(targetFilter || catalog[0]?.target || ''); setCredentials({}); setOpen(true);
    }}><Plus size={15} /></button></header>
    {error ? <div className="studio-notice tone-danger" role="alert">{error}</div> : null}
    {!targetFilter && !publicationId && <label className="capability-item"><input type="checkbox" checked={selected === null} disabled={disabled} onChange={(event) => onChange(event.target.checked ? null : [])} /><span>沿用团队连接</span></label>}
    {catalog.filter(entry => !targetFilter || entry.target === targetFilter).map((entry) => <label className="studio-field" key={entry.target}><span>{entry.target}</span>
      <select disabled={disabled || selected === null} value={(selected || []).find((id) => id === `team:${entry.target}` || connections.some((item) => item.connection_id === id && item.target === entry.target)) || ''} onChange={(event) => choose(entry.target, event.target.value)}>
        <option value="">未选择</option>{!entry.personal && <option value={`team:${entry.target}`}>团队身份</option>}
        {connections.filter((item) => item.target === entry.target).map((item) => <option key={item.connection_id} value={item.connection_id} disabled={item.revoked}>个人身份 {item.connection_id.slice(0, 8)}{item.revoked ? ' · 已撤销' : ''}</option>)}
      </select>
    </label>)}
    {connections.length ? <div className="config-vault-heading"><strong>个人凭证库</strong><span>验证、更换和撤销会立即独立保存</span></div> : null}
    {connections.filter(entry => !targetFilter || entry.target === targetFilter).map((connection) => <div className="inline-actions config-connection-row" key={connection.connection_id}>
      <span>{connection.target} · {connection.connection_id.slice(0, 8)}{valid === connection.connection_id ? ' · 验证通过' : connection.revoked ? ' · 已撤销' : ''}</span>
      <button type="button" className="studio-icon-button" title="验证连接" aria-label="验证连接" disabled={disabled || busy || connection.revoked} onClick={() => void action(async () => {
        await apiJson(`/api/connections/${connection.connection_id}/validate`, 'POST'); setValid(connection.connection_id);
      })}><Check size={15} /></button>
      <button type="button" className="studio-button" disabled={disabled || busy} onClick={() => { setEditing(connection); setTarget(connection.target); setCredentials({}); setOpen(true); }}>更换凭证</button>
      <button type="button" className="studio-icon-button" title="撤销连接" aria-label="撤销连接" disabled={disabled || busy || connection.revoked} onClick={() => {
        if (window.confirm('撤销该连接？后续调用将被阻止。')) void action(async () => { await apiJson(`/api/connections/${connection.connection_id}/revoke`, 'POST', { expected_revision: connection.revision }); await refresh(); });
      }}><Trash2 size={15} /></button>
    </div>)}
    {more ? <button type="button" className="studio-button" disabled={busy} onClick={() => void action(async () => {
      const result = await apiRequest<{ connections: Connection[] }>(`/api/connections?offset=${offset}`);
      setConnections((current) => [...current, ...result.connections.filter((item) => !current.some((existing) => existing.connection_id === item.connection_id))]);
      setOffset(offset + result.connections.length); setMore(result.connections.length === 50);
    })}>加载更多连接</button> : null}
    {(selected || []).filter((id) => !id.startsWith('team:') && !connections.some((item) => item.connection_id === id)).map((id) => <label className="capability-item" key={id}><input type="checkbox" checked disabled={disabled} onChange={() => onChange(selected!.filter((item) => item !== id))} /><span>{id} · 待配置</span></label>)}
    {open ? <div className="config-credential-editor"><header><strong>{editing ? '更换凭证' : '新增个人连接'}</strong><span>敏感字段不会写入 Agent 表单或浏览器存储</span></header><div className="config-field-grid">
      <label className="studio-field"><span>服务</span><select value={target} disabled={busy || !!editing || !!targetFilter} onChange={(event) => { setTarget(event.target.value); setCredentials({}); }}><option value="">选择服务</option>{catalog.map((item) => <option value={item.target} key={item.target}>{item.target}</option>)}</select></label>
      {(catalog.find((item) => item.target === target)?.fields || []).map((field) => <label className="studio-field" key={field}><span>{field}</span><input type="password" autoComplete="new-password" maxLength={8192} value={credentials[field] || ''} disabled={busy} onChange={(event) => setCredentials((current) => ({ ...current, [field]: event.target.value }))} /></label>)}
      <div className="inline-actions">
        <button type="button" className="studio-button" disabled={busy || !target} onClick={() => void action(async () => {
          await apiJson(editing ? `/api/connections/${editing.connection_id}` : '/api/connections', editing ? 'PUT' : 'POST', { target, credentials, expected_revision: editing?.revision });
          setCredentials({}); setOpen(false); await refresh();
        })}><Save size={15} />保存连接</button>
        <button type="button" className="studio-icon-button" disabled={busy} title="关闭凭证编辑" onClick={() => { setCredentials({}); setOpen(false); }}><X size={15} /></button>
      </div>
    </div></div> : null}
  </section>;
}
