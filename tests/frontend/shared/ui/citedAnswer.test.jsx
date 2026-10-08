import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { CitedAnswer } from '@src/shared/ui/CitedAnswer';
import { LadderTrace } from '@src/shared/ui/LadderTrace';
const citations = [{ n: 3, layer: 'source', id: 's', title: '原文', quote: '数字' }, { n: 1, layer: 'summary', id: 'd', title: '摘要', quote: '依据' }];
it('keeps original numbers and resolves only citation markers present in the receipt', () => {
 const open = vi.fn(); render(<CitedAnswer answer="数字[3]和依据[9]" citations={citations} onOpenCitation={open}/>);
 fireEvent.click(screen.getByRole('button', { name: '引用 3 · 原件 · 原文' })); expect(open).toHaveBeenCalledWith(citations[0]);
 expect(screen.getByText(/和依据\[9\]/)).toBeInTheDocument(); expect(screen.getByLabelText('引用 1 · 摘要 · 摘要')).toHaveTextContent('1');
});
it('uses actual sent persona counts rather than the smaller cited subset', () => {
 render(<LadderTrace layers={{ persona: 2 }} citations={[{ ...citations[0], layer: 'insight', persona: true }]} trace={[]}/>);
 expect(screen.getByRole('group', { name: '画像' })).toHaveTextContent('我2');
 expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
