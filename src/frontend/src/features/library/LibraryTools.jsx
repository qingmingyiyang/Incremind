import { useEffect, useRef, useState } from 'react';
import { FocusPanel, MarkdownBody } from '../../shared/ui';
import { recognitionApi as api } from '../../shared/api/recognitionApi';
import { formatLocalDate } from '../../shared/lib/time';
import VersionHistory from './VersionHistory';
import './LibraryTools.css';

const lines = text => text.split('\n').map(value => value.trim()).filter(Boolean);
function useOperation() {
  const live = useRef(true);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  async function run(operation, apply, onFailure) {
    if (busy) return;
    setBusy(true); setError('');
    try { const result = await operation(); if (live.current) apply?.(result); }
    catch (failure) { if (live.current) { setError(failure.status === 409 ? '内容已变化 · 重新读取' : '操作未完成 · 重试'); onFailure?.(failure); apply?.(null); } }
    finally { if (live.current) setBusy(false); }
  }
  return { busy, error, run };
}

export function MemoPanel({ projectId, onClose }) {
  const [items, setItems] = useState([]);
  const { error, run } = useOperation();
  useEffect(() => {
    let current = true;
    run(() => api.loadWorkbench({ projectId }), result => {
      if (current && result) setItems(result.mental_models || []);
    });
    return () => { current = false; };
  }, [projectId]);
  return <FocusPanel className="library-focus library-tools" title="备忘" onClose={onClose}>
    {error && <p role="alert">{error}</p>}
    {items.map(row => <section key={row.id} className="library-question"><h3>{row.question}</h3>
      <MarkdownBody>{row.answer}</MarkdownBody>
      <p className="library-meta">{formatLocalDate(row.updated_at)} · 认 {Number.isInteger(row.evidence_count) && row.evidence_count >= 0 ? row.evidence_count : '—'}</p>
    </section>)}
  </FocusPanel>;
}

