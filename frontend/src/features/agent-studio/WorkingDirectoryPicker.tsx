import { useEffect, useState } from 'react';
import { apiRequest } from '../../api/client';

export function WorkingDirectoryPicker({ value, disabled, onChange }: { value: string | null; disabled: boolean; onChange: (path: string | null) => void }) {
  const [options, setOptions] = useState<string[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    void apiRequest<{ roots: string[]; directories: string[]; selected: string }>(`/api/workspace/directories${value ? `?path=${encodeURIComponent(value)}` : ''}`, { signal: controller.signal }).then((result) => {
      if (!controller.signal.aborted) setOptions([...new Set([...result.roots, result.selected, ...result.directories])]);
    }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '目录读取失败'); });
    return () => controller.abort();
  }, [value]);
  return <section className="config-module"><header className="config-module-heading"><div><strong>工作目录</strong><span>仅影响新建 Session</span></div></header>
    {error ? <div className="studio-notice tone-danger" role="alert">{error}</div> : null}
    <label className="studio-field"><span>新会话默认目录</span><select value={value || ''} disabled={disabled} onChange={(event) => onChange(event.target.value || null)}>
      <option value="">当前工作区</option>
      {value && !options.includes(value) ? <option value={value}>{value} · 待配置</option> : null}
      {options.map((path) => <option value={path} key={path}>{path}</option>)}
    </select></label>
  </section>;
}
