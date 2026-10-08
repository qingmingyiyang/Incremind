import { describe, expect, it } from 'vitest';
import { createConversationSnapshot, removeConversationCitation, conversationSnapshotMarkdown } from '@src/shared/lib/conversationSnapshot';

const input = () => ({ question: '原问题😀\r\n下一行', answer: '原回答[2]【3】和字面【99】\n\n末行',
  citations: [{ n: 2, title: '真实标题', quote: '片段一\r\n片段二', id: 'internal-source', locator: { start: 3 }, tokens: 400 },
    { n: 3, persona: true, title: '画像标题', quote: '画像正文' }],
  profile: '画像块', usage: { tokens: 99 }, turn: { id: 'internal-turn' }, unknown: '其他内部字段' });

describe('只读对话快照', () => {
  it('only captures the original question, answer and ordinary citation text without mutating its input', () => {
    const value = input(), before = structuredClone(value), snapshot = createConversationSnapshot(value);
    expect(snapshot).toEqual({ question: value.question, answer: '原回答和字面【99】\n\n末行', citations: [{ title: '真实标题', quote: '片段一\r\n片段二' }] });
    expect(value).toEqual(before);
    expect(Object.isFrozen(snapshot)).toBe(true); expect(Object.isFrozen(snapshot.citations)).toBe(true); expect(Object.isFrozen(snapshot.citations[0])).toBe(true);
    value.citations[0].quote = '后来改动'; expect(snapshot.citations[0].quote).toBe('片段一\r\n片段二');
    expect(JSON.stringify(snapshot)).not.toMatch(/画像|internal-|tokens|profile|unknown|locator/);
  });
  it('removes only a chosen citation from the immutable preview and exported Markdown', () => {
    const snapshot = createConversationSnapshot({ ...input(), citations: [...input().citations, { n: 4, title: '保留标题', quote: '保留片段' }] });
    const removed = removeConversationCitation(snapshot, 0), markdown = conversationSnapshotMarkdown(removed);
    expect(removed.citations).toEqual([{ title: '保留标题', quote: '保留片段' }]);
    expect(snapshot.citations).toHaveLength(2); expect(markdown).not.toContain('真实标题'); expect(markdown).not.toContain('片段一');
    expect(markdown).toContain(snapshot.question); expect(markdown).toContain(snapshot.answer); expect(markdown).toContain('保留片段');
  });
  it('keeps an unknown citation-like number and does not consume unqualified or invalid markers', () => {
    expect(createConversationSnapshot({ question: '', answer: '[2]【2】[3]【99】', citations: [{ n: '3', title: '编号损坏', quote: '正文' }, { n: 2, title: '题', quote: '文' }] }).answer).toBe('[3]【99】');
  });
  it('escapes citation titles and quotes every original line without truncating long text', () => {
    const quote = '第一行\r\n\r\n最后😀' + '字'.repeat(12000);
    const markdown = conversationSnapshotMarkdown(createConversationSnapshot({ question: '问题', answer: '回答', citations: [{ n: 1, title: '#题[目]', quote }] }));
    expect(markdown).toContain('\\#题\\[目\\]'); expect(markdown).toContain('> 第一行\r\n> \r\n> 最后😀' + '字'.repeat(12000));
  });
  it('rejects non-text fields and an invalid removal instead of serializing internal objects', () => {
    expect(() => createConversationSnapshot({ question: '问', answer: { profile: '泄漏' } })).toThrow();
    expect(() => createConversationSnapshot({ question: '问', answer: '答', citations: [{ title: '题', quote: { secret: '隐藏' } }] })).toThrow();
    expect(() => removeConversationCitation(createConversationSnapshot({ question: '问', answer: '答' }), 0)).toThrow();
  });
  it('excludes only the standalone system reading marker and preserves every other original Markdown byte', () => {
    const marker = '<!-- source_sections.comments: capture-qualified -->';
    const body = `# 回答😀\r\n\r\n${marker}\r\n\r\n正文\r\n\r\n\`\`\`md\r\n${marker}\r\n\`\`\`\r\n\r\n行内\`${marker}\`\r\n\r\n> ${marker}\r\n\r\n<!-- 用户注释 -->\r\n`;
    const expected = body.replace(marker, ''), snapshot = createConversationSnapshot({ question: marker, answer: body });
    expect(snapshot.question).toBe(marker); expect(snapshot.answer).toBe(expected); expect(body).toContain(`\r\n\r\n${marker}\r\n\r\n`);
    expect(conversationSnapshotMarkdown(snapshot)).toContain(expected);
  });
});
