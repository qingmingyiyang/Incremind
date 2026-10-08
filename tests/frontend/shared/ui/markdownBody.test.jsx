import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MarkdownBody } from '@src/shared/ui/MarkdownBody';
afterEach(cleanup);
it('renders structured text without automatic remote images or active HTML', () => {
  const view = render(<MarkdownBody>{'# 整理稿\n\n![图像](https://example.invalid/tracker)\n\n<script>bad()</script>\n\n[链接](javascript:bad)'}</MarkdownBody>);
  expect(screen.getByRole('heading', { name: '整理稿' })).toBeInTheDocument();
  expect(view.container.querySelector('img,script')).toBeNull();
  expect(screen.getByText('图像')).toBeInTheDocument();
  expect(screen.getByText('链接')).not.toHaveAttribute('href', 'javascript:bad');
});
it('annotates a unique fact using a real evidence callback', () => {
  const fact = { text: '真实事实', evidence: { start: 1, end: 3, quote: '证据' } }, open = vi.fn();
  render(<MarkdownBody evidence={[fact]} onEvidence={open}>{'# 标题\n\n- 真实事实'}</MarkdownBody>);
  fireEvent.click(screen.getByRole('button', { name: '定位原句' }));
  expect(open).toHaveBeenCalledWith(fact);
});
it('keeps facts read-only without a verified callback', () => {
  render(<MarkdownBody evidence={[{ text: '事实' }]}>{'- 事实'}</MarkdownBody>);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
it('renders nested native lists and preserves a non-one ordered start', () => {
  const { container } = render(<MarkdownBody>{'7. 首项\n   - 嵌套圆点\n\n     4. 嵌套数字\n8. 次项'}</MarkdownBody>);
  const ordered = container.querySelector('.ui-markdown-body > ol');
  expect(ordered).toHaveAttribute('start', '7');
  expect(ordered.querySelectorAll(':scope > li')).toHaveLength(2);
  const bullet = ordered.querySelector('li > ul');
  expect(bullet.querySelectorAll(':scope > li')).toHaveLength(1);
  expect(bullet.querySelector('li > ol')).toHaveAttribute('start', '4');
  expect(screen.getByText('次项').closest('li').parentElement).toBe(ordered);
});
