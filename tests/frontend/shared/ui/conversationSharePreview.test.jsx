import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createConversationSnapshot } from '@src/shared/lib/conversationSnapshot';
import { ConversationSharePreview } from '@src/shared/ui/ConversationSharePreview';

const snapshot = () => createConversationSnapshot({ question: '原问题😀\n第二行', answer: '真实回答', citations: [{ title: '引用标题', quote: '引用片段' }] });
let writeText, toBlob, createObjectURL, revokeObjectURL, download;
const tokens = { '--paper': '#F7F4EE', '--ink': '#1C1A17', '--ink2': '#4E4A44', '--muted': '#6F6A62', '--line': '#E6E0D5', '--serif': '"Noto Serif SC"', '--sans': '"Noto Sans SC"', '--mono': '"Fira Code"' };
beforeEach(() => {
  writeText = vi.fn(async () => {}); Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  vi.stubGlobal('getComputedStyle', vi.fn(() => ({ getPropertyValue: key => tokens[key] || '' })));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ({ measureText: text => ({ width: Array.from(text).length * 10 }), fillText: vi.fn(), fillRect: vi.fn(), scale: vi.fn() }));
  toBlob = vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['image'], { type: 'image/png' })));
  createObjectURL = vi.fn(() => 'blob:local-image'); revokeObjectURL = vi.fn();
  download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL }); Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
  vi.stubGlobal('fetch', vi.fn());
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('allows a failed clipboard operation to retry and closes through the original focus panel', async () => {
  writeText.mockRejectedValueOnce(new Error('拒绝')); const close = vi.fn(); render(<ConversationSharePreview snapshot={snapshot()} scope="alpha" onClose={close}/>);
  const panel = screen.getByRole('dialog', { name: '分享' }); fireEvent.click(within(panel).getByRole('button', { name: '复制文字' })); await act(async () => {});
  expect(within(panel).getByRole('alert')).toHaveTextContent('复制未完成'); expect(within(panel).getByRole('button', { name: '复制文字' })).toBeEnabled();
  fireEvent.click(within(panel).getByRole('button', { name: '复制文字' })); await act(async () => {}); expect(writeText).toHaveBeenCalledTimes(2); expect(within(panel).queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.keyDown(panel, { key: 'Escape' }); expect(close).toHaveBeenCalledTimes(1); expect(fetch).not.toHaveBeenCalled();
});
it('exports the exact selected snapshot and revokes obsolete and unmounted local image URLs', async () => {
  const app = render(<ConversationSharePreview snapshot={snapshot()} scope="alpha" onClose={() => {}}/>); const panel = screen.getByRole('dialog', { name: '分享' });
  fireEvent.click(within(panel).getByRole('button', { name: '存为图片' })); await act(async () => {});
  expect(toBlob).toHaveBeenCalledTimes(1); const link = within(panel).getByRole('link', { name: '保存图片 1 / 1' }); expect(link).toHaveAttribute('download', '对话.png'); expect(link).toHaveAttribute('href', 'blob:local-image');
  expect(download).toHaveBeenCalledTimes(1); expect(download.mock.contexts[0]).toHaveAttribute('href', 'blob:local-image'); expect(download.mock.contexts[0]).toHaveAttribute('download', '对话.png');
  fireEvent.click(within(panel).getByRole('button', { name: '移除引用 引用标题' })); expect(within(panel).queryByRole('link')).not.toBeInTheDocument(); expect(revokeObjectURL).toHaveBeenCalledTimes(1);
  fireEvent.click(within(panel).getByRole('button', { name: '复制文字' })); await act(async () => {}); expect(writeText.mock.calls[0][0]).not.toContain('引用片段');
  fireEvent.click(within(panel).getByRole('button', { name: '存为图片' })); await act(async () => {}); app.unmount(); expect(revokeObjectURL).toHaveBeenCalledTimes(2); expect(fetch).not.toHaveBeenCalled();
});
it('does not create a download URL when an old image finishes after its scope changes', async () => {
  let finish; toBlob.mockImplementationOnce(callback => { finish = callback; }); const value = snapshot(), app = render(<ConversationSharePreview snapshot={value} scope="alpha" onClose={() => {}}/>);
  fireEvent.click(screen.getByRole('button', { name: '存为图片' })); await act(async () => {}); expect(finish).toBeTypeOf('function');
  app.rerender(<ConversationSharePreview snapshot={value} scope="beta" onClose={() => {}}/>);
  await act(async () => { finish(new Blob(['old'], { type: 'image/png' })); }); expect(createObjectURL).not.toHaveBeenCalled(); expect(screen.queryByRole('link')).not.toBeInTheDocument();
});
it('keeps a late clipboard result from marking a replacement preview copied', async () => {
  let finish; writeText.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })); const value = snapshot(), app = render(<ConversationSharePreview snapshot={value} scope="alpha" onClose={() => {}}/>);
  fireEvent.click(screen.getByRole('button', { name: '复制文字' })); await act(async () => {}); app.rerender(<ConversationSharePreview snapshot={value} scope="beta" onClose={() => {}}/>);
  await act(async () => finish()); expect(screen.getByRole('button', { name: '复制文字' }).querySelector('[data-icon="copy"]')).not.toBeNull(); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
it('rejects a failed PNG and releases partially created URLs before a retry', async () => {
  toBlob.mockImplementationOnce(callback => callback(null)); render(<ConversationSharePreview snapshot={snapshot()} scope="alpha" onClose={() => {}}/>);
  fireEvent.click(screen.getByRole('button', { name: '存为图片' })); await act(async () => {}); expect(screen.getByRole('alert')).toHaveTextContent('图片未生成'); expect(createObjectURL).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '存为图片' })); await act(async () => {}); expect(screen.getByRole('link', { name: '保存图片 1 / 1' })).toBeInTheDocument();
});
it.each(['click', 'Escape'])('invalidates a pending image immediately on close even if the parent delays unmount (%s)', async method => {
  let finish; toBlob.mockImplementationOnce(callback => { finish = callback; }); const close = vi.fn(); render(<ConversationSharePreview snapshot={snapshot()} scope="alpha" onClose={close}/>);
  const panel = screen.getByRole('dialog', { name: '分享' }); fireEvent.click(within(panel).getByRole('button', { name: '存为图片' })); await act(async () => {});
  if (method === 'click') fireEvent.click(within(panel).getByRole('button', { name: '关闭' })); else fireEvent.keyDown(panel, { key: 'Escape' });
  expect(close).toHaveBeenCalledTimes(1); expect(panel).toBeInTheDocument(); await act(async () => finish(new Blob(['late'], { type: 'image/png' })));
  expect(createObjectURL).not.toHaveBeenCalled(); expect(download).not.toHaveBeenCalled(); expect(within(panel).queryByRole('link')).not.toBeInTheDocument();
});
it('invalidates a late clipboard callback immediately on close without claiming to cancel the OS write', async () => {
  let finish; writeText.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })); const close = vi.fn(); render(<ConversationSharePreview snapshot={snapshot()} scope="alpha" onClose={close}/>);
  fireEvent.click(screen.getByRole('button', { name: '复制文字' })); await act(async () => {}); expect(writeText).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '关闭' })); await act(async () => finish());
  expect(close).toHaveBeenCalledTimes(1); expect(screen.getByRole('button', { name: '复制文字' }).querySelector('[data-icon="copy"]')).not.toBeNull();
  expect(createObjectURL).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
});
