import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';
import { Library } from '@src/features/library/Library';
import { SettingsData } from '@src/features/settings/SettingsData';
import { CitedAnswer } from '@src/shared/ui/CitedAnswer';
import { invalidateSignals, sendSignal, signalEpoch } from '@src/shared/signalsApi';

let requests, turn, pending, setting, mode, failSignal, finishDrill, delayedDrill;
let sequence=0;
const settle=()=>act(async()=>{});
const calls=()=>requests.filter(row=>row.path==='/api/v2/signals');
beforeEach(()=>{
 invalidateSignals(true); localStorage.clear(); sessionStorage.clear(); requests=[]; mode='desktop'; failSignal=false; delayedDrill=false;
 pending={id:`candidate-${++sequence}`,revision:2,kind:'candidate',state:'pending',text:'待确认内容',source_count:1,conditions:[],related:[],document_ids:[]};
 turn={id:`turn-${sequence}`,thread_id:'thread-1',intent:'ask',user_text:'问题',receipt:{ask:{answer:'第一段[3]，保留[9]。第二段【3】。',citations:[{n:3,layer:'source',id:'source-1',title:'原文',quote:'证据'}],layers:{source:1}}}};
 setting={enabled:true,count:5,retention_days:180,cleared_at:null,revision:1};
 vi.stubGlobal('navigator',Object.assign(Object.create(navigator),{clipboard:{writeText:vi.fn(async()=>{})}}));
 vi.stubGlobal('fetch',vi.fn(async(url,options={})=>{
  const parsed=new URL(String(url),'http://localhost'),path=parsed.pathname,body=options.body?JSON.parse(options.body):undefined;
  requests.push({path,body,options});
  let value={items:[]};
  if(path==='/api/v2/signals'){if(failSignal)throw new Error('synthetic network failure'); return {ok:true,status:204};}
  if(path==='/api/v2/settings/signals'){
   if(options.method==='PATCH') setting={...setting,enabled:body.enabled,revision:setting.revision+1};
   value=setting;
  } else if(path==='/api/v2/settings/signals/clear'){setting={...setting,count:0,revision:setting.revision+1,cleared_at:'2026-10-06T12:00:00Z'};value={cleared:5};}
  else if(path==='/api/v2/devices')value={mode,items:[]};
  else if(path.includes('/threads/'))value={id:'thread-1',turns:[turn]};
  else if(path.endsWith('/threads'))value={items:[{id:'thread-1',title:'对话'}]};
  else if(path.endsWith('/insights'))value={items:[pending],counts:{pending:1}};
  else if(path.endsWith('/drill')){
   value={insight:pending,grown:[],documents:[],sources:[],note:null,summary:null};
   if(delayedDrill)return new Promise(resolve=>{finishDrill=()=>resolve({ok:true,json:async()=>value});});
  }
  return {ok:true,status:200,json:async()=>value};
 }));
});
afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals();document.getSelection()?.removeAllRanges();});

