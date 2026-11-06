import { useEffect, useRef, useState } from 'react';
import { FocusPanel, ProjectSwitcher } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { projectScopeKind } from '../../shared/lib/projectScope';
import { sharedProjectsApi } from '../shared-projects/sharedProjectsApi';
import { libraryApi } from './libraryApi';
import { useSharedDocumentFiling } from './useSharedDocumentFiling';
import './Library.css';

export function DocumentPlacement({ projectId, projects, row, readonly=false, allowShared=false, onChanged, onError, onNavigate }) {
  const hint=row.placement, filing=row.filing, current=projects.find(project=>project.id===projectId);
  const currentScene=hint && 'current_scene' in hint ? hint.current_scene || '' : row.scene || '';
  const [choosing,setChoosing]=useState(false), [target,setTarget]=useState(projectId);
  const [targetKind,setTargetKind]=useState('personal'), [sharedProjects,setSharedProjects]=useState([]);
  const [sharedTarget,setSharedTarget]=useState(null), [sharedError,setSharedError]=useState(null);
  const [reload,setReload]=useState(0);
  const [scene,setScene]=useState(currentScene), [busy,setBusy]=useState(false);
  const gate=useRequestScope(`${projectId}:${row.document_id}:${row.revision}`), pending=useRef(false);
  const sharedEnabled = allowShared && !readonly && !current?.private;
  const shared = useSharedDocumentFiling({ enabled: sharedEnabled, project: projectId, row, onError, onChanged: result => {
    setChoosing(false); onChanged?.(result);
  } });
  useEffect(() => {
    if (!choosing || !sharedEnabled) return;
    const controller = new AbortController();
    setSharedError(null);
    sharedProjectsApi.scopes(controller.signal).then(rows => {
      if (!controller.signal.aborted) setSharedProjects(rows.filter(value => value.can_write));
    }).catch(error => { if (!controller.signal.aborted) setSharedError(error.message); });
    return () => controller.abort();
  }, [choosing, sharedEnabled, gate.scope, reload]);
  useEffect(() => {
    if (!choosing || targetKind !== 'shared') return;
    const controller = new AbortController();
    setSharedTarget(null); setSharedError(null);
    sharedProjectsApi.filingTarget(target, controller.signal).then(value => {
      if (value?.project_id !== target || typeof value.namespace !== 'string' || !value.namespace
          || !Array.isArray(value.scenes) || value.scenes.some(entry => typeof entry !== 'string')) throw new Error('共享项目未核对');
      if (!controller.signal.aborted) setSharedTarget(value);
    }).catch(error => { if (!controller.signal.aborted) setSharedError(error.message); });
    return () => controller.abort();
  }, [choosing, target, targetKind, gate.scope, reload]);
  const name=id=>projects.find(project=>project.id===id)?.name || id;
  async function mutate(operation) {
    if(pending.current) return;
    const request=gate.issue('placement',gate.scope);
    if(!request) return;
    pending.current=true;setBusy(true);
    try {
      const result=await operation();
      if(gate.isCurrent(request)){
        setChoosing(false);onChanged?.(result);
        onError?.(result.error ? '认识提炼未完成 · 重试' : '');
      }
    } catch(error){if(gate.isCurrent(request)) onError?.(error.message);}
    finally{if(gate.isCurrent(request)){pending.current=false;setBusy(false);}}
  }
  const move=(project,destination)=>mutate(()=>libraryApi.fileDocument(projectId,row,project,destination || null));
  if(readonly || !row.document_id || !Number.isInteger(row.revision)) return null;
  if (filing?.target_scope_kind === 'shared') {
    const label = `已移到 #${filing.target_project_name || filing.target_project_id}`;
    return <div className="library-actions">{onNavigate
      ? <button type="button" onClick={() => onNavigate('library', { project_id: filing.target_project_id,
        scope_kind: 'shared', layer: 'note', document_id: filing.target_document_id })}>{label}</button>
      : <span>{label}</span>}</div>;
  }
  if(filing) return <div className="library-actions"><span>已移到 #{name(filing.target_project_id)}</span>{filing.state==='filed' && <>
    {filing.error && <><span role="status">认识提炼未完成</span><button type="button" aria-label="重试提炼认识" disabled={busy} onClick={()=>move(filing.target_project_id,null)}>重试</button></>}
    <button type="button" aria-label="撤销移动" disabled={busy} onClick={()=>mutate(()=>libraryApi.unfileDocument(filing.target_project_id,filing.filing_document_id,filing.filing_revision))}>撤销</button>
  </>}</div>;
  const label=`#${name(projectId)}${currentScene ? '/'+currentScene : ''}`;
  const personalChoices=projects.filter(project=>!['me','inbox'].includes(project.id) && (!current?.private || project.id===projectId));
  const choices=sharedEnabled ? [...personalChoices, ...sharedProjects] : personalChoices;
  const destination=choices.find(project=>project.id===target && projectScopeKind(project)===targetKind);
  const chosenShared = targetKind === 'shared' && sharedTarget?.project_id === target ? sharedTarget : null;
  const locked = busy || shared.busy || Boolean(shared.command) || shared.blocked;
  const recovery = shared.command && <div className="library-actions">
    {shared.error && <span role="status">{shared.error}</span>}
    <button type="button" disabled={shared.busy} onClick={shared.check}>核对移入</button>
    {shared.canResume && <button type="button" disabled={shared.busy} onClick={shared.resume}>继续移入</button>}
    {shared.obsolete && <button type="button" disabled={shared.busy} onClick={shared.discard}>清除未受理操作</button>}
  </div>;
  return <div className="library-actions">
    <button type="button" disabled={busy || shared.busy} aria-label={`归属 ${label}`} onClick={()=>setChoosing(true)}>{label}</button>
    {!choosing && recovery}
    {!current?.private && hint?.project_id && hint.project_id!==projectId && personalChoices.some(project=>project.id===hint.project_id) && <button type="button" disabled={locked} aria-label={`移到 #${name(hint.project_id)}${hint.scene?'/'+hint.scene:''}`} onClick={()=>move(hint.project_id,hint.scene)}>→ #{name(hint.project_id)}{hint.scene?'/'+hint.scene:''}</button>}
    {choosing && <FocusPanel title="归属" onClose={()=>{if(!busy && !shared.busy)setChoosing(false);}}>
      <ProjectSwitcher projects={choices} value={target} scopeKind={targetKind} onChange={(value,project)=>{if(!locked){setTarget(value);setTargetKind(projectScopeKind(project));setScene('');}}}/>
      {sharedError && <><span role="status">{sharedError}</span><button type="button" disabled={locked} onClick={()=>setReload(value=>value+1)}>重读共享项目</button></>}
      {recovery}
      <label>场景<select aria-label="归属场景" disabled={locked} value={scene} onChange={event=>setScene(event.target.value)}><option value="">不设场景</option>{(chosenShared?.scenes || destination?.scenes || []).map(value=><option key={value} value={value}>{value}</option>)}</select></label>
      <button type="button" disabled={locked || !destination || targetKind==='shared' && !chosenShared || target===projectId && scene===currentScene} onClick={()=>targetKind==='shared'
        ? shared.start(chosenShared,scene) : target===projectId
        ? mutate(()=>libraryApi.documentScene(projectId,row,scene || null,hint?.assignment_revision || row.assignment_revision || 0))
        : move(target,scene)}>应用</button>
    </FocusPanel>}
  </div>;
}