const erasedLabels = { recognitions:'认识',recognition_versions:'历史版本',recognition_candidates:'待确认认识',recognition_task_feedback:'任务反馈',recognition_migration_imports:'迁移记录',recognition_migration_versions:'迁移历史',recognition_graph_views:'图谱视图',recognition_restructure_proposals:'重组记录',recognition_recall_preferences:'召回偏好',recognition_experiences:'关联原件',recognition_questions:'固定问题',recognition_relations:'关系',recognition_relation_proposals:'关系记录',recognition_context_packets:'发送记录',recognition_tasks:'任务',documents:'整理稿',document_revisions:'整理稿版本',document_markdown:'整理稿正文' };
export function InsightMaintenance({ projectId, insight, insights, action, onReload, onDone }) {
  const { busy, error, run } = useOperation();
  const [parts, setParts] = useState(['','']), [ids,setIds] = useState([insight.id]), [content,setContent] = useState(''), [conditions,setConditions] = useState('');
  const [preview,setPreview] = useState(null), [ack,setAck] = useState(false);
  const [choices, setChoices] = useState(()=>[...insights.filter(row=>row.id!==insight.id),insight]), [needsRead, setNeedsRead] = useState(false);
  const [reloaded, setReloaded] = useState(false), [notice, setNotice] = useState('');
  const draft = useRef(null);
  draft.current = { parts, ids, content, conditions };
  const scope={projectId,recognitionId:insight.id,expectedRevision:insight.revision};
  const candidates=choices.filter(row=>row.kind==='recognition' && (row.state==='active' || ids.includes(row.id)));
  const selected=ids.map(id=>choices.find(row=>row.id===id));
  const writable=insight.state==='active' && !needsRead && !busy;
  const canSplit=writable && parts.every(part=>part.trim());
  const canMerge=writable && ids.length>=2 && ids.length<=12 && content.trim()
    && selected.every(row=>row?.kind==='recognition' && row.state==='active');
  function maintain(operation) {
    const submitted=draft.current;
    run(operation,result=>{
      if (!result) return;
      const current=draft.current;
      if (current.parts===submitted.parts && current.ids===submitted.ids && current.content===submitted.content && current.conditions===submitted.conditions) onDone();
      else { setNeedsRead(true); setNotice('已保存 · 重新读取'); }
    },failure=>{if(failure.status===409)setNeedsRead(true);});
  }
  function reload() {
    run(onReload,result=>{
      if (!result) return;
      // 重读只替换权威数据，保留等待期间继续修改的草稿和选择。
      setChoices(current=>[...result.visibleItems,...draft.current.ids.filter(id=>!result.visibleItems.some(item=>item.id===id))
        .map(id=>result.items.find(item=>item.id===id) || {...current.find(row=>row.id===id),id,kind:'recognition',state:null,
          text:current.find(row=>row.id===id)?.text || '认识'})]);
      setNeedsRead(false); setReloaded(true); setNotice('');
    });
  }
  const stateLabel={active:'生效',superseded:'已取代',revoked:'已撤销',forgotten:'已遗忘',stale:'需复核'}[insight.state] || '当前不可用';
  if (action === 'versions') return <div className="library-tools"><VersionHistory projectId={projectId} recognitionId={insight.id}/></div>;
  return <div className="library-tools">
    {error && <p role="alert">{error}</p>}
    {notice && <p>{notice}</p>}
    {(needsRead || reloaded) && <button type="button" disabled={busy} onClick={reload}>重新读取</button>}
    {reloaded && <p className="library-meta">修订 {insight.revision} · {stateLabel}</p>}
    {action === 'split' && <form className="library-edit" onSubmit={event=>{event.preventDefault();if(canSplit)maintain(()=>api.splitRecognition({...scope,parts}));}}>
      {parts.map((part,index)=><label key={index}>第 {index+1} 条<textarea aria-label={`第 ${index+1} 条`} value={part} onChange={event=>setParts(old=>old.map((value,i)=>i===index?event.target.value:value))}/></label>)}
      <button type="button" disabled={parts.length>=12 || busy} onClick={()=>setParts(old=>[...old,''])}>增加一条</button>
      <button type="submit" disabled={!canSplit}>确认拆分</button>
    </form>}
    {action === 'merge' && <form className="library-edit" onSubmit={event=>{event.preventDefault();if(canMerge)maintain(()=>api.mergeRecognitions({projectId,expectedRevisions:Object.fromEntries(selected.map(row=>[row.id,row.revision])),content,conditions:lines(conditions)}));}}>
      <div className="library-choice">{candidates.map(row=><label key={row.id}><input type="checkbox" checked={ids.includes(row.id)} onChange={()=>setIds(old=>old.includes(row.id)?old.filter(id=>id!==row.id):[...old,row.id])}/>{row.text}{row.state!=='active' && ' · 当前不可用'}</label>)}</div>
      <label>合并正文<textarea aria-label="合并正文" value={content} onChange={event=>setContent(event.target.value)}/></label>
      <label>补充条件<textarea aria-label="补充条件" value={conditions} onChange={event=>setConditions(event.target.value)}/></label>
      <button type="submit" disabled={!canMerge}>确认合并</button>
    </form>}
    {action === 'erase' && <>
      <p>永久删除当前库中的认识及派生记录，无法恢复。原件保留；同批迁移记录可能一并删除。备份和外部副本需另行处理。</p>
      <button type="button" disabled={busy} onClick={()=>{setPreview(null);setAck(false);run(()=>api.previewErasure(scope),setPreview);}}>预览删除范围</button>
      {preview && <><ul>{Object.entries(preview.counts).map(([key,count])=><li key={key}>{erasedLabels[key] || '关联记录'}：{count}</li>)}</ul>
        <label className="library-ack"><input type="checkbox" aria-label="确认永久删除" checked={ack} onChange={event=>setAck(event.target.checked)}/>确认永久删除</label>
        <button type="button" disabled={busy || !ack} onClick={()=>run(()=>api.eraseRecognition({...scope,previewId:preview.preview_id}),result=>{setPreview(null);setAck(false);if(result)onDone();})}>永久删除</button></>}
    </>}
  </div>;
}
