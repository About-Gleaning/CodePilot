import { AlertTriangle, Search } from 'lucide-react';
import { useMemo, useState } from 'react';

export type CapabilityPickerItem = {
  id: string;
  title: string;
  description?: string;
  group?: string;
  status?: string;
  selected: boolean;
  disabled?: boolean;
  attention?: boolean;
  approval?: boolean;
};

type Filter = 'all' | 'selected' | 'attention';

export function AgentCapabilityPicker({
  title,
  items,
  disabled,
  emptyText,
  onToggle,
}: {
  title: string;
  items: CapabilityPickerItem[];
  disabled: boolean;
  emptyText: string;
  onToggle: (id: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const visible = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return [...items]
      .filter((item) => {
        if (filter === 'selected' && !item.selected) return false;
        if (filter === 'attention' && !item.attention) return false;
        return !keyword || `${item.title} ${item.description || ''} ${item.group || ''}`.toLowerCase().includes(keyword);
      })
      .sort((left, right) => Number(right.selected) - Number(left.selected) || left.title.localeCompare(right.title));
  }, [filter, items, query]);
  const selectedCount = items.filter((item) => item.selected).length;
  const attentionCount = items.filter((item) => item.attention).length;

  return (
    <section className="config-picker" aria-label={title}>
      <header className="config-picker-heading">
        <div><strong>{title}</strong><span>{selectedCount} 项已选</span></div>
        <label className="config-picker-search">
          <Search size={14} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`搜索${title}`} />
        </label>
      </header>
      <div className="config-picker-filters" role="group" aria-label={`${title}筛选`}>
        <button type="button" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>全部 <span>{items.length}</span></button>
        <button type="button" aria-pressed={filter === 'selected'} onClick={() => setFilter('selected')}>已选 <span>{selectedCount}</span></button>
        <button type="button" aria-pressed={filter === 'attention'} onClick={() => setFilter('attention')}>需注意 <span>{attentionCount}</span></button>
      </div>
      <div className="config-picker-list">
        {visible.map((item) => (
          <label className={`config-picker-item ${item.selected ? 'is-selected' : ''} ${item.disabled ? 'is-disabled' : ''}`} key={item.id}>
            <input type="checkbox" checked={item.selected} disabled={disabled || item.disabled} onChange={() => onToggle(item.id)} />
            <span className="config-picker-copy">
              <strong>{item.title}</strong>
              {item.description ? <small>{item.description}</small> : null}
            </span>
            <span className="config-picker-meta">
              {item.group ? <em>{item.group}</em> : null}
              {item.status ? <em>{item.status}</em> : null}
              {item.approval ? <em className="tone-warning">审批</em> : null}
              {item.attention ? <AlertTriangle size={13} aria-label="需要注意" /> : null}
            </span>
          </label>
        ))}
        {!visible.length ? <p className="config-picker-empty">{emptyText}</p> : null}
      </div>
    </section>
  );
}
