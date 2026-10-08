import { useRef, useState } from 'react';
import { FocusPanel } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { workbenchApi } from '../workbench/workbenchApi';
import { libraryApi } from './libraryApi';

export function InboxProjectSuggestion({ group, rows, scope, onChanged, onProjectCreated }) {
  const gate=useRequestScope(scope), pending=useRef(false);
  const [choosing,setChoosing]=useState(false), [name,setName]=useState(group.name), [busy,setBusy]=useState(false);
  const [progress,setProgress]=useState(null), [error,setError]=useState('');
  const retained=useRef({project:null,completed:new Set()});
  async function archive() {
    if(pending.current) return;
    const request=gate.issue('archive',gate.scope);
    if(!request) return;
    pending.current=true;setBusy(true);setError('');
    try {
      if(!retained.current.project){
        const project=await workbenchApi.createProject(name.trim());
        if(!gate.isCurrent(request)) return;
        retained.current.project=project;
        onProjectCreated?.(project);
      }
      for(const id of group.ids){
        if(retained.current.completed.has(id))continue;
        if(!gate.isCurrent(request))return;
        const row=rows.find(item=>item.id===id);
        if(!row)throw new Error('内容已变化 · 刷新');
        await libraryApi.fileInbox(retained.current.project.id,row,null);
        if(!gate.isCurrent(request))return;
        retained.current.completed.add(id);setProgress(retained.current.completed.size);
      }
      if(gate.isCurrent(request)){setChoosing(false);onChanged?.();}
    }catch(reason){if(gate.isCurrent(request))setError(`已归档 ${retained.current.completed.size}/${group.ids.length} · ${reason.message}`);}
    finally{if(gate.isCurrent(request)){pending.current=false;setBusy(false);}}
  }
  return <div className="library-inbox-filing"><button type="button" aria-label={`新建项目 ${group.name} ${group.count}`} onClick={()=>setChoosing(true)}>＋ #{group.name} <span>{group.count}</span></button>
    {choosing && <FocusPanel title="新建项目" onClose={()=>{if(!busy)setChoosing(false);}}>
      <label>名称<input aria-label="项目名称" maxLength={40} disabled={busy || Boolean(retained.current.project)} value={name} onChange={event=>setName(event.target.value)}/></label>
      {progress!==null && <span>{progress}/{group.count}</span>}
      {error && <p role="alert">{error}</p>}
      <button type="button" disabled={busy || !name.trim()} onClick={archive}>{error?'重试归档':'归档'}</button>
    </FocusPanel>}
  </div>;
}
