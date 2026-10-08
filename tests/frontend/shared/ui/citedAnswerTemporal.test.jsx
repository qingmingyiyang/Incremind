import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { CitedAnswer } from '@src/shared/ui/CitedAnswer';

const citation = {
  n: 2, layer: 'insight', id: 'recognition-q49-march',
  title: '三月展会', quote: '北厅小型展', historical: true,
};

it('shows the historical label beside the original number and opens the same citation', () => {
  const open = vi.fn();
  render(<CitedAnswer answer="当时在北厅[2]" citations={[citation]} onOpenCitation={open}/>);
  const reference = screen.getByRole('button', { name: /引用 2 · 认识 · 三月展会/ });
  expect(reference).toHaveTextContent('2当时');
  expect(screen.getByText('当时', { selector: '.ui-citation-temporal' })).toBeInTheDocument();
  fireEvent.click(reference);
  expect(open).toHaveBeenCalledExactlyOnceWith(citation);
});

it('preserves historical and bookshelf indicators together in an actual citation link', () => {
  const open = vi.fn(), historicalBook = { ...citation, bookshelf: true };
  render(<CitedAnswer answer="北厅【2】" citations={[historicalBook]}
    citationHref="#citation" onOpenCitation={open}/>);
  const reference = screen.getByRole('link', { name: /引用 2 · 认识 · 三月展会/ });
  expect(reference).toHaveAttribute('href', '#citation');
  expect(reference).toHaveTextContent('2当时');
  expect(reference.querySelector('svg')).not.toBeNull();
  fireEvent.click(reference);
  expect(open).toHaveBeenCalledExactlyOnceWith(historicalBook);
});

it('keeps old receipts and current citations unchanged without a historical label', () => {
  const { historical, ...oldCitation } = citation;
  render(<CitedAnswer answer="当前[2]" citations={[oldCitation]}/>);
  expect(screen.getByLabelText('引用 2 · 认识 · 三月展会')).toHaveTextContent(/^2$/);
  expect(screen.queryByText('当时')).not.toBeInTheDocument();
});
