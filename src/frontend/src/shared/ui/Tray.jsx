import { useId, useRef, useState } from 'react';
import { StatusDot } from './StatusDot';
import './shell.css';
function jobProgress(job) {
  if (job.state === 'pending') return job.pending_count || '';
  if (job.progress == null) return '';
  if (typeof job.progress === 'number') return `${job.progress}%`;
  return job.progress.total > 0 ? `${Math.round(100 * job.progress.done / job.progress.total)}%` : '';
}
function canRetryJob(job) {
  return job.state === 'failed' && (job.target?.type === 'backup'
    || job.kind === 'intake' && ['item', 'turn'].includes(job.target?.type));
}
export function Tray({ jobs = [], onOpen, onRetry }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const trigger = useRef(null);
  const counts = ['processing', 'pending', 'failed'].map(state => ({ state, count: jobs.filter(job => job.state === state).length }));
  return <div className="ui-tray" onKeyDown={event => { if (event.key === 'Escape' && open) { event.preventDefault(); setOpen(false); trigger.current?.focus(); } }}>
    <button type="button" ref={trigger} className="ui-tray-pill" aria-label="进度" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(!open)}>
      {counts.map(({ state, count }) => <span className="ui-tray-count" key={state}><StatusDot state={state}/><span>{count}</span></span>)}
    </button>
    {open && <div id={id} className="ui-tray-list" role="region" aria-label="进度列表">{jobs.map(job => <div className="ui-tray-row" key={job.id}><StatusDot state={job.state}/><button type="button" className="ui-tray-job" onClick={() => { setOpen(false); onOpen?.(job); }}>{job.title}</button>{job.image_read?.local_fallback && <span className="ui-tray-meta" title="本机识图">本机</span>}{canRetryJob(job) ? <button type="button" className="ui-tray-retry" onClick={() => onRetry?.(job)}>重试</button> : <span className="ui-tray-meta">{jobProgress(job)}</span>}</div>)}</div>}
  </div>;
}
export default Tray;
