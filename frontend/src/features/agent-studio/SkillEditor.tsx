import { useEffect, useState } from 'react';
import { apiRequest } from '../../api/client';
import { AgentCapabilityPicker } from './AgentCapabilityPicker';

export type SkillRecord = { skill_id: string; revision?: string; name: string; description: string; visibility: string; archived: boolean; files?: string[] };
type Props = { selected: string[] | null; disabled: boolean; onChange: (ids: string[] | null) => void };

// Agent 中只负责选择，资源编辑由独立管理页维护。
export function SkillEditor({ selected, disabled, onChange }: Props) {
  const [records, setRecords] = useState<SkillRecord[]>([]);
  const [error, setError] = useState('');
  const [offset, setOffset] = useState(0);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let controller: AbortController;
    const refresh = () => {
      controller?.abort(); controller = new AbortController();
      const signal = controller.signal;
      void apiRequest<{ skills: SkillRecord[]; shared: SkillRecord[] }>('/api/skills', { signal }).then(value => {
        if (signal.aborted) return;
        setRecords([...value.shared, ...value.skills]); setOffset(50); setMore(value.skills.length === 50 || value.shared.length === 50);
      }).catch(reason => { if (!signal.aborted) setError(String(reason)); });
    };
    refresh(); window.addEventListener('resources-changed', refresh);
    return () => { controller.abort(); window.removeEventListener('resources-changed', refresh); };
  }, []);
  const missing = (selected || []).filter(id => !records.some(record => record.skill_id === id));
  const items = [
    ...records.map(record => ({
      id: record.skill_id,
      title: record.name,
      description: record.description,
      group: record.visibility === 'shared' ? '共享' : '个人',
      status: record.archived ? '已归档' : undefined,
      selected: selected === null ? record.visibility === 'shared' : selected.includes(record.skill_id),
      disabled: selected === null || record.archived,
      attention: record.archived,
    })),
    ...missing.map(id => ({ id, title: id, status: more ? '未加载' : '待配置', selected: true, attention: true })),
  ];
  return <section className="config-module" id="agent-skills">
    <header className="config-module-heading"><div><strong>技能</strong><span>按需加载的专业指令</span></div><a href="#/skills">管理技能</a></header>
    {error && <p role="alert" className="studio-notice tone-danger">{error}</p>}
    <label className="config-inherit-toggle"><input type="checkbox" disabled={disabled} checked={selected === null} onChange={event => onChange(event.target.checked ? null : [])} /><span><strong>沿用共享技能</strong><small>自动使用当前可用的共享技能目录</small></span></label>
    <AgentCapabilityPicker title="技能" items={items} disabled={disabled} emptyText="没有符合条件的技能。" onToggle={(id) => onChange(selected?.includes(id) ? selected.filter(value => value !== id) : [...(selected || []), id])} />
    {more && <button type="button" className="studio-button config-load-more" disabled={busy} onClick={async () => { setBusy(true); try { const value = await apiRequest<{ skills: SkillRecord[]; shared: SkillRecord[] }>(`/api/skills?offset=${offset}`); setRecords(current => [...current, ...[...value.shared, ...value.skills].filter(item => !current.some(existing => existing.skill_id === item.skill_id))]); setOffset(offset + 50); setMore(value.skills.length === 50 || value.shared.length === 50); } catch (reason) { setError(String(reason)); } finally { setBusy(false); } }}>加载更多技能</button>}
  </section>;
}
