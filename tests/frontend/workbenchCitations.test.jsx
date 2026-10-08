import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';
const full = '🧸before first middle second after';
const cite = (layer='source', persona=false) => ({n:1,layer,persona,id:layer==='source'?'source-1':layer==='insight'?'insight-1':'doc-1',title:'Citation',quote:'first\n…\nsecond',locator:{coordinate_space:layer==='source'?'source_content_v1':'document_markdown_v1',windows:[{start:8,end:13},{start:21,end:27}]}});
let citation, fullResponse, drill;
const reply=value=>({ok:true,json:async()=>value});
beforeEach(()=>{
 localStorage.clear(); citation=cite(); fullResponse={id:'source-1',title:'Full title',text:full,coordinate_space:'source_content_v1'};
 drill={insight:{text:'Full insight'},note:{title:'Full title',markdown:full},summary:{text:'Short summary'},source:{title:'Original',window:{pre:'before ',quote:'Frozen evidence',post:' after'}}};
 vi.stubGlobal('fetch',vi.fn(async url=>String(url).includes('/library/drill?')?reply(drill):String(url).includes('/text?')?reply(fullResponse):String(url).includes('/threads/')?reply({turns:[{id:'ask-1',intent:'ask',receipt:{ask:{answer:'Answer[1]',citations:[citation],layers:{source:1},trace:[]}}}]}):reply({items:[{id:'thread-1',title:'Thread'}]})));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
async function open(){render(<Workbench projectId="alpha"/>);await act(async()=>{});const link=screen.getByRole('link',{name:/引用 1/});link.focus();fireEvent.click(link);await act(async()=>{});return screen.getByRole('dialog');}
for(const layer of ['insight','summary','note','source']) it(`opens the complete ${layer} body from its real layer contract`,async()=>{
 citation=cite(layer);const panel=await open();
 expect(panel).toHaveTextContent(layer==='insight'?'Full insight':full);
 if(layer!=='insight') expect([...panel.querySelectorAll('mark')].map(node=>node.textContent)).toEqual(['first','second']);
 if(['summary','note'].includes(layer)) expect(fetch.mock.calls.find(([url])=>String(url).includes('/drill?'))[0]).toContain('from=note');
 const link=screen.getByRole('link',{name:/引用 1/}); expect(link).toHaveAttribute('href','#workbench-citation');
 fireEvent.click(within(panel).getByRole('button',{name:'关闭'}));expect(link).toHaveFocus();
});
it('reads persona citations from me rather than the visible project',async()=>{
 citation=cite('source',true);await open();
 const reads=fetch.mock.calls.filter(([url])=>String(url).includes('/library/'));
 expect(reads).toHaveLength(2);for(const [url] of reads)expect(String(url)).toContain('project_id=me');
});
for(const mismatch of ['coordinate','quote','bounds']) it(`keeps the receipt quote above the full body on ${mismatch} mismatch`,async()=>{
 if(mismatch==='coordinate')fullResponse.coordinate_space='workspace_source_text_v1';
 if(mismatch==='quote')citation.quote='A different frozen quote';
 if(mismatch==='bounds')citation.locator.windows[1].end=999;
 const panel=await open();expect(panel.querySelectorAll('mark')).toHaveLength(0);
 expect(panel.querySelector('.workbench-citation-quote').textContent).toBe(citation.quote);
 expect(panel.querySelector('.ui-source-text').textContent).toBe(full);
});
it('reads full source text even when the drill has no selected document or source',async()=>{
 drill={source:null,note:null,documents:[{document_id:'one'},{document_id:'two'}]};
 const panel=await open();expect(panel).toHaveTextContent(full);expect(panel.querySelectorAll('mark')).toHaveLength(2);
});
it('keeps the genuine evidence snippet on full-text failure and retries the whole read',async()=>{
 fullResponse={items:[]};const panel=await open();
 expect(within(panel).getByRole('alert')).toHaveTextContent('全文未读取');expect(panel).toHaveTextContent('Frozen evidence');
 fullResponse={text:full,coordinate_space:'source_content_v1'};
 fireEvent.click(within(panel).getByRole('button',{name:'重试'}));await act(async()=>{});
 expect(panel).toHaveTextContent(full);expect(within(panel).queryByRole('alert')).not.toBeInTheDocument();
});