it('copies only answer text without recognized marks and echoes check for 1.5 seconds despite signal failure',async()=>{
 failSignal=true;render(<Workbench projectId="alpha"/>);await settle();
 vi.useFakeTimers();fireEvent.click(screen.getByRole('button',{name:'复制回答'}));await settle();
 expect(navigator.clipboard.writeText).toHaveBeenCalledWith('第一段，保留[9]。第二段。');
 expect(screen.getByRole('button',{name:'复制回答'}).querySelector('[data-icon=check]')).toBeInTheDocument();
 expect(calls()).toHaveLength(1);expect(calls()[0].body).toMatchObject({kind:'copy',project_id:'alpha',turn_id:turn.id});
 expect(Object.keys(calls()[0].body).sort()).toEqual(['client_id','kind','project_id','turn_id']);
 act(()=>vi.advanceTimersByTime(1500));expect(screen.getByRole('button',{name:'复制回答'}).querySelector('[data-icon=copy]')).toBeInTheDocument();
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('copies an actual partial answer selection without superscripts and emits only one copy event',async()=>{
 render(<Workbench projectId="alpha"/>);await settle();
 const answer=document.querySelector('.ui-cited-answer'),range=document.createRange();
 range.setStart(answer.firstChild,1);range.setEnd(answer.childNodes[2],3);document.getSelection().addRange(range);
 const setData=vi.fn();fireEvent.copy(answer,{clipboardData:{setData}});fireEvent.copy(answer,{clipboardData:{setData}});await settle();
 expect(setData).toHaveBeenCalledWith('text/plain','一段，保留');expect(calls()).toHaveLength(1);
 const input=screen.getByRole('textbox');fireEvent.copy(input,{clipboardData:{setData}});await settle();expect(calls()).toHaveLength(1);
});

it('does not intercept copying with no selected answer or a selection spanning other page text',async()=>{
 const copied=vi.fn();render(<><p>外部文字</p><CitedAnswer answer="正文[3]" citations={turn.receipt.ask.citations} onCopyAnswer={copied}/></>);
 const answer=document.querySelector('.ui-cited-answer'),setData=vi.fn();fireEvent.copy(answer,{clipboardData:{setData}});
 const range=document.createRange();range.setStart(screen.getByText('外部文字').firstChild,0);range.setEnd(answer.firstChild,1);document.getSelection().addRange(range);
 fireEvent.copy(answer,{clipboardData:{setData}});expect(setData).not.toHaveBeenCalled();expect(copied).not.toHaveBeenCalled();
});

it('records view only after the Library pending focus has actually opened and does not count active rows',async()=>{
 render(<Library projectId="alpha" projects={[{id:'alpha',scenes:[]}]}/>);await settle();
 fireEvent.click(screen.getByRole('button',{name:'待确认内容'}));await settle();
 expect(screen.getByRole('dialog',{name:'认识'})).toHaveTextContent('待确认内容');
 expect(calls()).toHaveLength(1);expect(calls()[0].body).toMatchObject({kind:'view',project_id:'alpha',object:{kind:'insight',id:pending.id,revision:2}});
 fireEvent.click(screen.getByRole('button',{name:'关闭'}));pending={...pending,state:'active'};
 fireEvent.click(screen.getByRole('button',{name:'待确认内容'}));await settle();expect(calls()).toHaveLength(1);
});

it('never records a late Library drill after changing scope',async()=>{
 delayedDrill=true;const app=render(<Library projectId="alpha" projects={[{id:'alpha',scenes:[]}]}/>);await settle();
 fireEvent.click(screen.getByRole('button',{name:'待确认内容'}));await settle();
 app.rerender(<Library projectId="beta" projects={[{id:'beta',scenes:[]}]}/>);await settle();await act(async()=>finishDrill());
 expect(calls()).toHaveLength(0);expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('opens a real pending memory receipt chip without confirming it and records view from its returned identity',async()=>{
 turn={...turn,intent:'remember',receipt:{remember:{state:'done',title:'材料',progress:{},insights:[pending],related:[]}}};
 render(<Workbench projectId="alpha"/>);await settle();
 fireEvent.click(screen.getByRole('button',{name:'打开认识 待确认内容'}));await settle();
 expect(screen.getByRole('dialog',{name:'认识'})).toHaveTextContent('待确认内容');
 expect(calls()).toHaveLength(1);expect(calls()[0].body).toMatchObject({kind:'view',object:{id:pending.id,revision:2}});
 expect(requests.some(row=>row.path.endsWith('/confirm'))).toBe(false);expect(screen.getByRole('button',{name:'确认'})).toBeInTheDocument();
});

it('renders usage count and server-only tooltip then performs CAS toggle and two-step timed clear',async()=>{
 mode='server';render(<SettingsData projectId="alpha"/>);await settle();
 expect(screen.getByText('5 条')).toBeInTheDocument();expect(screen.getByTitle('保留 180 天 · 管理员可见')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('switch',{name:'使用记录'}));await settle();
 expect(screen.getByRole('switch',{name:'使用记录'})).toHaveAttribute('aria-checked','false');
 expect(requests.find(row=>row.options.method==='PATCH').body).toEqual({enabled:false,expected_revision:1});
 vi.useFakeTimers();fireEvent.click(screen.getByRole('button',{name:'清除使用记录'}));expect(screen.getByRole('button',{name:'确认清除使用记录'})).toBeInTheDocument();
 act(()=>vi.advanceTimersByTime(3000));expect(screen.getByRole('button',{name:'清除使用记录'})).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'清除使用记录'}));fireEvent.click(screen.getByRole('button',{name:'确认清除使用记录'}));await settle();
 expect(screen.getByText('0 条')).toBeInTheDocument();expect(requests.find(row=>row.path.endsWith('/clear')).body).toEqual({expected_revision:2});
});


it('does not resurrect a delayed clipboard signal after clear while preserving the copy itself',async()=>{
 let finish; navigator.clipboard.writeText.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 render(<Workbench projectId="alpha"/>);await settle();fireEvent.click(screen.getByRole('button',{name:'复制回答'}));
 invalidateSignals();await act(async()=>finish());expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);
 expect(calls()).toHaveLength(0);expect(screen.getByRole('button',{name:'复制回答'}).querySelector('[data-icon=check]')).toBeInTheDocument();
});

