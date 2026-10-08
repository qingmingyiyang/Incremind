import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';
import { WorkbenchDraftPanel } from '@src/features/workbench/WorkbenchDraftPanel';
const markdown = '# Draft\n\n## 摘要\nSummary\n\n## 事实\n- Fact one\n- Fact two';
const note = { document_id: 'doc-1', title: 'Draft title', revision: 4, markdown,
  facts: [{ text: 'Fact one', evidence: { start: 6, end: 11, quote: 'first' } },
          { text: 'Fact two', evidence: { start: 16, end: 22, quote: 'second' } }], todos: [] };
const original = { id: 'source-1', title: 'Original title', text: 'start first and second end',
  coordinate_space: 'workspace_source_text_v1', url: 'https://example.invalid/original', download_url: null };
let detail, source, saveConflict;
const reply = (value, status=200) => ({ ok: status < 400, status, json: async () => value, text: async () => JSON.stringify(value) });
beforeEach(() => {
 localStorage.clear(); saveConflict = false; source = {...original};
 detail = { note: {...note}, summary: { text: 'Summary' }, source: {...original, window: null},
  sources: [{ id: 'source-1', title: 'Original title', kind: 'text' }], documents: [{document_id:'doc-1', title:'Draft title'}] };
 vi.stubGlobal('fetch', vi.fn(async (url, options={}) => {
  const path = String(url);
  if (options.method === 'PATCH') {
   if (saveConflict) { detail = {...detail, note:{...note, revision: 5, markdown: 'Server version'}}; return reply({detail:'revision_conflict'},409); }
   return reply({revision:5});
  }
  if (path.includes('/library/drill?')) return reply(detail);
  if (path.includes('/sources/') && path.includes('/text?')) return reply(source);
  if (path.includes('/threads/thread-1')) return reply({id:'thread-1', turns:[{id:'turn-1', intent:'remember', receipt:{remember:{state:'done',title:'Receipt', document_id:'doc-1',progress:{done:4,total:4}, insights:[],related:[]}}}]});
  return reply({items:[{id:'thread-1',title:'Thread'}]});
 }));
});
const originalScroll = HTMLElement.prototype.scrollIntoView;
it('opens every grouped original image in the saved upload order', async () => {
 const normal = fetch.getMockImplementation();
 fetch.mockImplementation((url, options) => String(url).includes('/items/image-item/images?')
  ? Promise.resolve(reply({images:[{ordinal:1,name:'first.png',url:'/first-image'}, {ordinal:2,name:'second.png',url:'/second-image'}]})) : normal(url, options));
 render(<WorkbenchDraftPanel projectId="alpha" documentId="doc-1" itemId="image-item" onClose={()=>{}}/>);
 await act(async()=>{});
 expect(screen.getByRole('link',{name:'first.png'})).toHaveAttribute('href','/first-image');
 expect(screen.getByRole('link',{name:'second.png'})).toHaveAttribute('href','/second-image');
 const links = screen.getAllByRole('link').filter(link=>/\.png$/.test(link.textContent));
 expect(links.map(link=>link.textContent)).toEqual(['first.png','second.png']);
});
it('discards an old item attachment response after changing project and item', async () => {
 let finish;
 const normal = fetch.getMockImplementation();
 fetch.mockImplementation((url, options) => String(url).includes('/items/old-image/images?')
  ? new Promise(resolve=>{finish=resolve;}) : String(url).includes('/items/new-image/images?')
  ? Promise.resolve(reply({images:[{ordinal:1,name:'new.png',url:'/new-image'}]})) : normal(url, options));
 const view = render(<WorkbenchDraftPanel projectId="alpha" documentId="doc-1" itemId="old-image" onClose={()=>{}}/>);
 await act(async()=>{});
 view.rerender(<WorkbenchDraftPanel projectId="beta" documentId="doc-1" itemId="new-image" onClose={()=>{}}/>);
 await act(async()=>{});
 await act(async()=>finish(reply({images:[{ordinal:1,name:'old.png',url:'/old-image'}]})));
 expect(screen.getByRole('link',{name:'new.png'})).toHaveAttribute('href','/new-image');
 expect(screen.queryByRole('link',{name:'old.png'})).not.toBeInTheDocument();
});
afterEach(() => {cleanup();vi.unstubAllGlobals();HTMLElement.prototype.scrollIntoView = originalScroll;});
async function open() {
 render(<Workbench projectId="alpha" threadId="thread-1"/>);
 await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'打开整理稿'}));
 await act(async()=>{});
 return screen.getByRole('dialog',{name:'整理稿'});
}
it('opens the receipt as the real source and draft comparison and marks each frozen fact',async()=>{
 const panel = await open();
 expect(within(panel).getByRole('heading',{name:'Draft',level:1})).toBeInTheDocument();
 const marks = [...panel.querySelectorAll('mark')];
 expect(marks.map(m=>m.textContent)).toEqual(['first','second']);
 expect(fetch.mock.calls.find(([u])=>String(u).includes('/library/drill?'))[0]).toContain('from=note');
 expect(fetch.mock.calls.find(([u])=>String(u).includes('/sources/source-1/text?'))[0]).toContain('project_id=alpha');
 expect(screen.queryByRole('combobox',{name:'原件'})).not.toBeInTheDocument();
});
it('clicks a fact to scroll to the exact marked range',async()=>{
 const scroll = vi.fn(); HTMLElement.prototype.scrollIntoView = scroll;
 const panel = await open(); fireEvent.click(within(panel).getByRole('button',{name:'定位原文：Fact two'}));
 expect(panel.querySelector('mark[data-current="true"]')?.textContent).toBe('second');
 expect(scroll).toHaveBeenCalled();
});
it('saves the edited draft with the revision captured at opening',async()=>{
 await open(); fireEvent.click(screen.getByRole('button',{name:'更多编辑'})); fireEvent.click(screen.getByRole('button',{name:'源码'})); fireEvent.change(screen.getByRole('textbox',{name:'整理稿正文'}),{target:{value:'My edited draft'}});
 fireEvent.click(screen.getByRole('button',{name:'保存'})); await act(async()=>{});
 const call = fetch.mock.calls.find(([,opts])=>opts?.method==='PATCH');
 expect(String(call[0])).toContain('/api/recognition/documents/doc-1');
 expect(JSON.parse(call[1].body)).toEqual({project_id:'alpha',expected_revision:4,markdown:'My edited draft'});
});
it('reads current data after 409 and preserves all three snapshots',async()=>{
 saveConflict = true; const panel = await open(); fireEvent.click(screen.getByRole('button',{name:'更多编辑'})); fireEvent.click(screen.getByRole('button',{name:'源码'}));
 fireEvent.change(screen.getByRole('textbox',{name:'整理稿正文'}),{target:{value:'My version'}});
 fireEvent.click(screen.getByRole('button',{name:'保存'})); await act(async()=>{});
 const conflict = within(panel).getByRole('alert',{name:'草稿版本冲突'});
 expect(conflict.textContent).toContain(markdown);
 expect(conflict).toHaveTextContent('My version'); expect(conflict).toHaveTextContent('Server version');
 expect(fetch.mock.calls.filter(([u])=>String(u).includes('/library/drill?'))).toHaveLength(2);
 fireEvent.click(within(conflict).getByRole('button',{name:'保留本地内容继续编辑'}));
 expect(screen.getByRole('textbox',{name:'整理稿正文'})).toHaveValue('My version');
 saveConflict = false;
 fireEvent.click(screen.getByRole('button',{name:'保存'})); await act(async()=>{});
 const saves = fetch.mock.calls.filter(([,opts])=>opts?.method==='PATCH');
 expect(JSON.parse(saves[1][1].body)).toEqual({project_id:'alpha',expected_revision:5,markdown:'My version'});
});
it('switches among originals while preserving unsaved edits',async()=>{
 detail = {...detail, source:null, sources:[...detail.sources,{id:'source-2',title:'Second original',kind:'text'}]};
 await open(); fireEvent.click(screen.getByRole('button',{name:'更多编辑'})); fireEvent.click(screen.getByRole('button',{name:'源码'})); fireEvent.change(screen.getByRole('textbox',{name:'整理稿正文'}),{target:{value:'Unsaved'}});
 source = {...original,id:'source-2',title:'Second original',text:'Different original'};
 fireEvent.change(screen.getByRole('combobox',{name:'原件'}),{target:{value:'source-2'}}); await act(async()=>{});
 expect(screen.getByText('Different original')).toBeInTheDocument();
 expect(screen.getByRole('textbox',{name:'整理稿正文'})).toHaveValue('Unsaved');
 expect(fetch.mock.calls.some(([u])=>String(u).includes('/sources/source-2/text?'))).toBe(true);
});
it('shows only the original title and link when the full text is empty',async()=>{
 source = {...original,text:''}; const panel = await open();
 expect(within(panel).getByText('Original title')).toBeInTheDocument();
 expect(within(panel).getByRole('link',{name:'打开原件'})).toHaveAttribute('href',original.url);
 expect(panel.querySelectorAll('mark')).toHaveLength(0);
});
it('uses Python Unicode coordinates and refuses mismatched frozen quotes',async()=>{
 source = {...original,text:'🧸first tail'};
 detail = {...detail,note:{...note,markdown:'# Draft\n\n- Emoji fact\n- Wrong fact',facts:[{text:'Emoji fact',evidence:{start:1,end:6,quote:'first'}},{text:'Wrong fact',evidence:{start:7,end:11,quote:'nope'}}]}};
 const panel = await open();
 expect([...panel.querySelectorAll('mark')].map(mark=>mark.textContent)).toEqual(['first']);
 fireEvent.click(within(panel).getByRole('button',{name:'定位原文：Wrong fact'}));
 expect(panel.querySelector('mark[data-current="true"]')).toBeNull();
});
it('drops an old-project draft response after the project changes',async()=>{
 let resolve; const normal = fetch.getMockImplementation();
 fetch.mockImplementation((url,opts)=>String(url).includes('/library/drill?') ? new Promise(r=>{resolve=r;}) : normal(url,opts));
 const view=render(<Workbench projectId="alpha" threadId="thread-1"/>); await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'打开整理稿'})); await act(async()=>{});
 view.rerender(<Workbench projectId="beta"/>); await act(async()=>{});
 await act(async()=>resolve(reply(detail)));
 expect(screen.queryByRole('dialog',{name:'整理稿'})).not.toBeInTheDocument();
});

