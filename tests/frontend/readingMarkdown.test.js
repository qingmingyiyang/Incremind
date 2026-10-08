import { describe, expect, it } from 'vitest';
import { hideReadingMarkers, isHiddenReadingNode, withoutHiddenReadingMarkers } from '@src/shared/lib/readingMarkdown';

const marker = '<!-- source_sections.comments: capture-qualified -->';
describe('阅读及分享共用的系统标记规则', () => {
  it.each(['\n', '\r\n', '\r'])('removes only root standalone marker ranges with original UTF16 offsets and line endings (%j)', newline => {
    const body = ['😀前文', '', marker, '', '后文😀', '', marker, '', '尾行', ''].join(newline);
    expect(withoutHiddenReadingMarkers(body)).toBe(body.replaceAll(marker, '')); expect(body.split(marker)).toHaveLength(3);
  });
  it('retains code blocks, inline code, inline HTML, nested quotes and ordinary user comments byte for byte', () => {
    const body = `# 标题\r\n\r\n~~~md\r\n${marker}\r\n~~~\r\n\r\n\`${marker}\`\r\n\r\n前文 ${marker} 后文\r\n\r\n> ${marker}\r\n\r\n<!-- 用户注释 -->\r\n`;
    expect(withoutHiddenReadingMarkers(body)).toBe(body);
  });
  it('retains an HTML block containing other content and rejects a non-text payload', () => {
    const block = `<div>\n${marker}\n<!-- 用户注释 -->\n</div>`;
    expect(withoutHiddenReadingMarkers(block)).toBe(block);
    expect(withoutHiddenReadingMarkers('<!-- source_sections.comments: capture-qualified extra -->')).toBe('<!-- source_sections.comments: capture-qualified extra -->');
    expect(() => withoutHiddenReadingMarkers({ internal: '字段' })).toThrow();
  });
  it('removes only the qualified root node when two complete comments are adjacent', () => {
    expect(withoutHiddenReadingMarkers(`${marker}\n<!-- 用户注释 -->`)).toBe('\n<!-- 用户注释 -->');
  });
  it('applies exactly the same root-only predicate to the original reader plugin', () => {
    const hidden = { type: 'html', value: `  ${marker}  ` }, user = { type: 'html', value: '<!-- 用户注释 -->' }, nested = { type: 'blockquote', children: [{ type: 'html', value: marker }] };
    expect(isHiddenReadingNode(hidden)).toBe(true); expect(isHiddenReadingNode(nested)).toBe(false); expect(isHiddenReadingNode(user)).toBe(false);
    const tree = { type: 'root', children: [user, hidden, nested] }; hideReadingMarkers()(tree);
    expect(tree.children).toEqual([user, nested]); expect(nested.children).toHaveLength(1);
  });
});
