import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { CitedAnswer, answerWithoutCitationMarks } from '@src/shared/ui/CitedAnswer';
import { createConversationSnapshot, conversationSnapshotMarkdown } from '@src/shared/lib/conversationSnapshot';

const reason = 'grounded_calendar_provenance_invalid';
const citation = { n: 1, layer: 'source', id: 'painting', title: '真实记录', quote: '原文【未核实】' };
const marked = '😀𠮷原文【未核实】。后来发生。【未核实】 [1]\n后来发生。【未核实】 [1]';
const spans = [];
for (const match of marked.matchAll(/后来发生。【未核实】/g)) {
  const start = Array.from(marked.slice(0, match.index + '后来发生。'.length)).length;
  spans.push({ start, end: start + Array.from('【未核实】').length, claim: 2, reason_codes: [reason] });
}

it('formats only bound system positions after Unicode characters and repeated original text', () => {
  const { container } = render(<CitedAnswer answer={marked} citations={[citation]} unverifiedSpans={spans}/>);
  const marks = screen.getAllByRole('note');
  expect(marks).toHaveLength(2);
  for (const node of marks) {
    expect(node).toHaveTextContent('【未核实】');
    expect(node).toHaveAttribute('title', reason);
    expect(node).toHaveAttribute('data-unverified-claim', '2');
  }
  expect(container.firstChild.textContent).toBe('😀𠮷原文【未核实】。后来发生。【未核实】 1\n后来发生。【未核实】 1');
  expect(container.firstChild.firstChild.textContent).toContain('原文【未核实】');
});

it('keeps old receipts, incorrect coordinates and unknown reasons as original plain text', () => {
  for (const unverifiedSpans of [undefined, [{ ...spans[0], start: spans[0].start + 1 }],
    [{ ...spans[0], reason_codes: ['arbitrary_model_text'] }]]) {
    const view = render(<CitedAnswer answer={marked} citations={[citation]} unverifiedSpans={unverifiedSpans}/>);
    expect(view.queryByRole('note')).not.toBeInTheDocument();
    expect(view.container.firstChild.textContent).toContain('【未核实】');
    view.unmount();
  }
});

it('retains system and original marks in full copy text and immutable share snapshots', () => {
  const expected = '😀𠮷原文【未核实】。后来发生。【未核实】 \n后来发生。【未核实】 ';
  expect(answerWithoutCitationMarks(marked, [citation])).toBe(expected);
  const snapshot = createConversationSnapshot({ question: '问题', answer: marked, citations: [citation] });
  expect(snapshot.answer).toBe(expected);
  expect(conversationSnapshotMarkdown(snapshot)).toContain(expected);
  expect(snapshot.citations[0].quote).toBe('原文【未核实】');
});

it('retains bound marks in an actual selection fragment spanning citation nodes', () => {
  const { container } = render(<CitedAnswer answer={marked} citations={[citation]} unverifiedSpans={spans}/>);
  const answer = container.firstChild;
  const range = document.createRange();
  range.selectNodeContents(answer);
  const selection = document.getSelection();
  selection.removeAllRanges(); selection.addRange(range);
  const fragment = selection.getRangeAt(0).cloneContents();
  fragment.querySelectorAll('sup').forEach(node => node.remove());
  expect(fragment.textContent).toBe(answerWithoutCitationMarks(marked, [citation]));
  expect(fragment.querySelectorAll('.ui-unverified-claim')).toHaveLength(2);
  selection.removeAllRanges();
});
