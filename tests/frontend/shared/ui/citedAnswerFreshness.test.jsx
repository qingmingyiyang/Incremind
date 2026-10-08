import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { CitedAnswer } from '@src/shared/ui/CitedAnswer';

it('shows the frozen stale label and opens the original citation', () => {
  const citation = { n: 1, layer: 'insight', id: 'price-2025', title: '已发布认识',
    quote: '价格80元', stale: true };
  const open = vi.fn();
  render(<CitedAnswer answer="以前价格80元[1]" citations={[citation]} onOpenCitation={open}/>);
  const button = screen.getByRole('button', { name: '引用 1 · 认识 · 已发布认识 · 过时' });
  expect(button).toHaveTextContent('1过时');
  expect(screen.getByText('过时', { selector: '.ui-citation-temporal' })).toBeInTheDocument();
  fireEvent.click(button);
  expect(open).toHaveBeenCalledExactlyOnceWith(citation);
});

it('keeps historical and bookshelf labels alongside stale evidence without changing its quote', () => {
  const citation = { n: 2, layer: 'insight', id: 'old', title: '已发布认识',
    quote: '原文与条件', stale: true, historical: true, bookshelf: true };
  render(<CitedAnswer answer="过去的材料【2】" citations={[citation]}/>);
  const mark = screen.getByLabelText('引用 2 · 认识 · 已发布认识 · 当时 · 过时');
  expect(mark).toHaveTextContent('2当时过时');
  expect(mark).toHaveAttribute('title', citation.quote);
  expect(mark.querySelector('svg')).not.toBeNull();
});

it('does not infer stale state from an old receipt or from display text', () => {
  render(<CitedAnswer answer="过时说法[1]" citations={[{
    n: 1, layer: 'insight', id: 'old', title: '旧标题', quote: '2025年', stale: false,
  }]}/>);
  expect(screen.getByLabelText('引用 1 · 认识 · 旧标题')).toHaveTextContent(/^1$/);
  expect(document.querySelector('.ui-citation-temporal')).toBeNull();
});
