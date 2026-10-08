import { afterEach, describe, expect, it, vi } from 'vitest';
import { createConversationSnapshot, removeConversationCitation } from '@src/shared/lib/conversationSnapshot';
import { createConversationImagePages, layoutConversationImage } from '@src/shared/lib/conversationImage';

const theme = { paper: '#F7F4EE', ink: '#1C1A17', ink2: '#4E4A44', muted: '#6F6A62', line: '#E6E0D5', serif: '"Noto Serif SC"', sans: '"Noto Sans SC"', mono: '"Fira Code"' };
function canvasFactory() {
  const canvases = [];
  return { canvases, createCanvas: () => {
    const context = { measureText: text => ({ width: Array.from(text).length * 10 }), fillText: vi.fn(), fillRect: vi.fn(), scale: vi.fn() };
    const canvas = { width: 0, height: 0, getContext: vi.fn(() => context), toBlob: vi.fn(callback => callback(new Blob(['png'], { type: 'image/png' }))), context };
    canvases.push(canvas); return canvas;
  } };
}
afterEach(() => vi.unstubAllGlobals());

describe('本机快照图片', () => {
  it('paginates every Unicode code point and explicit blank line without clipping or splitting a surrogate pair', () => {
    const snapshot = createConversationSnapshot({ question: '问题\r\n\r\n😀', answer: ('长文😀é\n\n').repeat(500), citations: [{ title: '末页标题', quote: '末页片段\n第二行' }] });
    const layout = layoutConversationImage(snapshot, text => Array.from(text).length * 10);
    expect(layout.pages.length).toBeGreaterThan(1);
    for (const [key, text] of [['question', snapshot.question], ['answer', snapshot.answer], ['citation-0-title', '末页标题'], ['citation-0-quote', '末页片段\n第二行']]) {
      expect(layout.pages.flatMap(page => page.rows.filter(row => row.key === key)).map(row => row.text + row.break).join('')).toBe(text);
    }
    expect(layout.pages.flatMap(page => page.rows).every(row => row.y + row.height <= layout.pageHeight - layout.padding)).toBe(true);
    expect(layout.pages.flatMap(page => page.rows).some(row => row.text === '' && row.break)).toBe(true);
    expect(layout.pages.flatMap(page => page.rows).some(row => /[\uD800-\uDBFF]$/.test(row.text))).toBe(false);
  });
  it('draws and encodes the current filtered snapshot with token fonts and no hidden fields or remote resources', async () => {
    const original = createConversationSnapshot({ question: '问题', answer: '回答', profile: '画像块', usage: 9, citations: [{ title: '移除标题', quote: '移除片段' }, { title: '保留标题', quote: '保留片段' }] });
    const factory = canvasFactory(); vi.stubGlobal('fetch', vi.fn());
    const pages = await createConversationImagePages(removeConversationCitation(original, 0), { ...factory, theme });
    expect(pages).toHaveLength(1); expect(pages[0].blob.type).toBe('image/png'); expect(pages[0].filename).toBe('对话.png');
    const drawn = factory.canvases.flatMap(canvas => canvas.context.fillText.mock.calls.map(([text]) => text)).join('');
    expect(drawn).toContain('保留标题'); expect(drawn).toContain('保留片段'); expect(drawn).not.toMatch(/移除|画像块|usage/);
    expect(fetch).not.toHaveBeenCalled(); expect(factory.canvases[0].context.fillStyle).toBe(theme.muted);
  });
  it('encodes all pages in order and rejects a failed page rather than delivering incomplete images', async () => {
    const snapshot = createConversationSnapshot({ question: '问', answer: '😀'.repeat(8000) }), factory = canvasFactory();
    const pages = await createConversationImagePages(snapshot, { ...factory, theme });
    expect(pages.length).toBeGreaterThan(1); expect(pages.length).toBe(factory.canvases.length);
    expect(pages.map(page => page.filename)).toEqual(pages.map((_, i) => `对话-${i + 1}.png`));
    const broken = canvasFactory(); const normal = broken.createCanvas;
    broken.createCanvas = () => { const canvas = normal(); if (broken.canvases.length === 2) canvas.toBlob.mockImplementation(callback => callback(null)); return canvas; };
    await expect(createConversationImagePages(snapshot, { ...broken, theme })).rejects.toThrow();
  });
  it('rejects missing tokens and unavailable canvas so the preview can offer a retry', async () => {
    const snapshot = createConversationSnapshot({ question: '问', answer: '答' });
    await expect(createConversationImagePages(snapshot, { theme: {}, createCanvas: () => ({ getContext: () => null }) })).rejects.toThrow();
    await expect(createConversationImagePages(snapshot, { theme, createCanvas: () => ({ getContext: () => null }) })).rejects.toThrow();
  });
  it('keeps the hidden system reading marker out of images while keeping code literals and ordinary user comments', async () => {
    const marker = '<!-- source_sections.comments: capture-qualified -->', factory = canvasFactory();
    const snapshot = createConversationSnapshot({ question: '问', answer: `正文\n\n${marker}\n\n后文` });
    await createConversationImagePages(snapshot, { ...factory, theme });
    const drawn = factory.canvases.flatMap(canvas => canvas.context.fillText.mock.calls.map(([value]) => value)).join('');
    expect(drawn).not.toContain('source_sections.comments'); expect(drawn).toContain('正文'); expect(drawn).toContain('后文');
    expect(createConversationSnapshot({ question: '问', answer: `\`${marker}\`\n\n<!-- 用户注释 -->` }).answer).toBe(`\`${marker}\`\n\n<!-- 用户注释 -->`);
  });
});
