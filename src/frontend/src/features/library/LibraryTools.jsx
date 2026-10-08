import { useEffect, useRef, useState } from 'react';
import { FocusPanel, MarkdownBody } from '../../shared/ui';
import { recognitionApi as api } from '../../shared/api/recognitionApi';
import VersionHistory from './VersionHistory';
import './LibraryTools.css';

const eligible = rows => rows.filter(row => row.kind === 'recognition' && row.state === 'active');
const lines = text => text.split('\n').map(value => value.trim()).filter(Boolean);
function useOperation() {
  const live = useRef(true);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  async function run(operation, apply) {
    if (busy) return;
    setBusy(true); setError('');
    try { const result = await operation(); if (live.current) apply?.(result); }
    catch (failure) { if (live.current) { setError(failure.status === 409 ? '内容已变化 · 重新读取' : '操作未完成 · 重试'); apply?.(null); } }
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
      <p className="library-meta">{row.updated_at ? String(row.updated_at).slice(0, 10).replaceAll('-', '·') : '—'} · 认 {Number.isInteger(row.evidence_count) && row.evidence_count >= 0 ? row.evidence_count : '—'}</p>
    </section>)}
  </FocusPanel>;
}

const erasedLabels = { recognitions:'认识',recognition_versions:'历史版本',recognition_candidates:'待确认认识',recognition_task_feedback:'任务反馈',recognition_migration_imports:'迁移记录',recognition_migration_versions:'迁移历史',recognition_graph_views:'图谱视图',recognition_restructure_proposals:'重组记录',recognition_recall_preferences:'召回偏好',recognition_experiences:'关联原件',recognition_questions:'固定问题',recognition_relations:'关系',recognition_relation_proposals:'关系记录',recognition_context_packets:'发送记录',recognition_tasks:'任务',documents:'整理稿',document_revisions:'整理稿版本',document_markdown:'整理稿正文' };
export function InsightMaintenance({ projectId, insight, insights, action, onDone }) {
  const { busy, error, run } = useOperation();
  const [parts, setParts] = useState(['','']), [ids,setIds] = useState([insight.id]), [content,setContent] = useState(''), [conditions,setConditions] = useState('');
  const [preview,setPreview] = useState(null), [ack,setAck] = useState(false);
  const scope={projectId,recognitionId:insight.id,expectedRevision:insight.revision};
  const candidates=eligible(insights), selected=candidates.filter(row=>ids.includes(row.id));
  if (action === 'versions') return <div className="library-tools"><VersionHistory projectId={projectId} recognitionId={insight.id}/></div>;
  return <div className="library-tools">
    {error && <p role="alert">{error}</p>}
    {action === 'split' && <form className="library-edit" onSubmit={event=>{event.preventDefault();run(()=>api.splitRecognition({...scope,parts}),result=>result && onDone());}}>
      {parts.map((part,index)=><label key={index}>第 {index+1} 条<textarea aria-label={`第 ${index+1} 条`} value={part} onChange={event=>setParts(old=>old.map((value,i)=>i===index?event.target.value:value))}/></label>)}
      <button type="button" disabled={parts.length>=12 || busy} onClick={()=>setParts(old=>[...old,''])}>增加一条</button>
      <button type="submit" disabled={busy || parts.some(part=>!part.trim())}>确认拆分</button>
    </form>}
    {action === 'merge' && <form className="library-edit" onSubmit={event=>{event.preventDefault();run(()=>api.mergeRecognitions({projectId,expectedRevisions:Object.fromEntries(selected.map(row=>[row.id,row.revision])),content,conditions:lines(conditions)}),result=>result && onDone());}}>
      <div className="library-choice">{candidates.map(row=><label key={row.id}><input type="checkbox" checked={ids.includes(row.id)} onChange={()=>setIds(old=>old.includes(row.id)?old.filter(id=>id!==row.id):[...old,row.id])}/>{row.text}</label>)}</div>
      <label>合并正文<textarea aria-label="合并正文" value={content} onChange={event=>setContent(event.target.value)}/></label>
      <label>补充条件<textarea aria-label="补充条件" value={conditions} onChange={event=>setConditions(event.target.value)}/></label>
      <button type="submit" disabled={busy || selected.length<2 || selected.length>12 || !content.trim()}>确认合并</button>
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
