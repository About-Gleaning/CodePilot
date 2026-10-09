import { describe, expect, it } from 'vitest';
import { parseSkillDocument, updateSkillDocument } from './skillDocument';

describe('技能表单与源码互转', () => {
  it('保留未知字段、嵌套值与注释，仅替换表单字段', () => {
    const source = '---\n# 原注释\nname: old\ndescription: old\nmetadata:\n  tags: [one, two]\n---\n正文\n';
    const next = updateSkillDocument(source, { name: 'new', description: '包含: 冒号', body: '# 新内容\n' });
    const parsed = parseSkillDocument(next);
    expect(parsed.name).toBe('new');
    expect(parsed.description).toBe('包含: 冒号');
    expect(parsed.document.toJS().metadata.tags).toEqual(['one', 'two']);
    expect(next).toContain('# 原注释');
    expect(parsed.body).toBe('# 新内容\n');
  });
  it.each(['正文', '---\nname: x\nname: y\ndescription: d\n---\n', '---\nname: [a]\ndescription: d\n---\n'])('拒绝无法安全往返的头部 %s', source => {
    expect(() => parseSkillDocument(source)).toThrow();
  });
});