function rememberedThread(id) {
 return {id:`thread-${id}`,turns:[{id:`turn-${id}`,intent:'remember',receipt:{remember:{
  state:'done',title:`Receipt ${id}`,document_id:`doc-${id}`,progress:{done:4,total:4},insights:[],related:[]
 }}}]};
}
function delayedThreads() {
 const normal = fetch.getMockImplementation(), finish = {};
 fetch.mockImplementation((url,options)=>{
  const path=String(url), match=path.match(/\/threads\/thread-(\d+)\?/);
  if(match) return match[1]==='1' ? Promise.resolve(reply(rememberedThread('1')))
   : new Promise(resolve=>{finish[match[1]]=resolve;});
  if(path.includes('/threads?')) return Promise.resolve(reply({items:[1,2,3].map(id=>({id:`thread-${id}`,title:`Thread ${id}`}))}));
  return normal(url,options);
 });
 return finish;
}
it.each([{threadId:'thread-2'},{turnId:'turn-2'}])('hides old receipts until same-project navigation loads %j',async target=>{
 const finish=delayedThreads();
 const view=render(<Workbench projectId="alpha" threadId="thread-1"/>); await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'打开整理稿'})); await act(async()=>{});
 expect(screen.getByRole('dialog',{name:'整理稿'})).toBeInTheDocument();
 const before=fetch.mock.calls.filter(([url])=>String(url).includes('/library/drill?')).length;
 view.rerender(<Workbench projectId="alpha" {...target}/>); await act(async()=>{});
 expect(finish['2']).toBeTypeOf('function');
 expect(screen.queryByText('Receipt 1')).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'打开整理稿'})).not.toBeInTheDocument();
 expect(screen.queryByRole('dialog',{name:'整理稿'})).not.toBeInTheDocument();
 expect(fetch.mock.calls.filter(([url])=>String(url).includes('/library/drill?'))).toHaveLength(before);
 await act(async()=>finish['2'](reply(rememberedThread('2'))));
 expect(screen.getByText('Receipt 2')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'打开整理稿'})); await act(async()=>{});
 const calls=fetch.mock.calls.filter(([url])=>String(url).includes('/library/drill?'));
 expect(new URL(calls.at(-1)[0],'https://example.invalid').searchParams.get('id')).toBe('doc-2');
});
it('ignores the delayed prior thread after another same-project navigation',async()=>{
 const finish=delayedThreads();
 const view=render(<Workbench projectId="alpha" threadId="thread-1"/>); await act(async()=>{});
 view.rerender(<Workbench projectId="alpha" threadId="thread-2"/>); await act(async()=>{});
 view.rerender(<Workbench projectId="alpha" threadId="thread-3"/>); await act(async()=>{});
 await act(async()=>finish['2'](reply(rememberedThread('2'))));
 expect(screen.queryByText('Receipt 1')).not.toBeInTheDocument();
 expect(screen.queryByText('Receipt 2')).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'打开整理稿'})).not.toBeInTheDocument();
 await act(async()=>finish['3'](reply(rememberedThread('3'))));
 expect(screen.getByText('Receipt 3')).toBeInTheDocument();
 expect(screen.queryByText('Receipt 2')).not.toBeInTheDocument();
});
it('ignores a late same-project draft while the next thread is loading',async()=>{
 const finish=delayedThreads(), normal=fetch.getMockImplementation(); let finishDraft;
 fetch.mockImplementation((url,options)=>String(url).includes('/library/drill?')
  ? new Promise(resolve=>{finishDraft=resolve;}) : normal(url,options));
 const view=render(<Workbench projectId="alpha" threadId="thread-1"/>); await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'打开整理稿'})); await act(async()=>{});
 view.rerender(<Workbench projectId="alpha" threadId="thread-2"/>); await act(async()=>{});
 await act(async()=>finishDraft(reply(detail)));
 expect(screen.queryByRole('dialog',{name:'整理稿'})).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'打开整理稿'})).not.toBeInTheDocument();
 await act(async()=>finish['2'](reply(rememberedThread('2'))));
 expect(screen.getByText('Receipt 2')).toBeInTheDocument();
 expect(screen.queryByText('Draft title')).not.toBeInTheDocument();
});
it('preserves input and waits to send into the newly loaded thread',async()=>{
 const finish=delayedThreads(), normal=fetch.getMockImplementation();
 fetch.mockImplementation((url,options)=>String(url).endsWith('/turns')
  ? Promise.resolve(reply({thread_id:'thread-2',turn:{...rememberedThread('2').turns[0],id:'turn-2-new'}})) : normal(url,options));
 const view=render(<Workbench projectId="alpha" threadId="thread-1"/>); await act(async()=>{});
 fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'Keep this input'}});
 view.rerender(<Workbench projectId="alpha" threadId="thread-2"/>); await act(async()=>{});
 fireEvent.click(screen.getByRole('button',{name:'发送'})); await act(async()=>{});
 const posted=()=>fetch.mock.calls.filter(([url,options])=>String(url).endsWith('/turns') && options?.method==='POST');
 expect(posted()).toHaveLength(0);
 expect(screen.getByRole('textbox',{name:'输入'})).toBeDisabled();
 expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('Keep this input');
 expect(screen.getByRole('button',{name:'发送'})).toBeDisabled();
 expect(screen.getByRole('button',{name:'录音'})).toBeDisabled();
 await act(async()=>finish['2'](reply(rememberedThread('2'))));
 expect(screen.getByRole('textbox',{name:'输入'})).toBeEnabled();
 expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('Keep this input');
 fireEvent.click(screen.getByRole('button',{name:'发送'})); await act(async()=>{});
 expect(posted()).toHaveLength(1);
 expect(JSON.parse(posted()[0][1].body)).toEqual({project_id:'alpha',thread_id:'thread-2',text:'Keep this input',intent:'remember'});
});
