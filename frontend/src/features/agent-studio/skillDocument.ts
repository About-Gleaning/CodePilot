import { isMap, parseDocument } from 'yaml';

// 保留 YAML 文档节点，表单只修改自己负责的字段，避免丢失扩展元数据。
export function parseSkillDocument(source: string) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)([\s\S]*)$/.exec(source);
  if (!match) throw new Error('技能文件缺少有效的 YAML 头部，请在源码中修复。');
  const document = parseDocument(match[1]);
  if (document.errors.length || !isMap(document.contents)) throw new Error('技能元数据无法解析，请在源码中修复后保存。');
  const name = document.get('name'), description = document.get('description');
  if (typeof name !== 'string' || typeof description !== 'string') throw new Error('技能名称和用途必须是文本。');
  return { document, name, description, body: match[2] };
}

export function updateSkillDocument(source: string, values: { name: string; description: string; body: string }) {
  const { document } = parseSkillDocument(source);
  document.set('name', values.name);
  document.set('description', values.description);
  return `---\n${document.toString()}---\n${values.body}`;
}
