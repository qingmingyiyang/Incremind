import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';
import { Composer } from '@src/shared/ui/Composer';
import { AppRouter } from '@src/AppRouter';

const gap = {id:'gap-1',scene:'阅读',text:'怎么核对预算来源？',count:3,last_at:'2026-10-05T12:00:00Z'};
const projects = [{id:'alpha',name:'甲',scenes:['阅读']},{id:'beta',name:'乙',scenes:[]}];
const response = (value,status=200) => ({ok:status<400,status,json:async()=>value});
const settle = async () => act(async()=>{});
let gaps;
beforeEach(()=>{
  gaps = [gap]; localStorage.clear(); window.location.hash = 'view=library&project_id=alpha';
  vi.stubGlobal('fetch',vi.fn(async(url,options={})=>{
    const path = new URL(String(url),'http://localhost');
    if (path.pathname.endsWith('/gaps')) return response({items:gaps});
    if (path.pathname.endsWith('/dismiss')) {gaps=[];return response({},204);}
    if (path.pathname.endsWith('/projects')) return response({items:projects});
    if (path.pathname.endsWith('/consolidate')) return response({score:0,running:false,limit:false});
    return response({items:[],counts:{pending:0,active:0,stale:0,forgotten:0}});
  }));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();window.location.hash='';});
async function openMenu(){fireEvent.click(screen.getByRole('button',{name:'资料库更多'}));await settle();}
async function openGaps(){await openMenu();fireEvent.click(screen.getByRole('menuitem',{name:'待补 1'}));await settle();return screen.getByRole('dialog',{name:'待补'});}

it('shows the scoped count and existing question in FocusPanel and dismisses only its ID',async()=>{
  const navigate=vi.fn();render(<Library projectId="alpha" projects={projects} onNavigate={navigate}/>);await settle();
  const panel=await openGaps();
  expect(panel).toHaveTextContent('3 次 · 10·05');
  fireEvent.click(within(panel).getByRole('button',{name:gap.text+' 3 次 · 10·05'}));
  expect(navigate).toHaveBeenCalledWith('workbench',{project_id:'alpha',compose:{scene:'阅读',intent:'remember'}});
  expect(fetch.mock.calls.filter(([url])=>String(url).endsWith('/turns'))).toHaveLength(0);
  fireEvent.click(within(panel).getByRole('button',{name:'丢弃 '+gap.text}));await settle();
  expect(JSON.parse(fetch.mock.calls.find(([url])=>String(url).endsWith('/dismiss'))[1].body)).toEqual({project_id:'alpha'});
  expect(within(panel).queryByText(gap.text)).not.toBeInTheDocument();
});

it('hides the whole zero-count menu item',async()=>{
  gaps=[];render(<Library projectId="alpha" projects={projects}/>);await settle();await openMenu();
  expect(screen.queryByRole('menuitem',{name:/待补/})).not.toBeInTheDocument();
});

it('handoff focuses and locks remember once while preserving existing Composer text and files',async()=>{
  const send=vi.fn(async()=>false);
  const {rerender}=render(<Composer projectTag="甲" onSend={send}/>);
  const input=screen.getByRole('textbox',{name:'输入'}),file=new File(['Synthetic'],'synthetic.txt',{type:'text/plain'});
  fireEvent.change(input,{target:{value:'原有草稿？'}});
  fireEvent.change(screen.getByLabelText('文件'),{target:{files:[file]}});
  fireEvent.click(screen.getByRole('button',{name:'问'}));
  const handoff={id:1,scene:'阅读',intent:'remember'};
  rerender(<Composer projectTag="甲" onSend={send} handoff={handoff}/>);await settle();
  expect(input).toHaveFocus();expect(input).toHaveValue('原有草稿？');
  expect(screen.getByText('#甲/阅读')).toBeInTheDocument();expect(screen.getByRole('button',{name:'记住'})).toHaveAttribute('aria-pressed','true');
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'问'}));
  rerender(<Composer projectTag="甲" onSend={send} handoff={handoff}/>);await settle();
  expect(screen.getByRole('button',{name:'问'})).toHaveAttribute('aria-pressed','true');
  fireEvent.click(screen.getByRole('button',{name:'发送'}));await settle();
  expect(send).toHaveBeenCalledWith({text:'#甲/阅读 原有草稿？',files:[file],intent:'ask'});
});

