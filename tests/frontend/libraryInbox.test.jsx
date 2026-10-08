import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';
const old = {id:'inbox-1',text:'Inbox thought',state:'pending',revision:3,source_count:1};
const projects=[{id:'alpha',name:'Alpha',scenes:['Writing','Travel']},{id:'beta',name:'Beta',scenes:['Other']}];
let suggested, filed, fail;
beforeEach(()=>{
 suggested='Writing';filed=false;fail=false;
 vi.stubGlobal('fetch',vi.fn(async (url, options={})=>{
  const path=new URL(String(url),'http://localhost');let value={items:[]},status=200;
  if(path.pathname.endsWith('/file')) {status=fail?409:200;if(!fail)filed=true;value=fail?{detail:'conflict'}:{...old,id:'new-1',state:'pending',scene:JSON.parse(options.body).scene};}
  else if(path.pathname.endsWith('/suggestions'))value={items:[{id:old.id,scene:suggested}]};
  else if(path.pathname.endsWith('/insights'))value={items:path.searchParams.get('project_id')==='inbox'?filed?[]:[old]:filed?[{...old,id:'new-1',text:'Filed thought'}]:[],counts:{pending:filed?0:1,active:0}};
  return {ok:status<400,status,json:async()=>value};
 }));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
async function inbox(){const view=render(<Library projectId="alpha" projects={projects}/>);await act(async()=>{});fireEvent.click(screen.getByRole('button',{name:'收件箱 1'}));await act(async()=>{});return view;}
it('files a suggested scene into the current project while viewing the inbox',async()=>{
 await inbox();fireEvent.click(screen.getByRole('button',{name:'归入场景：Writing'}));await act(async()=>{});
 const call=fetch.mock.calls.find(([url])=>String(url).endsWith('/inbox/insight/inbox-1/file'));
 expect(JSON.parse(call[1].body)).toEqual({target_project_id:'alpha',scene:'Writing',expected_revision:3});
 expect(screen.queryByRole('button',{name:'Inbox thought'})).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'收件箱 0'})).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'全部'}));await act(async()=>{});
 expect(screen.getByRole('button',{name:'Filed thought'})).toBeInTheDocument();
});
it('opens a scene choice when no suggestion exists and files only after choosing',async()=>{
 suggested=null;await inbox();fireEvent.click(screen.getByRole('button',{name:'选择场景：Inbox thought'}));
 expect(fetch.mock.calls.some(([url])=>String(url).endsWith('/file'))).toBe(false);
 fireEvent.change(screen.getByRole('combobox',{name:'归类场景'}),{target:{value:'Travel'}});
 fireEvent.click(screen.getByRole('button',{name:'归类'}));await act(async()=>{});
 expect(JSON.parse(fetch.mock.calls.find(([url])=>String(url).endsWith('/file'))[1].body).scene).toBe('Travel');
});
it('retains the inbox row after a filing conflict and shows an actionable error',async()=>{
 fail=true;await inbox();fireEvent.click(screen.getByRole('button',{name:'归入场景：Writing'}));await act(async()=>{});
 expect(screen.getByRole('alert')).toHaveTextContent('内容已变化');expect(screen.getByRole('button',{name:'Inbox thought'})).toBeInTheDocument();
});
it('returns to project layers from the inbox without leaving an inbox filter behind',async()=>{
 await inbox();fireEvent.click(screen.getByRole('button',{name:'原件 0'}));await act(async()=>{});
 expect(screen.getByRole('button',{name:'全部'})).toHaveAttribute('aria-pressed','true');
 const lists=fetch.mock.calls.filter(([url])=>String(url).includes('/sources?'));
 expect(String(lists.at(-1)[0])).toContain('project_id=alpha');
});
it('discards late suggestions after the project changes',async()=>{
 let finish;const normal=fetch.getMockImplementation();
 fetch.mockImplementation((url,opts)=>String(url).includes('/suggestions?')?new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>({items:[{id:old.id,scene:'Writing'}]})});}):normal(url,opts));
 const view=await inbox();view.rerender(<Library projectId="beta" projects={projects}/>);await act(async()=>{});
 await act(async()=>finish());expect(screen.queryByRole('button',{name:'归入场景：Writing'})).not.toBeInTheDocument();
});
