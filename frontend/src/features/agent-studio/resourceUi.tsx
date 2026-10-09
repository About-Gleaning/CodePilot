import { useEffect, useState } from 'react';

export const hookStages = ['session.before', 'loop.before', 'llm.before', 'llm.after', 'tool.before', 'tool.after', 'loop.after', 'session.after'];
export const hookStageLabels = ['本次执行开始', '本轮开始', '模型请求前', '模型响应后', '工具执行前', '工具执行后', '本轮结束', '本次执行结束'];
export const hookKinds: Record<string, string> = { prompt: '补充提示', command: '运行脚本', http: '调用 HTTP', agent: 'Agent 插件（尚不支持）' };
export type ResourceAgent = { agent_id: string; name: string };
export type ResourcePageProps = { onReturn?: () => void; active?: boolean };

export function useResourceSearch(value: string) {
  const [query, setQuery] = useState(value);
  useEffect(() => { const timer = window.setTimeout(() => setQuery(value), 250); return () => clearTimeout(timer); }, [value]);
  return query;
}
