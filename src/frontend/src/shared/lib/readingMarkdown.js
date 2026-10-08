import { unified } from 'unified';
import remarkParse from 'remark-parse';

const parser = unified().use(remarkParse);
const systemMarker = '<!-- source_sections.comments: capture-qualified -->';

// 沿用阅读器的根节点语义，不隐藏代码、行内文字或嵌套引用里的字面标记。
export const isHiddenReadingNode = node => node.type === 'html' && node.value.trim() === systemMarker;
export function hideReadingMarkers() {
  return tree => { tree.children = tree.children.filter(node => !isHiddenReadingNode(node)); };
}

// 只从分享副本移除解析器确认的节点区间，原文档及其证据坐标保持原值。
export function withoutHiddenReadingMarkers(markdown) {
  if (typeof markdown !== 'string') throw new TypeError('阅读正文必须为文字');
  const hidden = parser.parse(markdown).children.filter(isHiddenReadingNode);
  if (!hidden.length) return markdown;
  const parts = []; let cursor = 0;
  for (const node of hidden) {
    const start = node.position?.start.offset, end = node.position?.end.offset;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < cursor || end < start || end > markdown.length) throw new Error('阅读标记位置无效');
    parts.push(markdown.slice(cursor, start)); cursor = end;
  }
  parts.push(markdown.slice(cursor)); return parts.join('');
}