it('drops view after an in-flight pending drill crosses a clear epoch',async()=>{
 delayedDrill=true;render(<Library projectId="alpha" projects={[{id:'alpha',scenes:[]}]}/>);await settle();
 fireEvent.click(screen.getByRole('button',{name:'待确认内容'}));invalidateSignals();await act(async()=>finishDrill());
 expect(screen.getByRole('dialog',{name:'认识'})).toHaveTextContent('待确认内容');expect(calls()).toHaveLength(0);
});

it('never queues or retries signals and aborts prior fetch on disabling usage records',async()=>{
 const original=fetch.getMockImplementation();let signal;
 fetch.mockImplementation((url,options)=>String(url).endsWith('/api/v2/signals')?new Promise(()=>{signal=options.signal;}):original(url,options));
 const captured=signalEpoch();sendSignal({kind:'copy',project_id:'alpha',turn_id:turn.id});expect(signal.aborted).toBe(false);
 invalidateSignals(false);expect(signal.aborted).toBe(true);
 sendSignal({kind:'copy',project_id:'alpha',turn_id:'other'},captured);expect(fetch.mock.calls.filter(([url])=>String(url).endsWith('/api/v2/signals'))).toHaveLength(1);
 await settle();expect(fetch.mock.calls.filter(([url])=>String(url).endsWith('/api/v2/signals'))).toHaveLength(1);
});

