import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('distinguishes automatic and manual forgetting and cooling in real library rows', async () => {
  const items = [
    { id:'auto', text:'自动认识', state:'forgotten', recall_by:'auto' },
    { id:'manual', text:'手动认识', state:'forgotten', recall_by:'user' },
    { id:'legacy', text:'历史认识', state:'forgotten' },
    { id:'cooled', text:'淡忘认识', state:'active', recall_state:'cooled' },
  ].map(row => ({ ...row, kind:'recognition', conditions:[], related:[], source_count:1, document_ids:[] }));
  vi.stubGlobal('fetch', vi.fn(async url => ({ ok:true, json:async () => String(url).includes('/insights')
    ? { items, counts:{pending:0,active:1,stale:0,forgotten:3} } : { items:[], archived:[] } })));
  render(<Library projectId="alpha" projects={[{id:'alpha',name:'项目',scenes:[]}]}/>);
  const dot = async text => within((await screen.findByRole('button', {name:text})).closest('.ui-row')).getByRole('img');
  expect(await dot('自动认识')).toHaveAttribute('data-state', 'forgotten');
  expect(await dot('手动认识')).toHaveAttribute('data-state', 'forgotten-user');
  expect(await dot('历史认识')).toHaveAttribute('data-state', 'forgotten-user');
  expect(await dot('淡忘认识')).toHaveAttribute('data-state', 'cooled');
  expect((await dot('手动认识')).closest('.ui-row')).toHaveClass('ui-row-forgotten');
});

it('shows manually forgotten drafts with the solid state point', async () => {
  const doc = {document_id:'doc', title:'整理稿正文', revision:1, verified:true};
  vi.stubGlobal('fetch', vi.fn(async url => ({ok:true, json:async () => /\/notes\?|documents-archived/.test(String(url))
    ? {items:[doc]} : {items:[], counts:{pending:0,active:0,stale:0,forgotten:0}}})));
  render(<Library projectId="alpha" projects={[{id:'alpha',name:'项目',scenes:[]}]}/>);
  fireEvent.click(await screen.findByRole('button', {name:'整理稿 1'}));
  const row = (await screen.findByRole('button', {name:'整理稿正文'})).closest('.ui-row');
  expect(within(row).getByRole('img')).toHaveAttribute('data-state', 'forgotten-user');
  expect(row).toHaveClass('ui-row-forgotten');
});