it('real Router navigation preserves per-project draft without prefilling the gap question',async()=>{
  window.location.hash='view=workbench&project_id=alpha';render(<AppRouter/>);
  const input=await screen.findByRole('textbox',{name:'输入'});
  fireEvent.change(input,{target:{value:'现有输入草稿'}});
  const file=new File(['Synthetic'],'kept.txt',{type:'text/plain'});
  fireEvent.change(screen.getByLabelText('文件'),{target:{files:[file]}});
  fireEvent.click(screen.getByRole('button',{name:'资料库'}));await screen.findByRole('button',{name:'资料库更多'});await settle();
  const panel=await openGaps();fireEvent.click(within(panel).getByRole('button',{name:gap.text+' 3 次 · 10·05'}));
  const resumed=await screen.findByRole('textbox',{name:'输入'});await settle();
  expect(resumed).toHaveValue('现有输入草稿');expect(resumed).toHaveFocus();
  expect(screen.getByText('kept.txt')).toBeInTheDocument();expect(screen.getByText('#甲/阅读')).toBeInTheDocument();
  expect(screen.getByRole('button',{name:'记住'})).toHaveAttribute('aria-pressed','true');
  expect(resumed.value).not.toContain(gap.text);
  expect(fetch.mock.calls.filter(([url,options])=>String(url).endsWith('/turns')&&options?.method==='POST')).toHaveLength(0);
});

it('accepts the actual empty 204 dismissal without parsing a JSON body',async()=>{
  const original=fetch, json=vi.fn(async()=>{throw new SyntaxError('empty');});
  vi.stubGlobal('fetch',vi.fn((url,options)=>String(url).endsWith('/dismiss')
    ? Promise.resolve({ok:true,status:204,json}) : original(url,options)));
  render(<Library projectId="alpha" projects={projects}/>);await settle();
  const panel=await openGaps();fireEvent.click(within(panel).getByRole('button',{name:'丢弃 '+gap.text}));await settle();
  expect(within(panel).queryByText(gap.text)).not.toBeInTheDocument();
  expect(json).not.toHaveBeenCalled();expect(within(panel).queryByRole('alert')).not.toBeInTheDocument();
});

it('opens one handoff for repeated activation of the same gap row',async()=>{
  const navigate=vi.fn();render(<Library projectId="alpha" projects={projects} onNavigate={navigate}/>);await settle();
  const panel=await openGaps(), row=within(panel).getByRole('button',{name:gap.text+' 3 次 · 10·05'});
  fireEvent.click(row);fireEvent.click(row);expect(navigate).toHaveBeenCalledTimes(1);
});

it('ignores late reads from an earlier project and aborts their real request',async()=>{
  const original=fetch;let resolve, signal;
  vi.stubGlobal('fetch',vi.fn((url,options)=>{
    const path=new URL(String(url),'http://localhost');
    if(path.pathname.endsWith('/gaps')&&path.searchParams.get('project_id')==='alpha') {
      signal=options.signal;return new Promise(done=>{resolve=done;});
    }
    if(path.pathname.endsWith('/gaps'))return Promise.resolve(response({items:[]}));
    return original(url,options);
  }));
  const {rerender}=render(<Library projectId="alpha" projects={projects}/>);await settle();
  rerender(<Library projectId="beta" projects={projects}/>);await settle();
  await act(async()=>{resolve(response({items:[gap]}));});await openMenu();
  expect(signal.aborted).toBe(true);expect(screen.queryByRole('menuitem',{name:/待补/})).not.toBeInTheDocument();
});

