import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MemoPanel, InsightMaintenance } from '@src/features/library/LibraryTools';
import { recognitionApi } from '@src/shared/api/recognitionApi';
import { ProjectConstraints } from '@src/features/settings/ProjectConstraints';
vi.mock('@src/shared/api/recognitionApi', async original => {
  const module = await original();
  return { ...module, recognitionApi: { ...module.recognitionApi } };
});

const row = { id: 'recognition-one', revision: 2, kind: 'recognition', state: 'active', text: '保留证据' };
const other = { ...row, id: 'recognition-two', revision: 3, text: '保留数字' };
const settle = async () => act(async () => {});
beforeEach(() => {
  vi.spyOn(recognitionApi, 'loadConstraints').mockResolvedValue({ items: [{ id: 'c', revision: 2, content: '注明出处', enabled: true, effective: true }] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('creates and disables scoped constraints with CAS and validity preserved', async () => {
  const save = vi.spyOn(recognitionApi, 'saveConstraint').mockImplementation(async value => ({ id: value.constraintId, revision: 3, content: value.content, enabled: value.enabled }));
  render(<ProjectConstraints projectId="alpha" onChanged={()=>{}}/>); await settle();
  fireEvent.click(screen.getByRole('switch',{name:'注明出处'})); await settle();
  expect(save).toHaveBeenCalledWith(expect.objectContaining({projectId:'alpha',constraintId:'c',expectedRevision:2,content:'注明出处',enabled:false}));
  fireEvent.change(screen.getByRole('textbox',{name:'新约束'}),{target:{value:'每次引用原文'}});
  fireEvent.submit(screen.getByRole('textbox',{name:'新约束'}).closest('form')); await settle();
  expect(save).toHaveBeenLastCalledWith(expect.objectContaining({projectId:'alpha',expectedRevision:0,content:'每次引用原文',enabled:true}));
});
it('reads existing memos without creating or refreshing models and keeps unknown metadata explicit', async () => {
  vi.spyOn(recognitionApi, 'loadWorkbench').mockResolvedValue({ mental_models: [{ id: 'q', question: '如何阅读？', answer: '保留证据', updated_at: '' }] });
  render(<MemoPanel projectId="alpha" onClose={()=>{}}/>); await settle();
  const panel = screen.getByRole('dialog', { name: '备忘' });
  expect(panel).toHaveTextContent('如何阅读？'); expect(panel).toHaveTextContent('保留证据');
  expect(panel).toHaveTextContent('— · 认 —');
  expect(within(panel).queryByRole('button', { name: /新增|刷新|生成/ })).not.toBeInTheDocument();
  expect(recognitionApi.createMentalModel).toBeUndefined(); expect(recognitionApi.refreshMentalModel).toBeUndefined();
});
it('ignores a late constraint save after changing the project', async () => {
  let resolve;
  const changed = vi.fn();
  vi.spyOn(recognitionApi, 'saveConstraint').mockReturnValue(new Promise(done => { resolve = done; }));
  const view = render(<ProjectConstraints projectId="alpha" onChanged={changed}/>); await settle();
  fireEvent.click(screen.getByRole('switch', { name: '注明出处' }));
  vi.mocked(recognitionApi.loadConstraints).mockResolvedValue({ items: [] });
  view.rerender(<ProjectConstraints projectId="beta" onChanged={changed}/>); await settle();
  await act(async () => resolve({ id: 'c', revision: 3, content: '旧项目约束', enabled: false }));
  expect(screen.queryByText('旧项目约束')).not.toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
});
it('splits only after the user fills and submits both parts', async () => {
  const split=vi.spyOn(recognitionApi,'splitRecognition').mockResolvedValue({});
  render(<InsightMaintenance projectId="alpha" insight={row} insights={[row,other]} action="split" onDone={()=>{}}/>);
  expect(split).not.toHaveBeenCalled(); expect(screen.getByRole('button',{name:'确认拆分'})).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox',{name:'第 1 条'}),{target:{value:'核对原文'}});
  fireEvent.change(screen.getByRole('textbox',{name:'第 2 条'}),{target:{value:'保留出处'}});
  fireEvent.click(screen.getByRole('button',{name:'确认拆分'})); await settle();
  expect(split).toHaveBeenCalledWith({projectId:'alpha',recognitionId:row.id,expectedRevision:2,parts:['核对原文','保留出处']});
});
it('merges selected published identities and revisions with manual text', async () => {
  const merge=vi.spyOn(recognitionApi,'mergeRecognitions').mockResolvedValue({});
  render(<InsightMaintenance projectId="alpha" insight={row} insights={[row,other]} action="merge" onDone={()=>{}}/>);
  fireEvent.click(screen.getByRole('checkbox',{name:'保留数字'}));
  fireEvent.change(screen.getByRole('textbox',{name:'合并正文'}),{target:{value:'证据与数字都保留'}});
  fireEvent.click(screen.getByRole('button',{name:'确认合并'})); await settle();
  expect(merge).toHaveBeenCalledWith({projectId:'alpha',expectedRevisions:{'recognition-one':2,'recognition-two':3},content:'证据与数字都保留',conditions:[]});
});
it('reads actual version content', async () => {
  vi.spyOn(recognitionApi,'loadRecognitionVersions').mockResolvedValue({versions:[{id:'v',version:1,action:'publish',snapshot:{content:'历史正文',conditions:[]}}]});
  render(<InsightMaintenance projectId="alpha" insight={row} insights={[row]} action="versions" onDone={()=>{}}/>); await settle();
  expect(screen.getByText('历史正文')).toBeInTheDocument();
});
it('requires preview and explicit acknowledgement before erasure; conflict invalidates preview', async () => {
  const preview=vi.spyOn(recognitionApi,'previewErasure').mockResolvedValue({preview_id:'p',counts:{recognitions:1,recognition_versions:2}});
  const erase=vi.spyOn(recognitionApi,'eraseRecognition').mockRejectedValue(new Error('conflict'));
  render(<InsightMaintenance projectId="alpha" insight={row} insights={[row]} action="erase" onDone={()=>{}}/>);
  expect(erase).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole('button',{name:'预览删除范围'})); await settle();
  expect(preview).toHaveBeenCalledWith({projectId:'alpha',recognitionId:row.id,expectedRevision:2});
  expect(screen.getByText('历史版本：2')).toBeInTheDocument(); expect(screen.getByRole('button',{name:'永久删除'})).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox',{name:'确认永久删除'})); fireEvent.click(screen.getByRole('button',{name:'永久删除'})); await settle();
  expect(erase).toHaveBeenCalledWith({projectId:'alpha',recognitionId:row.id,expectedRevision:2,previewId:'p'});
  expect(screen.queryByRole('checkbox',{name:'确认永久删除'})).not.toBeInTheDocument();
});
