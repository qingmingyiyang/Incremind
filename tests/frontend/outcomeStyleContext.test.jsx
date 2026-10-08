import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ContextPanel } from '@src/features/workbench/ContextPanel';

afterEach(cleanup);
const row = label => within(screen.getByLabelText('分类')).getByText(label).closest('.context-part');

it('shows actual writing and frozen previous counts without changing context arithmetic', () => {
  render(<ContextPanel kind="do" receipt={{ context: { window: 10000, parts: [
    { key: 'style', count: 4, tokens: 178 }, { key: 'previous', count: 1, tokens: 320 },
    { key: 'question', count: 1, tokens: 2 },
  ] } }}/>);
  expect([...row('写法').querySelectorAll('span')].slice(1).map(item => item.textContent)).toEqual(['写法', '4', '178', '1.8%']);
  expect([...row('上一版').querySelectorAll('span')].slice(1).map(item => item.textContent)).toEqual(['上一版', '1', '320', '3.2%']);
  expect(row('写法').firstChild).toHaveStyle({ backgroundColor: 'var(--ink2)' });
  expect(row('上一版').firstChild).toHaveStyle({ backgroundColor: 'var(--line2)' });
  expect(screen.getByLabelText('总量')).toHaveTextContent('500 / 10000 5%');
  expect(screen.getByRole('progressbar', { name: '上下文用量' })).toHaveAttribute('aria-valuenow', '500');
  expect(screen.getByTitle('写法 178')).toHaveStyle({ width: '1.78%' });
  expect(screen.getByTitle('上一版 320')).toHaveStyle({ width: '3.2%' });
  expect(screen.queryByText('预留回答')).not.toBeInTheDocument();
});

it('does not invent writing or previous rows when the actual first-draft receipt omits them', () => {
  render(<ContextPanel kind="do" receipt={{ context: { parts: [{ key: 'question', count: 1, tokens: 20 }] } }}/>);
  expect(screen.queryByText('写法')).not.toBeInTheDocument();
  expect(screen.queryByText('上一版')).not.toBeInTheDocument();
  expect(screen.getByLabelText('分类').querySelectorAll('.context-part')).toHaveLength(1);
  expect(screen.getByLabelText('总量')).toHaveTextContent('20 / —');
});

it('hides only an explicitly empty writing row while retaining the original total', () => {
  render(<ContextPanel kind="do" receipt={{ context: { window: 10000, parts: [
    { key: 'style', count: 0, tokens: 0 }, { key: 'question', count: 1, tokens: 200 },
  ] } }}/>);
  expect(screen.queryByText('写法')).not.toBeInTheDocument();
  expect(screen.getByLabelText('分类').querySelectorAll('.context-part')).toHaveLength(1);
  expect(screen.getByLabelText('总量')).toHaveTextContent('200 / 10000 2%');
});

it.each([{ count: 0 }, { tokens: 0 }, {}])('keeps unknown writing measurements visible instead of treating them as empty %#', values => {
  render(<ContextPanel kind="do" receipt={{ context: { parts: [{ key: 'style', ...values }, { key: 'previous', count: 1 }] } }}/>);
  expect(row('写法')).toHaveTextContent('—');
  expect(row('上一版')).toHaveTextContent('1—');
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.getByLabelText('总量')).toHaveTextContent('— / —');
});

it('renders only the new receipt parts when another task replaces the contextual view', () => {
  const view = render(<ContextPanel kind="do" receipt={{ context: { parts: [
    { key: 'style', count: 4, tokens: 178 }, { key: 'previous', count: 1, tokens: 320 },
  ] } }}/>);
  expect(row('写法')).toBeInTheDocument();
  view.rerender(<ContextPanel kind="do" receipt={{ context: { parts: [{ key: 'question', count: 1, tokens: 15 }] } }}/>);
  expect(screen.queryByText('写法')).not.toBeInTheDocument();
  expect(screen.queryByText('上一版')).not.toBeInTheDocument();
  expect(screen.getByLabelText('总量')).toHaveTextContent('15 / —');
});