it('holds one dismissal while busy and offers a real read retry after safe failure',async()=>{
  const original=fetch;let reject;
  vi.stubGlobal('fetch',vi.fn((url,options)=>String(url).endsWith('/dismiss')
    ? new Promise((_,fail)=>{reject=fail;}) : original(url,options)));
  const navigate=vi.fn();render(<Library projectId="alpha" projects={projects} onNavigate={navigate}/>);await settle();
  const panel=await openGaps(), close=within(panel).getByRole('button',{name:'丢弃 '+gap.text});
  fireEvent.click(close);fireEvent.click(close);
  expect(fetch.mock.calls.filter(([url])=>String(url).endsWith('/dismiss'))).toHaveLength(1);
  expect(close).toBeDisabled();expect(within(panel).queryByRole('button',{name:gap.text+' 3 次 · 10·05'})).not.toBeInTheDocument();
  await act(async()=>{reject(new Error('Synthetic transport detail'));});
  expect(within(panel).getByRole('alert')).toHaveTextContent('操作未完成 · 重试');
  expect(panel).not.toHaveTextContent('Synthetic transport detail');
  const before=fetch.mock.calls.filter(([url])=>String(url).includes('/gaps?')).length;
  fireEvent.click(within(panel).getByRole('button',{name:'重试'}));await settle();
  expect(fetch.mock.calls.filter(([url])=>String(url).includes('/gaps?'))).toHaveLength(before+1);
  expect(navigate).not.toHaveBeenCalled();
});

it('ordinary navigation and project switching keep each existing draft isolated without refocusing',async()=>{
  window.location.hash='view=workbench&project_id=alpha';render(<AppRouter/>);
  const input=await screen.findByRole('textbox',{name:'输入'});await settle();
  fireEvent.change(input,{target:{value:'甲的原输入？'}});
  fireEvent.change(screen.getByLabelText('文件'),{target:{files:[new File(['Synthetic'],'alpha.txt')]}});
  fireEvent.click(screen.getByRole('button',{name:'问'}));
  fireEvent.click(screen.getByRole('button',{name:'资料库'}));await screen.findByRole('button',{name:'资料库更多'});
  fireEvent.click(screen.getByRole('button',{name:'工作台'}));
  expect(await screen.findByRole('textbox',{name:'输入'})).toHaveValue('甲的原输入？');
  expect(screen.getByRole('button',{name:'问'})).toHaveAttribute('aria-pressed','true');
  fireEvent.click(screen.getByRole('button',{name:'切换项目'}));fireEvent.click(screen.getByRole('option',{name:'乙'}));await settle();
  expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('');expect(screen.queryByText('alpha.txt')).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'乙的草稿'}});
  fireEvent.click(screen.getByRole('button',{name:'切换项目'}));fireEvent.click(screen.getByRole('option',{name:'甲'}));await settle();
  expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('甲的原输入？');expect(screen.getByText('alpha.txt')).toBeInTheDocument();
  expect(screen.getByRole('textbox',{name:'输入'})).not.toHaveFocus();
});

it('keeps a manual remember lock across draft restoration even when auto-selection already matches',async()=>{
  let draft;const save=value=>{draft=value;};
  const first=render(<Composer onDraft={save}/>);
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'https://example.test/synthetic'}});
  fireEvent.click(screen.getByRole('button',{name:'记住'}));first.unmount();
  render(<Composer draft={draft}/>);
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'怎么核对？'}});
  expect(screen.getByRole('button',{name:'记住'})).toHaveAttribute('aria-pressed','true');
});

it('does not reinsert a dismissed gap from an older pending GET',async()=>{
  const original=fetch,resolvers=[];let hold=false;
  vi.stubGlobal('fetch',vi.fn((url,options)=>String(url).includes('/gaps?')&&hold
    ? new Promise(resolve=>resolvers.push(resolve)) : original(url,options)));
  render(<Library projectId="alpha" projects={projects}/>);await settle();hold=true;
  const panel=await openGaps();fireEvent.click(within(panel).getByRole('button',{name:'丢弃 '+gap.text}));await settle();
  await act(async()=>{resolvers.forEach(resolve=>resolve(response({items:[gap]})));});
  expect(within(panel).queryByText(gap.text)).not.toBeInTheDocument();
});

