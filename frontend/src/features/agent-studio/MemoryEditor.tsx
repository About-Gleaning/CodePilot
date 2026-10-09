import { useEffect, useState } from 'react';
import { Save, Trash2 } from 'lucide-react';
import { apiJson, apiRequest } from '../../api/client';

type MemorySnapshot = { content: string; revision: string };

export function MemoryEditor({ agentId }: { agentId: string }) {
  const [scope, setScope] = useState<'agent' | 'global'>('agent');
  const [snapshot, setSnapshot] = useState<MemorySnapshot | null>(null);
  const [content, setContent] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const memoryId = scope === 'global' ? '_global' : agentId;
  const url = `/api/memories/${encodeURIComponent(memoryId)}`;

  useEffect(() => {
    const controller = new AbortController();
    setSnapshot(null);
    setContent('');
    setError('');
    void apiRequest<MemorySnapshot>(url, { signal: controller.signal }).then((value) => {
      if (controller.signal.aborted) return;
      setSnapshot(value);
      setContent(value.content);
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '记忆读取失败');
    });
    return () => controller.abort();
  }, [url]);

  const save = async (nextContent: string) => {
    if (!snapshot || saving) return;
    setSaving(true);
    setError('');
    try {
      const result = await apiJson<MemorySnapshot>(url, 'PUT', { content: nextContent, expected_revision: snapshot.revision });
      setSnapshot(result);
      setContent(result.content);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '记忆保存失败');
    } finally {
      setSaving(false);
    }
  };

  return <section className="config-module config-independent-module">
    <header className="config-module-heading"><div><strong>记忆正文</strong><span>独立保存，不会创建 Agent revision</span></div><em>独立版本</em></header>
    <label className="studio-field"><span>记忆范围</span>
      <select value={scope} disabled={saving} onChange={(event) => {
        if (snapshot && content !== snapshot.content && !window.confirm('放弃未保存的记忆修改？')) return;
        setScope(event.target.value as 'agent' | 'global');
      }}><option value="agent">当前 Agent</option><option value="global">个人全局记忆</option></select>
    </label>
    {error ? <div role="alert" className="studio-notice tone-danger">{error}</div> : null}
    <label className="studio-field"><span>记忆正文</span>
      <textarea rows={6} maxLength={100000} disabled={!snapshot || saving} value={content} onChange={(event) => setContent(event.target.value)} />
    </label>
    <div className="inline-actions config-independent-actions">
      <button type="button" className="studio-button" disabled={!snapshot || saving || content === snapshot.content} onClick={() => void save(content)}><Save size={15} />保存记忆</button>
      <button type="button" className="studio-icon-button" title="清除记忆" aria-label="清除记忆" disabled={!snapshot || saving || !snapshot.content} onClick={() => {
        if (window.confirm('清除所选范围的全部记忆？')) void save('');
      }}><Trash2 size={15} /></button>
    </div>
  </section>;
}
