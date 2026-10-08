import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { CitedAnswer } from '@src/shared/ui/CitedAnswer';

afterEach(cleanup);
it('adds a book beside each restored citation without adding visible words', () => {
 const { container } = render(<CitedAnswer answer="旧暗号[1]" citations={[{ n: 1, layer: 'insight', title: '暗号', bookshelf: true }]}/>);
 expect(container.querySelector('[data-icon="book"]')).toBeTruthy();
 expect(container.textContent).toBe('旧暗号1');
 expect(screen.getByLabelText('引用 1 · 认识 · 暗号')).toBeInTheDocument();
});
it('keeps ordinary citations unchanged', () => {
 const { container } = render(<CitedAnswer answer="暗号[1]" citations={[{ n: 1, layer: 'insight', title: '暗号' }]}/>);
 expect(container.querySelector('[data-icon="book"]')).toBeNull();
 expect(container.textContent).toBe('暗号1');
});