it('aborts dismissal on panel close and ignores its late failure',async()=>{
  const original=fetch;let reject,signal;
  vi.stubGlobal('fetch',vi.fn((url,options)=>{
    if(String(url).endsWith('/dismiss')) {signal=options.signal;return new Promise((_,fail)=>{reject=fail;});}
    return original(url,options);
  }));
  render(<Library projectId="alpha" projects={projects}/>);await settle();
  const panel=await openGaps();fireEvent.click(within(panel).getByRole('button',{name:'丢弃 '+gap.text}));
  fireEvent.click(within(panel).getByRole('button',{name:'关闭'}));
  await act(async()=>{reject(new Error('Synthetic late failure'));});
  expect(signal.aborted).toBe(true);expect(screen.queryByRole('dialog',{name:'待补'})).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('an explicit new scope after handoff wins while retaining text files and manual intent',async()=>{
  const send=vi.fn(async()=>false);
  render(<Composer projectTag="甲" handoff={{id:1,scene:'阅读',intent:'remember'}} onSend={send}/>);await settle();
  const input=screen.getByRole('textbox',{name:'输入'}),file=new File(['Synthetic'],'retained.txt');
  fireEvent.change(screen.getByLabelText('文件'),{target:{files:[file]}});
  fireEvent.click(screen.getByRole('button',{name:'干活'}));
  fireEvent.change(input,{target:{value:'#乙/工作 新材料'}});
  expect(input).toHaveValue('#乙/工作 新材料');expect(screen.getByText('retained.txt')).toBeInTheDocument();
  expect(screen.getByText('#乙/工作')).toBeInTheDocument();
  expect(screen.getByRole('button',{name:'干活'})).toHaveAttribute('aria-pressed','true');
  expect(send).not.toHaveBeenCalled();fireEvent.click(screen.getByRole('button',{name:'发送'}));await settle();
  expect(send).toHaveBeenCalledWith({text:'#乙/工作 新材料',files:[file],intent:'do'});
});

it('real Router sends the handoff project ID when project labels contain spaces and repeat',async()=>{
  const original=fetch, labels=[{id:'alpha',name:'同名 项目',scenes:['阅读']},{id:'beta',name:'同名 项目',scenes:['阅读']}];
  vi.stubGlobal('fetch',vi.fn((url,options={})=>{
    if(String(url).endsWith('/projects'))return Promise.resolve(response({items:labels}));
    if(String(url).endsWith('/turns')&&options.method==='POST')return Promise.resolve(response({thread_id:'synthetic-thread',
      turn:{id:'synthetic-turn',thread_id:'synthetic-thread',intent:'remember',user_text:'补充合成资料',receipt:{remember:{title:'合成资料',state:'done',insights:[],related:[]}}}}));
    return original(url,options);
  }));
  window.location.hash='view=workbench&project_id=beta';render(<AppRouter/>);
  const input=await screen.findByRole('textbox',{name:'输入'});await settle();
  fireEvent.change(input,{target:{value:'补充合成资料'}});
  fireEvent.click(screen.getByRole('button',{name:'资料库'}));await screen.findByRole('button',{name:'资料库更多'});await settle();
  const panel=await openGaps();fireEvent.click(within(panel).getByRole('button',{name:gap.text+' 3 次 · 10·05'}));
  const resumed=await screen.findByRole('textbox',{name:'输入'});await settle();
  expect(resumed).toHaveValue('补充合成资料');expect(resumed).toHaveFocus();
  expect(screen.getByText('#同名 项目/阅读')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'发送'}));await settle();
  const calls=fetch.mock.calls.filter(([url,options])=>String(url).endsWith('/turns')&&options.method==='POST');
  expect(calls).toHaveLength(1);expect(JSON.parse(calls[0][1].body)).toEqual({project_id:'beta',intent:'remember',text:'#beta/阅读 补充合成资料'});
});

