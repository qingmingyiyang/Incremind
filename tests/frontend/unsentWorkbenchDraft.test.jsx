import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { useWorkbenchDraft } from '@src/shared/lib/workbenchDraft';
import { Composer } from '@src/shared/ui/Composer';
import { forgetDeviceCredential, saveDeviceCredential, selectUserSpace, userStorageKey } from '@src/shared/api/deviceTransport';

function DraftInput({ projectId = 'alpha', userEpoch = 0, onSend, handoff }) {
  const { draft, saveDraft, draftKey } = useWorkbenchDraft(projectId, userEpoch);
  return <Composer key={draftKey} draft={draft} onDraft={saveDraft} onSend={onSend} handoff={handoff} projectTag={projectId}/>;
}
const input = () => screen.getByRole('textbox', { name: '输入' });
const saved = project => localStorage.getItem(`${userStorageKey('chriptmas-workbench-draft')}:${project}`);
beforeEach(() => { forgetDeviceCredential(); localStorage.clear(); });
afterEach(() => { cleanup(); forgetDeviceCredential(); localStorage.clear(); });

it('restores text, project scope and intent after remount without attachments', () => {
  const first = render(<DraftInput handoff={{ id: 1, projectId: 'alpha', scene: '选题', intent: 'remember' }}/>);
  fireEvent.change(input(), { target: { value: '待整理的文字' } });
  fireEvent.change(first.container.querySelector('input[type=file]'), { target: { files: [new File(['正文'], '材料.txt')] } });
  expect(JSON.parse(saved('alpha'))).toEqual({ projectId: 'alpha', text: '待整理的文字', selection: 'remember', locked: true, scopeTag: { label: '#alpha/选题', tag: '#alpha/选题' } });
  first.unmount();
  let submitted;
  render(<DraftInput onSend={value => { submitted = value; return false; }}/>);
  expect(input()).toHaveValue('待整理的文字');
  expect(screen.queryByText('材料.txt')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  expect(submitted.text).toBe('#alpha/选题\n待整理的文字');
  expect(submitted.intent).toBe('remember');
  expect(submitted.files).toEqual([]);
});

it('clears accepted input from storage and after remount', async () => {
  const first = render(<DraftInput onSend={async () => true}/>);
  fireEvent.change(input(), { target: { value: '已经提交' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })); });
  expect(input()).toHaveValue('');
  expect(saved('alpha')).toBeNull();
  first.unmount(); render(<DraftInput/>);
  expect(input()).toHaveValue('');
});

it('isolates owner accounts and administrator selected user spaces', () => {
  saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'device-a', user_id: 'owner-a' } });
  const view = render(<DraftInput/>);
  fireEvent.change(input(), { target: { value: '甲账号草稿' } });
  selectUserSpace({ user_id: 'user-b' });
  view.rerender(<DraftInput userEpoch={1}/>);
  expect(input()).toHaveValue('');
  fireEvent.change(input(), { target: { value: '乙空间草稿' } });
  selectUserSpace(null); view.rerender(<DraftInput userEpoch={2}/>);
  expect(input()).toHaveValue('甲账号草稿');
  saveDeviceCredential({ key: 'b'.repeat(43), device: { device_id: 'device-b', user_id: 'owner-b' } });
  view.rerender(<DraftInput userEpoch={3}/>);
  expect(input()).toHaveValue('');
});

it('restores separate project drafts during navigation', () => {
  const view = render(<DraftInput/>);
  fireEvent.change(input(), { target: { value: '甲项目草稿' } });
  view.rerender(<DraftInput projectId="beta"/>);
  expect(input()).toHaveValue('');
  fireEvent.change(input(), { target: { value: '乙项目草稿' } });
  view.rerender(<DraftInput/>);
  expect(input()).toHaveValue('甲项目草稿');
});

it('ignores malformed or mismatched stored input without blocking new edits', () => {
  localStorage.setItem('chriptmas-workbench-draft:alpha', '{');
  localStorage.setItem('chriptmas-workbench-draft:beta', JSON.stringify({ projectId: 'alpha', text: '错误项目的文字' }));
  const view = render(<DraftInput/>);
  expect(input()).toHaveValue('');
  fireEvent.change(input(), { target: { value: '新文字' } });
  expect(JSON.parse(saved('alpha')).text).toBe('新文字');
  view.rerender(<DraftInput projectId="beta"/>);
  expect(input()).toHaveValue('');
});

it('retains new edits when an earlier send is accepted', async () => {
  let accept;
  render(<DraftInput onSend={() => new Promise(resolve => { accept = resolve; })}/>);
  fireEvent.change(input(), { target: { value: '提交时文字' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  fireEvent.change(input(), { target: { value: '提交后新文字' } });
  await act(async () => { accept(true); });
  expect(input()).toHaveValue('提交后新文字');
  expect(JSON.parse(saved('alpha')).text).toBe('提交后新文字');
});

it('does not let a late acceptance replace a draft after switching spaces', async () => {
  saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'device-a', user_id: 'owner-a' } });
  let accept;
  const view = render(<DraftInput onSend={() => new Promise(resolve => { accept = resolve; })}/>);
  fireEvent.change(input(), { target: { value: '甲原草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  selectUserSpace({ user_id: 'user-b' }); view.rerender(<DraftInput userEpoch={1}/>);
  selectUserSpace(null); view.rerender(<DraftInput userEpoch={2}/>);
  fireEvent.change(input(), { target: { value: '甲后续草稿' } });
  await act(async () => { accept(true); });
  expect(input()).toHaveValue('甲后续草稿');
  expect(JSON.parse(saved('alpha')).text).toBe('甲后续草稿');
});

it('preserves new project edits after navigating away and back before acceptance', async () => {
  let accept;
  const view = render(<DraftInput onSend={() => new Promise(resolve => { accept = resolve; })}/>);
  fireEvent.change(input(), { target: { value: '甲发送文字' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  view.rerender(<DraftInput projectId="beta"/>);
  view.rerender(<DraftInput/>);
  fireEvent.change(input(), { target: { value: '甲项目新草稿' } });
  await act(async () => { accept(true); });
  expect(input()).toHaveValue('甲项目新草稿');
  expect(JSON.parse(saved('alpha')).text).toBe('甲项目新草稿');
  view.unmount(); render(<DraftInput/>);
  expect(input()).toHaveValue('甲项目新草稿');
});

it('clears accepted source project input after navigating to another project', async () => {
  let accept;
  const view = render(<DraftInput onSend={() => new Promise(resolve => { accept = resolve; })}/>);
  fireEvent.change(input(), { target: { value: '甲发送文字' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  view.rerender(<DraftInput projectId="beta"/>);
  fireEvent.change(input(), { target: { value: '乙保留文字' } });
  await act(async () => { accept(true); });
  expect(saved('alpha')).toBeNull();
  expect(input()).toHaveValue('乙保留文字');
  view.rerender(<DraftInput/>);
  expect(input()).toHaveValue('');
});
