import { useRef, useState } from 'react';
import { FocusPanel, ProjectSwitcher } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';
import './Library.css';

export function DocumentPlacement({ projectId, projects, row, readonly=false, onChanged, onError }) {
  const hint=row.placement, filing=row.filing, current=projects.find(project=>project.id===projectId);
  const [choosing,setChoosing]=useState(false), [target,setTarget]=useState(projectId);
  const [scene,setScene]=useState(hint?.current_scene || row.scene || ''), [busy,setBusy]=useState(false);
  const gate=useRequestScope(`${projectId}:${row.document_id}:${row.revision}`), pending=useRef(false);
  const name=id=>projects.find(project=>project.id===id)?.name || id;
  async function mutate(operation) {
    if(pending.current) return;
    const request=gate.issue('placement',gate.scope);
    if(!request) return;
    pending.current=true;setBusy(true);
    try {
      const result=await operation();
      if(gate.isCurrent(request)){setChoosing(false);onChanged?.(result);}
    } catch(error){if(gate.isCurrent(request)) onError?.(error.message);}
    finally{if(gate.isCurrent(request)){pending.current=false;setBusy(false);}}
  }
  const move=(project,destination)=>mutate(()=>libraryApi.fileDocument(projectId,row,project,destination || null));
  if(readonly || !row.document_id || !Number.isInteger(row.revision)) return null;
  if(filing) return <div className="library-actions"><span>已移到 #{name(filing.target_project_id)}</span>{filing.state==='filed' && <button type="button" aria-label="撤销移动" disabled={busy} onClick={()=>mutate(()=>libraryApi.unfileDocument(filing.target_project_id,filing.filing_document_id,filing.filing_revision))}>撤销</button>}</div>;
  const label=`#${name(projectId)}${hint?.current_scene || row.scene ? '/'+(hint?.current_scene || row.scene) : ''}`;
  const choices=projects.filter(project=>!['me','inbox'].includes(project.id) && (!current?.private || project.id===projectId));
  const destination=choices.find(project=>project.id===target);
  return <div className="library-actions">
    <button type="button" disabled={busy} aria-label={`归属 ${label}`} onClick={()=>setChoosing(true)}>{label}</button>
    {!current?.private && hint?.project_id && hint.project_id!==projectId && choices.some(project=>project.id===hint.project_id) && <button type="button" disabled={busy} aria-label={`移到 #${name(hint.project_id)}${hint.scene?'/'+hint.scene:''}`} onClick={()=>move(hint.project_id,hint.scene)}>→ #{name(hint.project_id)}{hint.scene?'/'+hint.scene:''}</button>}
    {choosing && <FocusPanel title="归属" onClose={()=>{if(!busy)setChoosing(false);}}>
      <ProjectSwitcher projects={choices} value={target} onChange={value=>{if(!busy){setTarget(value);setScene('');}}}/>
      <label>场景<select aria-label="归属场景" disabled={busy} value={scene} onChange={event=>setScene(event.target.value)}><option value="">不设场景</option>{(destination?.scenes || []).map(value=><option key={value} value={value}>{value}</option>)}</select></label>
      <button type="button" disabled={busy || !destination || target===projectId && !scene} onClick={()=>target===projectId
        ? mutate(()=>libraryApi.documentScene(projectId,row,scene,hint?.assignment_revision || row.assignment_revision || 0))
        : move(target,scene)}>应用</button>
    </FocusPanel>}
  </div>;
}