it.each([true,false])('cross-project sending accepted=%s clears only the sent source draft and retains the target draft',async accepted=>{
  const original=fetch;
  vi.stubGlobal('fetch',vi.fn((url,options={})=>{
    if(String(url).endsWith('/files')&&options.method==='POST')return Promise.resolve(response({id:'uploaded-source'}));
    if(String(url).endsWith('/turns')&&options.method==='POST')return Promise.resolve(accepted
      ? response({thread_id:'sent-thread',turn:{id:'sent-turn',thread_id:'sent-thread',intent:'remember',user_text:'合成原件',receipt:{remember:{title:'合成原件',state:'done',insights:[],related:[]}}}})
      : response({detail:'remote_disabled'},400));
    return original(url,options);
  }));
  async function choose(name){fireEvent.click(screen.getByRole('button',{name:'切换项目'}));fireEvent.click(screen.getByRole('option',{name}));await settle();}
  window.location.hash='view=workbench&project_id=beta';render(<AppRouter/>);
  await screen.findByRole('textbox',{name:'输入'});await settle();
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'乙的未发送草稿'}});
  fireEvent.change(screen.getByLabelText('文件'),{target:{files:[new File(['Synthetic target'],'target.txt')]}});
  await choose('甲');
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'#乙/工作 合成原件'}});
  fireEvent.change(screen.getByLabelText('文件'),{target:{files:[new File(['Synthetic source'],'source.txt')]}});
  fireEvent.click(screen.getByRole('button',{name:'发送'}));await settle();
  const upload=fetch.mock.calls.find(([url,options])=>String(url).endsWith('/files')&&options.method==='POST');
  expect(upload[1].body.get('project_id')).toBe('beta');expect(upload[1].body.get('file').name).toBe('source.txt');
  const sent=fetch.mock.calls.filter(([url,options])=>String(url).endsWith('/turns')&&options.method==='POST');
  expect(sent).toHaveLength(1);expect(JSON.parse(sent[0][1].body)).toMatchObject({project_id:'beta',text:'#乙/工作 合成原件',intent:'remember',item_id:'uploaded-source'});
  if(accepted){
    expect(window.location.hash).toContain('project_id=beta');
    expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('乙的未发送草稿');expect(screen.getByText('target.txt')).toBeInTheDocument();
    await choose('甲');
    expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('');expect(screen.queryByText('source.txt')).not.toBeInTheDocument();
    await choose('乙');expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('乙的未发送草稿');expect(screen.getByText('target.txt')).toBeInTheDocument();
  }else{
    expect(window.location.hash).toContain('project_id=alpha');expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('#乙/工作 合成原件');expect(screen.getByText('source.txt')).toBeInTheDocument();
    await choose('乙');expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('乙的未发送草稿');expect(screen.getByText('target.txt')).toBeInTheDocument();
    await choose('甲');expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('#乙/工作 合成原件');expect(screen.getByText('source.txt')).toBeInTheDocument();
  }
});

it('keeps the source draft when project navigation invalidates an unfinished real upload',async()=>{
  const original=fetch;let finishUpload;
  vi.stubGlobal('fetch',vi.fn((url,options={})=>String(url).endsWith('/files')&&options.method==='POST'
    ? new Promise(resolve=>{finishUpload=resolve;}) : original(url,options)));
  async function choose(name){fireEvent.click(screen.getByRole('button',{name:'切换项目'}));fireEvent.click(screen.getByRole('option',{name}));await settle();}
  window.location.hash='view=workbench&project_id=beta';render(<AppRouter/>);
  await screen.findByRole('textbox',{name:'输入'});await settle();
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'目标项目原草稿'}});
  await choose('甲');fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'#乙/工作 尚未发送原件'}});
  fireEvent.change(screen.getByLabelText('文件'),{target:{files:[new File(['Synthetic unfinished'],'unfinished.txt')]}});
  fireEvent.click(screen.getByRole('button',{name:'发送'}));await settle();
  expect(fetch.mock.calls.filter(([url,options])=>String(url).endsWith('/files')&&options.method==='POST')).toHaveLength(1);
  await choose('乙');expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('目标项目原草稿');
  await act(async()=>{finishUpload(response({id:'late-upload'}));});
  expect(fetch.mock.calls.filter(([url,options])=>String(url).endsWith('/turns')&&options.method==='POST')).toHaveLength(0);
  await choose('甲');expect(screen.getByRole('textbox',{name:'输入'})).toHaveValue('#乙/工作 尚未发送原件');
  expect(screen.getByText('unfinished.txt')).toBeInTheDocument();
});
