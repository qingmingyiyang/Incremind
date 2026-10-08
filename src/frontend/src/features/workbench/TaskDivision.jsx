import { useRef, useEffect, useState } from 'react';
import { Row } from '../../shared/ui';
import { workbenchApi } from './workbenchApi';

const toolNames = {'memory.recall':'查找资料','source.evidence.read':'读取原件',
 'document.draft.propose':'起草整理稿','analyze_source':'分析原件','project_skill.evidence.read':'读取方法'};

export function TaskDivision({ project, turnId, task, api = workbenchApi, onRedo, onReference }) {
 const [expanded,setExpanded] = useState(null), [sample,setSample] = useState(null);
 const [items,setItems] = useState([]), [busy,setBusy] = useState(false), [error,setError] = useState('');
 const [deleted,setDeleted] = useState(false);
 const current=useRef(0);
 useEffect(()=>{ current.current++; setSample(null); setItems([]); setError(''); setDeleted(false); setBusy(false); return ()=>{current.current++;}; },[project,turnId]);
 async function run(action) {
  const generation=current.current; setBusy(true); setError('');
  try { const result=await action(); return current.current===generation ? result : null; }
  catch { if(current.current===generation) setError('分工未更新 · 重试'); return null; }
  finally { if(current.current===generation) setBusy(false); }
 }
 async function load() { const value=await run(()=>api.division(project,turnId)); if(value){setSample(value);setItems(value.items);} }
 const update=(index,changes)=>setItems(old=>old.map((item,i)=>i===index?{...item,...changes}:item));
 function remove(index) {
  setItems(old=>old.filter((_,i)=>i!==index).map(item=>({...item,depends_on:item.depends_on.filter(i=>i!==index).map(i=>i>index?i-1:i)})));
 }
 function split(index) {
  setItems(old=>old.flatMap((item,i)=>{
   const deps=item.depends_on.flatMap(dep=>dep===index?[index,index+1]:[dep>index?dep+1:dep]);
   if(i!==index) return [{...item,depends_on:deps}];
   return [{...item,goal:item.goal+'（一）',depends_on:deps},{...item,goal:item.goal+'（二）',depends_on:deps}];
  }));
 }
 function merge(index) {
  setItems(old=>old.filter((_,i)=>i!==index+1).map((item,i)=>{
   const combined=i===index ? {...item,goal:item.goal+'；'+old[index+1].goal,
    deliverable:item.deliverable+'；'+old[index+1].deliverable,
    capabilities:[...new Set([...item.capabilities,...old[index+1].capabilities])],
    depends_on:[...new Set([...item.depends_on,...old[index+1].depends_on])].filter(dep=>dep!==index&&dep!==index+1)} : item;
   return {...combined,depends_on:[...new Set(combined.depends_on.map(dep=>dep===index+1?index:dep>index+1?dep-1:dep))]};
  }));
 }
 const terminal=['done','partial','failed'].includes(task.state);
 return <div className="workbench-division">
  {(task.division||[]).map((item,index)=><div key={item.assignment_id||index}>
   <Row dot={item.state==='running'?'processing':item.state==='waiting'?'unverified':item.state}
    title={item.goal} expanded={expanded===index} onOpen={()=>setExpanded(expanded===index?null:index)}/>
   {expanded===index&&<div className="workbench-division-detail">
    <span>{item.deliverable}</span><span className="workbench-division-usage">{item.model_usage?.input_tokens??'—'} / {item.model_usage?.output_tokens??'—'}</span>
    {(item.tools||[]).map(tool=><Row key={tool.id} title={toolNames[tool.capability_id]||'工具'}
      dot={tool.state==='running'?'processing':tool.state==='waiting'?'unverified':tool.state} readOnly/>)}
    {(item.recalled||[]).map((entry,i)=><div key={i}><Row title={entry.title} readOnly/><p>{entry.text}</p></div>)}
   </div>}
  </div>)}
  {terminal&&<details onToggle={event=>{if(event.currentTarget.open&&!sample&&!deleted&&!busy) load();}}>
   <summary>分工{sample?.adjusted?' · 已调整':''}</summary>
   {sample&&<form onSubmit={async event=>{event.preventDefault();const value=await run(()=>api.saveDivision(project,turnId,items,sample.revision));if(value){setSample(value);setItems(value.items);}}}>
    {items.map((item,index)=><fieldset key={index} disabled={busy}>
     <legend>{index+1}</legend>
     <label>目标<input aria-label={`目标 ${index+1}`} value={item.goal} maxLength={2000} required onChange={event=>update(index,{goal:event.target.value})}/></label>
     <label>交付<input aria-label={`交付 ${index+1}`} value={item.deliverable} maxLength={2000} required onChange={event=>update(index,{deliverable:event.target.value})}/></label>
     <label>依赖<input aria-label={`依赖 ${index+1}`} value={item.depends_on.map(i=>i+1).join(',')} onChange={event=>update(index,{depends_on:event.target.value.trim()?event.target.value.split(',').map(i=>Number(i.trim())-1):[]})}/></label>
     <button type="button" onClick={()=>split(index)} disabled={items.length>=8}>拆分</button>
     <button type="button" onClick={()=>merge(index)} disabled={index===items.length-1}>合并</button>
     <button type="button" aria-label={`删除第${index+1}项`} disabled={items.length===1} onClick={()=>remove(index)}>×</button>
    </fieldset>)}
    <button type="button" disabled={busy||items.length>=8} aria-label="添加一项" onClick={()=>setItems(old=>[...old,{goal:'',deliverable:'整理稿',capabilities:['memory.recall','document.draft.propose'],depends_on:[]}])}>＋</button>
    <button type="submit" disabled={busy}>保存</button>
    <button type="button" disabled={busy||JSON.stringify(items)!==JSON.stringify(sample.items)} aria-label="按此分工重做" onClick={async()=>{const result=await run(()=>api.redo(project,turnId,sample.revision));if(result)onRedo?.(result);}}>重做</button>
    <button type="button" disabled={busy} aria-label="删除分工样例" onClick={async()=>{const value=await run(()=>api.deleteDivision(project,turnId,sample.revision));if(value){setDeleted(true);setSample(null);setItems([]);}}}>删除样例</button>
   </form>}
   {!!task.division_examples?.length&&<div>{task.division_examples.map((item,index)=><button type="button" key={item.turn_id} aria-label={`查看参考分工 ${index+1}`} onClick={()=>onReference?.(item)}>↗ {index+1}</button>)}</div>}
   {error&&<div role="alert">{error}</div>}
  </details>}
 </div>;
}