it('does not count failed pending Library opens',async()=>{
 const original=fetch.getMockImplementation();fetch.mockImplementation((url,options)=>String(url).includes('/drill?')?Promise.reject(new Error('synthetic drill failed')):original(url,options));
 render(<Library projectId="alpha" projects={[{id:'alpha',scenes:[]}]}/>);await settle();
 fireEvent.click(screen.getByRole('button',{name:'待确认内容'}));await settle();expect(calls()).toHaveLength(0);expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('does not count late receipt focus responses in another project',async()=>{
 turn={...turn,intent:'remember',receipt:{remember:{state:'done',title:'材料',progress:{},insights:[pending],related:[]}}};
 delayedDrill=true;const app=render(<Workbench projectId="alpha"/>);await settle();
 fireEvent.click(screen.getByRole('button',{name:'打开认识 待确认内容'}));app.rerender(<Workbench projectId="beta"/>);await settle();await act(async()=>finishDrill());
 expect(calls()).toHaveLength(0);expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('does not count an active object returned to a pending receipt opener',async()=>{
 turn={...turn,intent:'remember',receipt:{remember:{state:'done',title:'材料',progress:{},insights:[pending],related:[]}}};
 render(<Workbench projectId="alpha"/>);await settle();pending={...pending,state:'active'};
 fireEvent.click(screen.getByRole('button',{name:'打开认识 待确认内容'}));await settle();expect(calls()).toHaveLength(0);
 expect(screen.getByRole('dialog',{name:'认识'})).toBeInTheDocument();
});

it('never records a failed receipt focus and keeps original confirmation actions available',async()=>{
 turn={...turn,intent:'remember',receipt:{remember:{state:'done',title:'材料',progress:{},insights:[pending],related:[]}}};
 const original=fetch.getMockImplementation();fetch.mockImplementation((url,options)=>String(url).includes('/drill?')?Promise.reject(new Error('synthetic drill failed')):original(url,options));
 render(<Workbench projectId="alpha"/>);await settle();fireEvent.click(screen.getByRole('button',{name:'打开认识 待确认内容'}));await settle();
 expect(calls()).toHaveLength(0);expect(screen.getByRole('button',{name:'确认'})).toBeInTheDocument();expect(screen.getByRole('button',{name:'丢弃'})).toBeInTheDocument();
});


it('restores failed optimistic closing after settings unmount without suppressing later copies',async()=>{
 const original=fetch.getMockImplementation();let rejectPatch;
 fetch.mockImplementation((url,options={})=>String(url).endsWith('/settings/signals')&&options.method==='PATCH'
  ?new Promise((resolve,reject)=>{rejectPatch=reject;}):original(url,options));
 const settings=render(<SettingsData projectId="alpha"/>);await settle();fireEvent.click(screen.getByRole('switch',{name:'使用记录'}));
 settings.unmount();await act(async()=>rejectPatch(new Error('synthetic patch failure')));
 render(<Workbench projectId="alpha"/>);await settle();fireEvent.click(screen.getByRole('button',{name:'复制回答'}));await settle();
 expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);expect(setting.enabled).toBe(true);expect(calls()).toHaveLength(1);
});


it('does not let stale failed closing override a later epoch decision',async()=>{
 const original=fetch.getMockImplementation();let rejectPatch;
 fetch.mockImplementation((url,options={})=>String(url).endsWith('/settings/signals')&&options.method==='PATCH'
  ?new Promise((resolve,reject)=>{rejectPatch=reject;}):original(url,options));
 const settings=render(<SettingsData projectId="alpha"/>);await settle();fireEvent.click(screen.getByRole('switch',{name:'使用记录'}));settings.unmount();
 invalidateSignals(false);await act(async()=>rejectPatch(new Error('synthetic patch failure')));
 sendSignal({kind:'copy',project_id:'alpha',turn_id:turn.id});expect(calls()).toHaveLength(0);
});

it('retries an actual signals read failure in the same button slot and restores count and switch',async()=>{
 const original=fetch.getMockImplementation();let failed=true;
 fetch.mockImplementation((url,options={})=>String(url).endsWith('/settings/signals')&&failed?Promise.reject(new Error('synthetic read failed')):original(url,options));
 render(<SettingsData projectId="alpha"/>);await settle();
 expect(screen.getByRole('button',{name:'重试读取使用记录'})).toHaveAttribute('title','signal_settings_failed');
 expect(screen.queryByRole('button',{name:'清除使用记录'})).not.toBeInTheDocument();expect(screen.getByRole('switch',{name:'使用记录'})).toBeDisabled();
 failed=false;fireEvent.click(screen.getByRole('button',{name:'重试读取使用记录'}));await settle();
 expect(screen.getByText('5 条')).toBeInTheDocument();expect(screen.getByRole('switch',{name:'使用记录'})).toHaveAttribute('aria-checked','true');
 expect(screen.getByRole('button',{name:'清除使用记录'})).toBeInTheDocument();
});
