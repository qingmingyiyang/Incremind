import { answerWithoutCitationMarks } from '../ui/CitedAnswer';
import { withoutHiddenReadingMarkers } from './readingMarkdown';

function text(value, name) {
  if (typeof value !== 'string') throw new TypeError(`对话${name}必须为文字`);
  return value;
}
function freezeSnapshot(question, answer, citations) {
  return Object.freeze({ question, answer, citations: Object.freeze(citations.map(citation => Object.freeze({ title: citation.title, quote: citation.quote }))) });
}

// 分享只复制白名单文字，原回执、画像及引用定位信息始终留在调用方。
export function createConversationSnapshot({ question, answer, citations = [] }) {
  text(question, '问题'); text(answer, '回答');
  if (!Array.isArray(citations)) throw new TypeError('对话引用必须为列表');
  const numbers = citations.filter(citation => Number.isInteger(citation?.n) && citation.n > 0);
  const ordinary = citations.filter(citation => citation && !citation.persona && citation.layer !== 'persona')
    .map(citation => ({ title: text(citation.title, '引用标题'), quote: text(citation.quote, '引用片段') }));
  return freezeSnapshot(question, answerWithoutCitationMarks(withoutHiddenReadingMarkers(answer), numbers), ordinary);
}

export function removeConversationCitation(snapshot, index) {
  if (!Number.isInteger(index) || index < 0 || index >= snapshot.citations.length) throw new RangeError('引用位置无效');
  return freezeSnapshot(snapshot.question, snapshot.answer, snapshot.citations.filter((_, ordinal) => ordinal !== index));
}

const escapeTitle = value => value.replace(/[\\`*_{}\[\]()#+.!|>~-]/g, '\\$&');
const quoteLines = value => '> ' + value.replace(/\r\n|\r|\n/g, newline => `${newline}> `);
export function conversationSnapshotMarkdown(snapshot) {
  const parts = ['# 对话', `## 问题\n\n${snapshot.question}`, `## 回答\n\n${snapshot.answer}`];
  if (snapshot.citations.length) parts.push('## 引用', ...snapshot.citations.map(citation => `### ${escapeTitle(citation.title)}\n\n${quoteLines(citation.quote)}`));
  return parts.join('\n\n');
}
