import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, test, expect } from 'vitest';
import { OriginalPrivacy } from '../../src/frontend/src/features/library/OriginalPrivacy';

test('original switch saves both revisions then re-reads authoritative privacy', async () => {
  const state = { source_revision:3, policy_revision:2, allowed_purposes:['generation','embedding','rerank'], inherited:false };
  const api = { sourcePrivacy:vi.fn().mockResolvedValueOnce(state).mockResolvedValue({...state,policy_revision:3,allowed_purposes:[]}),
    setSourcePrivacy:vi.fn().mockResolvedValue({}) };
  render(<OriginalPrivacy projectId="alpha" sourceId="original" api={api}/>);
  const toggle = await screen.findByRole('switch',{name:'私密'});
  await waitFor(()=>expect(toggle).not.toBeDisabled());
  fireEvent.click(toggle);
  await waitFor(()=>expect(api.setSourcePrivacy).toHaveBeenCalledWith('alpha','original',state,true));
  await waitFor(()=>expect(toggle).toHaveAttribute('aria-checked','true'));
});

test('inherited private authority is checked and cannot be cancelled', async () => {
  const api = { sourcePrivacy:vi.fn().mockResolvedValue({source_revision:1,policy_revision:0,allowed_purposes:[],inherited:true}) };
  render(<OriginalPrivacy projectId="alpha" sourceId="original" api={api}/>);
  const toggle = await screen.findByRole('switch',{name:'私密'});
  await waitFor(()=>expect(toggle).toHaveAttribute('aria-checked','true'));
  expect(toggle).toBeDisabled();
});

test('revision conflict shows the existing short error and refreshes state', async () => {
  const state = {source_revision:1,policy_revision:0,allowed_purposes:[],inherited:false};
  const api = {sourcePrivacy:vi.fn().mockResolvedValue(state),setSourcePrivacy:vi.fn().mockRejectedValue(new Error('内容已变化 · 刷新'))};
  render(<OriginalPrivacy projectId="alpha" sourceId="original" api={api}/>);
  const toggle=await screen.findByRole('switch',{name:'私密'});
  await waitFor(()=>expect(toggle).not.toBeDisabled());
  fireEvent.click(toggle);
  expect(await screen.findByRole('alert')).toHaveTextContent('内容已变化 · 刷新');
  await waitFor(()=>expect(api.sourcePrivacy).toHaveBeenCalledTimes(2));
});
