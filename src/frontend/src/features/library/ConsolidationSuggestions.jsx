import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { useEffect, useState } from 'react';
import { Icon } from '../../shared/ui/Icon';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { libraryApi } from './libraryApi';

export function ConsolidationSuggestions({ projectId, insightId, originals = [], onOpenDocument, onReviewed, readonly = false }) {
  const [merges, setMerges] = useState([]), [supports, setSupports] = useState([]);
  const [expanded, setExpanded] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const fetchImpl = (url, options) => fetch(url, {...options, signal:controller.signal});
    Promise.all([recognitionApi.listRestructureProposals({projectId,fetchImpl}),
      libraryApi.evidenceSupport(projectId, insightId, {signal:controller.signal})]).then(([a,b]) => {
      if (controller.signal.aborted) return;
      setMerges((a.items || []).filter(r=>r.snapshot?.pending_output && r.state === 'pending' && r.target_recognition_ids.includes(insightId)));
      setSupports((b.items || []).filter(r=>r.state !== 'rejected'));
    }).catch(()=>{if(!controller.signal.aborted)setError('读取未完成 · 重试');});
    return ()=>controller.abort();
  }, [projectId, insightId]);
  async function review(row, type, accept) {
    if(readonly || busy)return;
    setBusy(true);setError('');
    try {
      if(type==='merge') {
        await recognitionApi.reviewRestructureProposal({projectId,proposalId:row.id,expectedRevision:row.revision,decision:accept?'approve':'reject'});
        setMerges(old=>old.filter(r=>r.id!==row.id));
      } else {
        await libraryApi.reviewEvidenceSupport(row.project_id || projectId,row.id,row.revision,accept);
        setSupports(old=>old.filter(r=>r.id!==row.id));
      }
      onReviewed?.();
    } catch {setError('内容已变化 · 刷新');}
    finally {setBusy(false);}
  }
  const documents = row => row.documents || [...new Map((row.snapshot?.experiences || []).flatMap(r=>r.payload.provenance?.source_refs || [])
    .filter(r=>r.type==='document').map(r=>[r.id,r])).values()];
  return <section className="library-grown library-consolidation" aria-label="整理建议">
    {error && <p role="alert">{error}</p>}
    {!!originals.length && <details><summary>原认识 {originals.length}</summary>{originals.map(r=><p key={r.id}>{r.text}</p>)}</details>}
    <div className="library-actions">{!!merges.length && <button type="button" aria-label={`合并 ${merges.length}`} aria-expanded={expanded==='merge'} onClick={()=>setExpanded(expanded==='merge'?null:'merge')}><Icon name="merge" size={16}/>{merges.length}</button>}
      {!!supports.length && <button type="button" aria-label={`支持 ${supports.length}`} aria-expanded={expanded==='support'} onClick={()=>setExpanded(expanded==='support'?null:'support')}><Icon name="supports" size={16}/>{supports.length}</button>}</div>
    {(expanded==='merge'?merges:expanded==='support'?supports:[]).map(row=><div key={row.id}>
      {expanded==='merge' ? <><ul>{[...(row.snapshot.recognitions || []),...(row.snapshot.candidates || [])].filter(r=>row.target_recognition_ids.includes(r.id)).map(r=><li key={r.id}>{r.payload.content}</li>)}</ul>
        {row.outputs.map((r,i)=><div key={i}><p>{r.content}</p>{r.conditions?.length>0 && <ul>{r.conditions.map(c=><li key={c}>{c}</li>)}</ul>}</div>)}</> : <p>{row.evidence}</p>}
      <div className="library-actions">{documents(row).map(d=><button type="button" key={d.id} aria-label={`打开整理稿 ${d.id}`} onClick={()=>onOpenDocument?.(d.id,row.project_id || projectId)}><Icon name="open" size={14}/>{d.id}</button>)}</div>
      {row.state==='pending' && !readonly && <div className="library-actions"><button type="button" disabled={busy || row.current===false} onClick={()=>review(row,expanded, true)}>{expanded==='merge'?'确认合并方案':'确认支持'}</button>
        <button type="button" disabled={busy} onClick={()=>review(row,expanded,false)}>{expanded==='merge'?'忽略合并':'忽略支持'}</button></div>}
    </div>)}
  </section>;
}
