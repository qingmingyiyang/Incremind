import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ConsolidationSuggestions } from '../../src/frontend/src/features/library/ConsolidationSuggestions';
import { InsightChip } from '../../src/frontend/src/shared/ui/InsightChip';

afterEach(() => vi.unstubAllGlobals());
it('reviews a frozen merge without publishing and shows all source documents', async () => {
  const changed = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const path = new URL(String(url), 'http://localhost').pathname;
    if (options?.method === 'PATCH') return { ok: true, json: async () => ({ result_candidate_ids: ['new-pending'] }) };
    return { ok: true, json: async () => path.endsWith('/restructure-proposals') ? { items: [{
      id: 'merge1', revision: 3, state: 'pending', operation: 'merge', target_recognition_ids: ['a', 'b'],
      snapshot: { pending_output: true, candidates: [{id:'a',payload:{content:'旧认识'}}], recognitions: [],
        experiences: [{payload:{provenance:{source_refs:[{type:'document',id:'doc1'},{type:'document',id:'doc2'}]}}}] },
      outputs: [{content:'合并认识',conditions:['条件']}], diff:{removed_contents:['旧认识'],added_contents:['合并认识']},
    }] } : { items: [] } };
  }));
  render(<ConsolidationSuggestions projectId="alpha" insightId="a" onReviewed={changed}/>);
  fireEvent.click(await screen.findByRole('button', { name: '合并 1' }));
  expect(await screen.findByText('旧认识')).toBeTruthy();
  expect(screen.getByText('合并认识')).toBeTruthy();
  expect(screen.getByRole('button', {name:'打开整理稿 doc1'})).toBeTruthy();
  expect(screen.getByRole('button', {name:'打开整理稿 doc2'})).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'确认合并方案'}));
  await waitFor(() => expect(changed).toHaveBeenCalled());
  const calls = global.fetch.mock.calls.filter(([,o])=>o?.method);
  expect(calls).toHaveLength(1);
  expect(JSON.parse(calls[0][1].body)).toEqual({project_id:'alpha',expected_revision:3,decision:'approve'});
  expect(String(calls[0][0])).not.toContain('/confirm');
});
it('keeps stale support visible and prevents approval while permitting dismissal', async () => {
  vi.stubGlobal('fetch',vi.fn(async url=>({ok:true,json:async()=>String(url).includes('evidence-support') ? {
    items:[{id:'s',revision:2,state:'pending',current:false,evidence:'支持说明',documents:[{id:'d',revision:1}]}],
  }:{items:[]}})));
  render(<ConsolidationSuggestions projectId="alpha" insightId="a"/>);
  fireEvent.click(await screen.findByRole('button',{name:'支持 1'}));
  expect(screen.getByRole('button',{name:'确认支持'}).disabled).toBe(true);
  expect(screen.getByRole('button',{name:'忽略支持'}).disabled).toBe(false);
});
it('marks patterns with the shared icon and retains the confirmation actions',()=>{
  render(<InsightChip insight={{id:'p',text:'规律正文',state:'pending',pattern:true}} onConfirm={vi.fn()} onDrop={vi.fn()}/>);
  expect(screen.getByRole('img',{name:'规律'})).toBeTruthy();
  expect(screen.getByRole('button',{name:'确认'}).disabled).toBe(false);
});
