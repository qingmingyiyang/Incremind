import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DocumentPlacement } from '@src/features/library/DocumentPlacement';
import { InboxProjectSuggestion } from '@src/features/library/InboxProjectSuggestion';
import { Workbench } from '@src/features/workbench/Workbench';
import { Library } from '@src/features/library/Library';

const projects = [{id:'alpha',name:'甲',scenes:['阅读']},{id:'beta',name:'乙',scenes:['简历']},{id:'me',name:'我'}];
const row = {document_id:'document-1',revision:3,placement:{project_id:'beta',scene:'简历',assignment_revision:0}};
const settle = () => act(async()=>{});
beforeEach(()=>{
  localStorage.clear();
  vi.stubGlobal('fetch',vi.fn(async url=>({ok:true,json:async()=>String(url).endsWith('/api/v2/projects')
    ? {id:'new-project',name:'新主题',scenes:[]} : String(url).endsWith('/turns')
      ? {thread_id:'thread-1',turn:{id:'turn-1',intent:'inspiration',user_text:'新材料',receipt:{inspiration:{insight:{id:'i1',text:'新材料',state:'pending',revision:1}}}}}
      : {items:[]}})));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
it('moves only on explicit choice and undoes by the real filing CAS identity',async()=>{
  const changed=vi.fn(), error=vi.fn();
  const view=render(<DocumentPlacement projectId="alpha" projects={projects} row={row} onChanged={changed} onError={error}/>);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'移到 #乙/简历'}));await settle();
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({project_id:'alpha',target_project_id:'beta',scene:'简历',expected_revision:3});
  expect(changed).toHaveBeenCalledTimes(1);
  view.rerender(<DocumentPlacement projectId="alpha" projects={projects} row={{...row,filing:{state:'filed',target_project_id:'beta',filing_document_id:'copy-1',filing_revision:2}}} onChanged={changed} onError={error}/>);
  expect(screen.getByText('已移到 #乙')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'撤销移动'}));await settle();
  expect(String(fetch.mock.calls.at(-1)[0])).toContain('/notes/copy-1/unfile');
  expect(JSON.parse(fetch.mock.calls.at(-1)[1].body)).toEqual({project_id:'beta',expected_revision:2});
});
it('uses the shared selection panel and exact scene revision',async()=>{
  render(<DocumentPlacement projectId="alpha" projects={projects} row={{...row,placement:{project_id:'alpha',scene:'阅读',current_scene:'阅读',assignment_revision:4}}} onChanged={()=>{}} onError={()=>{}}/>);
  fireEvent.click(screen.getByRole('button',{name:'归属 #甲/阅读'}));
  expect(screen.getByRole('dialog',{name:'归属'})).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'应用'}));await settle();
  expect(fetch.mock.calls[0][1].method).toBe('PATCH');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({project_id:'alpha',scene:'阅读',expected_revision:3,assignment_revision:4});
});
it('does not offer cross-project writes for private or readonly material',async()=>{
  const view=render(<DocumentPlacement projectId="alpha" projects={[{...projects[0],private:true},projects[1]]} row={row} onChanged={()=>{}} onError={()=>{}}/>);
  expect(screen.queryByRole('button',{name:'移到 #乙/简历'})).not.toBeInTheDocument();
  view.rerender(<DocumentPlacement projectId="alpha" projects={projects} row={row} readonly onChanged={()=>{}} onError={()=>{}}/>);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();expect(fetch).not.toHaveBeenCalled();
});
it('retains a new project and reports partial filing then retries only the remainder',async()=>{
  let failed=false;
  const normal=fetch.getMockImplementation();
  fetch.mockImplementation(async(url,options)=>{
    if(String(url).includes('/inbox/insight/b/file')&&!failed){failed=true;return {ok:false,status:409,json:async()=>({detail:'conflict'})};}
    return normal(url,options);
  });
  const rows=['a','b','c'].map(id=>({id,revision:1,text:'咖啡',kind:'candidate',state:'pending'}));
  render(<InboxProjectSuggestion group={{name:'咖啡',ids:['a','b','c'],count:3}} rows={rows} scope="alpha" onChanged={()=>{}} onProjectCreated={()=>{}}/>);
  fireEvent.click(screen.getByRole('button',{name:'新建项目 咖啡 3'}));
  fireEvent.change(screen.getByRole('textbox',{name:'项目名称'}),{target:{value:'新主题'}});
  fireEvent.click(screen.getByRole('button',{name:'归档'}));await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('已归档 1/3');
  fireEvent.click(screen.getByRole('button',{name:'重试归档'}));await settle();
  expect(fetch.mock.calls.filter(([url])=>String(url).endsWith('/api/v2/projects'))).toHaveLength(1);
  expect(fetch.mock.calls.filter(([url])=>String(url).includes('/inbox/insight/a/file'))).toHaveLength(1);
  expect(fetch.mock.calls.filter(([url])=>String(url).includes('/inbox/insight/c/file'))).toHaveLength(1);
});
it('creates an unknown tagged project and resends the retained input',async()=>{
  const navigate=vi.fn(), created=vi.fn();
  render(<Workbench projectId="alpha" projects={projects} onNavigate={navigate} onProjectCreated={created}/>);await settle();
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'#新主题 新材料'}});
  fireEvent.click(screen.getByRole('button',{name:'发送'}));await settle();
  expect(fetch.mock.calls.some(([url])=>String(url).endsWith('/turns'))).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'新建 #新主题'}));await settle();
  const submission=fetch.mock.calls.find(([url])=>String(url).endsWith('/turns'));
  expect(JSON.parse(submission[1].body)).toMatchObject({project_id:'new-project',text:'#新主题 新材料'});
  expect(created).toHaveBeenCalledWith({id:'new-project',name:'新主题',scenes:[]});
  expect(navigate).toHaveBeenCalledWith('workbench',{project_id:'new-project',thread_id:'thread-1'});
});
it('does not resend a retained unknown tag after changing projects during creation',async()=>{
  let finish;const normal=fetch.getMockImplementation();
  fetch.mockImplementation((url,options)=>String(url).endsWith('/api/v2/projects')
    ? new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>({id:'new-project',name:'新主题'})});}) : normal(url,options));
  const created=vi.fn(), view=render(<Workbench projectId="alpha" projects={projects} onProjectCreated={created}/>);await settle();
  fireEvent.change(screen.getByRole('textbox',{name:'输入'}),{target:{value:'#新主题 新材料'}});
  fireEvent.click(screen.getByRole('button',{name:'发送'}));await settle();
  fireEvent.click(screen.getByRole('button',{name:'新建 #新主题'}));await settle();
  view.rerender(<Workbench projectId="beta" projects={projects} onProjectCreated={created}/>);await settle();
  await act(async()=>finish());
  expect(created).not.toHaveBeenCalled();expect(fetch.mock.calls.some(([url])=>String(url).endsWith('/turns'))).toBe(false);
});
it('renders the real memory receipt placement action and refreshes after a move',async()=>{
  let moved=false;
  const memory={state:'done',title:'材料',document_id:row.document_id,document_revision:3,
    progress:{done:4,total:4},insights:[],related:[],placement:row.placement};
  const turn={id:'t1',intent:'remember',receipt:{remember:memory}};
  fetch.mockImplementation(async(url)=>({ok:true,json:async()=>String(url).endsWith('/file')
    ? (moved=true,{}) : String(url).includes('/threads/t1?')
      ? {turns:[{...turn,receipt:{remember:{...memory,...(moved?{filing:{state:'filed',target_project_id:'beta',filing_document_id:'copy-1',filing_revision:1}}:{})}}}]}
      : {items:[{id:'t1',title:'材料'}]}}));
  render(<Workbench projectId="alpha" projects={projects}/>);await settle();
  fireEvent.click(screen.getByRole('button',{name:'移到 #乙/简历'}));await settle();
  expect(screen.getByText('已移到 #乙')).toBeInTheDocument();
  expect(JSON.parse(fetch.mock.calls.find(([url])=>String(url).endsWith('/file'))[1].body).expected_revision).toBe(3);
});
it('offers the three-note project suggestion from the actual library inbox',async()=>{
  const rows=['a','b','c'].map(id=>({id,text:'咖啡 '+id,revision:1,kind:'candidate',state:'pending',source_count:1}));
  fetch.mockImplementation(async(url)=>({ok:true,json:async()=>String(url).endsWith('/project-suggestions')
    ? {items:[{name:'咖啡',count:3,ids:['a','b','c']}]} : String(url).endsWith('/api/v2/projects')
      ? {id:'coffee',name:'咖啡',scenes:[]} : String(url).includes('/insights?')
        ? {items:rows,counts:{pending:3}} : {items:[]}}));
  const created=vi.fn();render(<Library projectId="inbox" projects={projects} onProjectCreated={created}/>);await settle();
  fireEvent.click(screen.getByRole('button',{name:'新建项目 咖啡 3'}));
  fireEvent.click(screen.getByRole('button',{name:'归档'}));await settle();
  expect(created).toHaveBeenCalledWith({id:'coffee',name:'咖啡',scenes:[]});
  expect(fetch.mock.calls.filter(([url])=>String(url).endsWith('/file'))).toHaveLength(3);
});
it('shows document placement in the library and keeps retained originals readonly',async()=>{
  const note={...row,title:'材料',markdown:'整理正文',facts:[],verified:true};
  fetch.mockImplementation(async(url)=>({ok:true,json:async()=>String(url).includes('/notes?')
    ? {items:[note]} : String(url).includes('/drill?') ? {note,source:{id:'source-1',title:'原文',kind:'text',window:null},source_project_id:'alpha',source_readonly:true}
      : String(url).includes('/original?') ? {text:'原文内容'} : {items:[]}}));
  render(<Library projectId="beta" projects={projects} initialLayer="note"/>);await settle();
  fireEvent.click(screen.getByRole('button',{name:'材料'}));await settle();
  expect(screen.getByRole('button',{name:'归属 #乙'})).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'下一层'}));await settle();
  expect(screen.queryByRole('button',{name:/私密/})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:/归属/})).not.toBeInTheDocument();
});
