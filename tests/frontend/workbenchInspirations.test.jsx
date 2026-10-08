import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';
import { LayerBadges } from '@src/shared/ui/LayerBadges';
import { LadderTrace } from '@src/shared/ui/LadderTrace';
import { ContextPanel } from '@src/features/workbench/ContextPanel';

const text='🧸before first middle second after';
let citation, result, read;
const reply=(value,ok=true)=>({ok,status:ok?200:404,json:async()=>value});
beforeEach(()=>{
  localStorage.clear();
  citation={n:4,layer:'inspiration',persona:false,id:'experience-1',title:'你的灵感',quote:'first\n…\nsecond',locator:{coordinate_space:'recognition_experience_content_v1',windows:[{start:8,end:13},{start:21,end:27}]}};
  result={insight:null,grown:[],summary:null,note:null,source:null,documents:[],sources:[],readonly:true,source_project_id:'inbox',inspiration:{id:citation.id,title:'你的灵感',text,revision:1,coordinate_space:'recognition_experience_content_v1'}};
  read=()=>Promise.resolve(reply(result));
  vi.stubGlobal('fetch',vi.fn((url)=>String(url).includes('/library/drill?')?read():String(url).includes('/threads/')?Promise.resolve(reply({turns:[{id:'ask-1',intent:'ask',receipt:{ask:{answer:'合成回答[4]',citations:[citation],layers:{insight:1,inspiration:2},trace:[{layer:'insight',selected:1,stopped:true}]}}}]})):Promise.resolve(reply({items:[{id:'thread-1',title:'合成对话'}]}))));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
async function mount(){const view=render(<Workbench projectId="alpha"/>);await act(async()=>{});return view;}
async function open(){await mount();const link=screen.getByRole('link',{name:'引用 4 · 灵感 · 你的灵感'});link.focus();fireEvent.click(link);await act(async()=>{});return screen.getByRole('dialog',{name:'灵感'});}

it('shows actual inspiration counts and omits zero without altering existing layers',()=>{
 const view=render(<LayerBadges layers={{insight:1,inspiration:2,persona:1}} onOpenTrace={()=>{}}/>);
 expect(screen.getByText('灵 2')).toBeInTheDocument();expect(screen.getByText('认 1')).toBeInTheDocument();expect(screen.getByText('我 1')).toBeInTheDocument();
 view.rerender(<LayerBadges layers={{inspiration:0}}/>);expect(screen.queryByText('灵 0')).not.toBeInTheDocument();
});
it('places inspiration beside insight without creating a stopping layer and keeps sent count',()=>{
 render(<LadderTrace layers={{inspiration:2}} trace={[{layer:'insight',selected:1,stopped:true}]} citations={[citation]}/>);
 expect(screen.getByRole('group',{name:'灵感'})).toHaveTextContent('灵2');expect(screen.getAllByLabelText('已足够')).toHaveLength(1);expect(screen.getAllByRole('listitem')).toHaveLength(5);
});
it('labels actual inspiration budget and uncited sent entries for ask and do',()=>{
 const receipt={context:{parts:[{key:'inspiration',count:2,tokens:37}],entries:[{...citation,inspiration:true,tokens:19}]},citations:[]};
 const view=render(<ContextPanel receipt={receipt} kind="ask"/>);
 expect(screen.getByLabelText('分类')).toHaveTextContent('灵感237');fireEvent.click(screen.getByText('条目'));expect(screen.getByLabelText('条目')).toHaveTextContent('灵感');
 view.rerender(<ContextPanel receipt={receipt} kind="do"/>);expect(screen.getByLabelText('分类')).toHaveTextContent('灵感237');
});
it.each(['inbox','alpha'])('opens %s owned original through current project without candidate actions',async owner=>{
 result.source_project_id=owner;if(owner==='alpha'){citation.id='experience-copy-v2-1';result.inspiration.id=citation.id;}
 const panel=await open();expect(panel.querySelector('.ui-source-text').textContent).toBe(text);expect([...panel.querySelectorAll('mark')].map(node=>node.textContent)).toEqual(['first','second']);
 const calls=fetch.mock.calls.filter(([url])=>String(url).includes('/library/'));expect(calls).toHaveLength(1);const url=new URL(calls[0][0],'http://local');expect(url.searchParams.get('project_id')).toBe('alpha');expect(url.searchParams.get('from')).toBe('inspiration');expect(url.searchParams.get('id')).toBe(citation.id);
 expect(within(panel).queryByRole('button',{name:/确认|编辑|丢弃/})).not.toBeInTheDocument();const link=screen.getByRole('link',{name:/引用 4/});fireEvent.click(within(panel).getByRole('button',{name:'关闭'}));expect(link).toHaveFocus();
});
it.each(['coordinate','quote','bounds'])('preserves frozen quote without replacement marks on %s mismatch',async mismatch=>{
 if(mismatch==='coordinate')result.inspiration.coordinate_space='other';if(mismatch==='quote')citation.quote='合成不匹配引用';if(mismatch==='bounds')citation.locator.windows[1].end=999;
 const panel=await open();expect(panel.querySelectorAll('mark')).toHaveLength(0);expect(panel.querySelector('blockquote').textContent).toBe(citation.quote);expect(panel.querySelector('.ui-source-text').textContent).toBe(text);
});
it('retries failed original read without writing usage or candidate state',async()=>{
 read=()=>Promise.resolve(reply({detail:'library_item_not_found'},false));const panel=await open();expect(within(panel).getByRole('alert')).toHaveTextContent('读取未完成');
 read=()=>Promise.resolve(reply(result));fireEvent.click(within(panel).getByRole('button',{name:'重试'}));await act(async()=>{});expect(panel).toHaveTextContent(text);expect(fetch.mock.calls.every(([,options])=>!options?.method||options.method==='GET')).toBe(true);
});
it.each(['close','project'])('discards a late inspiration read after %s',async mode=>{
 let finish;read=()=>new Promise(resolve=>{finish=resolve;});const view=await mount();fireEvent.click(screen.getByRole('link',{name:/引用 4/}));
 if(mode==='close')fireEvent.click(within(screen.getByRole('dialog')).getByRole('button',{name:'关闭'}));else view.rerender(<Workbench projectId="beta"/>);
 await act(async()=>{finish(reply(result));});expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(screen.queryByText(text)).not.toBeInTheDocument();
});
