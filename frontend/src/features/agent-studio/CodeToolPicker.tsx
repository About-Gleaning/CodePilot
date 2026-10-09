import { useEffect, useState } from 'react';
import { apiRequest } from '../../api/client';
import { ConnectionEditor } from './ConnectionEditor';
import { useResourceSearch } from './resourceUi';

export type CodeToolSummary = { tool_id: string; name: string; call_name?: string; description: string; visibility: string; revision: string; version: string; owner_user_id?: string; archived?: boolean; withdrawn?: boolean; disabled?: boolean };

export function CodeToolPicker({ selected, disabled, connections, onChange, onConnections }: { selected: string[]; disabled: boolean; connections: string[] | null; onChange: (ids: string[]) => void; onConnections: (ids: string[] | null) => void }) {
  const [scope, setScope] = useState('private');
  const [query, setQuery] = useState('');
  const search = useResourceSearch(query);
  const [page, setPage] = useState(0);
  const [rows, setRows] = useState<CodeToolSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState('');
  const [credentialTool, setCredentialTool] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void apiRequest<{ tools: CodeToolSummary[]; total: number }>(`/api/tools?${new URLSearchParams({ scope, q: search, offset: String(page * 50) })}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setRows(value.tools || []); setTotal(value.total || 0); setError(''); } })
      .catch(() => { if (!controller.signal.aborted) setError('代码工具目录加载失败'); });
    return () => controller.abort();
  }, [scope, search, page]);
  return <section className="config-module">
    <header className="config-module-heading"><div><strong>代码工具</strong><span>选择自己编写或团队发布的 Python 工具；只读 Agent 无法执行</span></div><a href="#/tools">管理工具</a></header>
    <div className="resource-toolbar"><select aria-label="代码工具来源" value={scope} onChange={event => { setScope(event.target.value); setPage(0); }}><option value="private">我的工具</option><option value="public">公共工具</option></select><input aria-label="搜索代码工具" placeholder="搜索代码工具" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} /></div>
    {error && <p role="alert">{error}</p>}
    {rows.map(item => <label className="capability-item" key={item.tool_id}><input type="checkbox" checked={selected.includes(item.tool_id)} disabled={disabled || (!!(item.archived || item.withdrawn || item.disabled) && !selected.includes(item.tool_id))} onChange={() => onChange(selected.includes(item.tool_id) ? selected.filter(id => id !== item.tool_id) : [...selected, item.tool_id])} /><span><strong>{item.name}</strong><small>{item.description}{item.archived || item.withdrawn || item.disabled ? ' · 不可用' : ''}</small>{item.call_name && <small>调用名称：{item.call_name}</small>}</span></label>)}
    <div className="inline-actions"><button type="button" className="studio-button" disabled={!page} onClick={() => setPage(page - 1)}>上一页</button><span>{page + 1} 页 · {total} 个</span><button type="button" className="studio-button" disabled={(page + 1) * 50 >= total} onClick={() => setPage(page + 1)}>下一页</button></div>
    {selected.length > 0 && <div className="code-tool-selected"><strong>已选代码工具</strong>{selected.map(id => <div className="inline-actions" key={id}><span>{rows.find(item => item.tool_id === id)?.name || id}</span><button type="button" className="studio-button" disabled={disabled} onClick={() => onChange(selected.filter(value => value !== id))}>移除</button><button type="button" className="studio-button" onClick={() => setCredentialTool(credentialTool === id ? '' : id)}>配置凭证</button></div>)}</div>}
    {credentialTool && selected.includes(credentialTool) && <ConnectionEditor key={credentialTool} targetFilter={`tool:${credentialTool}`} selected={connections || []} disabled={disabled} onChange={onConnections} />}
  </section>;
}
