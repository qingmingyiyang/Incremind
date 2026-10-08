import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react';
import { afterEach,expect,it,vi } from 'vitest';
import { InsightLinks } from '@src/features/library/InsightLinks';

afterEach(()=>{cleanup();vi.unstubAllGlobals();});
it('shows typed counts and reviews the actual proposal revision without manual creation',async()=>{
 let pending=true;
 vi.stubGlobal('fetch',vi.fn(async(url,options={})=>{
  const path=new URL(String(url),'http://localhost');
  let value={};
  if(path.pathname.endsWith('/links')) value={links:[{id:'proposal-one',other_id:'recognition-two',kind:'supports',state:pending?'suggested':'active',score:null}]};
  if(path.pathname.endsWith('/relation-proposals')) value={proposals:[{id:'proposal-one',revision:7,evidence:'两条材料支持同一结论'}]};
  if(path.pathname.endsWith('/accept')) pending=false;
  return {ok:true,json:async()=>value};
 }));
 render(<InsightLinks projectId="alpha" insightId="recognition-one" insights={[{id:'recognition-two',text:'邻居认识'}]}/>);
 fireEvent.click(await screen.findByRole('button',{name:'支持 1'}));
 expect(await screen.findByText('邻居认识')).toBeTruthy();
 await waitFor(()=>expect(screen.getByRole('button',{name:'确认连接'}).disabled).toBe(false));
 fireEvent.click(screen.getByRole('button',{name:'确认连接'}));
 await waitFor(()=>expect(screen.queryByRole('button',{name:'确认连接'})).toBeNull());
 const call=fetch.mock.calls.find(([url])=>String(url).endsWith('/accept'));
 expect(JSON.parse(call[1].body)).toEqual({project_id:'alpha',expected_revision:7});
 expect(screen.queryByText('手动连接')).toBeNull();
});
it('ignores a suggestion and handles review failures without losing it',async()=>{
 let failed=true;
 vi.stubGlobal('fetch',vi.fn(async(url)=>{
  const path=new URL(String(url),'http://localhost');
  if(path.pathname.endsWith('/dismiss')) return {ok:!failed,status:409,json:async()=>({})};
  if(path.pathname.endsWith('/relation-proposals')) return {ok:true,json:async()=>({proposals:[{id:'p',revision:3}]})};
  return {ok:true,json:async()=>({links:[{id:'p',other_id:'b',kind:'refutes',state:'suggested',score:null}]})};
 }));
 render(<InsightLinks projectId="alpha" insightId="a" insights={[{id:'b',text:'另一认识'}]}/>);
 fireEvent.click(await screen.findByRole('button',{name:'矛盾 1'}));
 await waitFor(()=>expect(screen.getByRole('button',{name:'忽略连接'}).disabled).toBe(false));
 fireEvent.click(screen.getByRole('button',{name:'忽略连接'}));
 expect(await screen.findByText('内容已变化 · 刷新')).toBeTruthy();
 expect(screen.getByRole('button',{name:'忽略连接'})).toBeTruthy();
});

for (const neighborProject of ['alpha', 'me']) {
 it(`loads an omitted ${neighborProject} neighbor without probing an incorrect drill scope`, async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
   const path = new URL(String(url), 'http://localhost');
   let value = {};
   if (path.pathname.endsWith('/links')) value = {links: [{id:'related-one', other_id:'neighbor', kind:'related', state:'active', score:0.8}]};
   else if (path.pathname.endsWith('/insights')) value = {items: path.searchParams.get('project_id') === neighborProject ? [{id:'neighbor', text:'范围正确的邻居'}] : []};
   else return {ok:false, status:404, json:async()=>({})};
   return {ok:true, json:async()=>value};
  }));
  render(<InsightLinks projectId="alpha" insightId="anchor" insights={[]}/>);
  fireEvent.click(await screen.findByRole('button', {name:'相关 1'}));
  expect(await screen.findByText('范围正确的邻居')).toBeTruthy();
  const requests = fetch.mock.calls.map(([url]) => new URL(String(url), 'http://localhost'));
  expect(requests.some(path => path.pathname.endsWith('/drill'))).toBe(false);
  expect(requests.filter(path => path.pathname.endsWith('/insights')).every(path => path.searchParams.get('q') === '')).toBe(true);
  expect(screen.queryByRole('alert')).toBeNull();
 });
}
