import { useInterventions } from './useInterventions';
import { interventionTextLength } from './interventionState';

const labels = { queued: '待读取', applied: '已读取', unapplied: '未读取' };
export function Interventions({ onNewRound, ...props }) {
  const value = useInterventions(props);
  const { data, text, pending, missing, sending, error } = value;
  const target = data.steer_target;
  const count = interventionTextLength(text);
  if (!target && !text && !pending && !data.steers?.length) return null;
  const title = data.receipt?.do?.title || data.user_text;
  const entries = data.steers || [];
  const history = entries.length > 0 && <details className="workbench-intervention-history">
    <summary>本轮插话 {entries.length} 条</summary>
    {entries.map(entry => <div className="workbench-intervention-entry" key={entry.id}>
      <span>{entry.text}</span><span className="ui-count">{labels[entry.state]}</span>
    </div>)}
  </details>;
  if (!props.active && !text && !pending) return history;
  return <section className="workbench-interventions" aria-label="中途插话">
    {history}
    {(target || text || pending) && <><div className="workbench-intervention-heading">
      <span>{target ? `补充本轮${target.part === null ? '' : `第${target.part + 1}部分`}` : '本轮插话草稿'}{title && <strong title={title}>{title}</strong>}</span>
      <span className="ui-count">{Number.isInteger(data.steer_remaining) ? `${5 - data.steer_remaining}/5` : '—/5'} · {count}/2000</span>
    </div><textarea aria-label="插话" rows={2} value={text} onChange={event => value.draft(event.target.value)}
      onDragOver={event => { if (Array.from(event.dataTransfer.types).includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'none'; } }}
      onDrop={event => { if (event.dataTransfer.files.length) event.preventDefault(); }}
      placeholder="补充本轮要求" aria-invalid={count > 2000} disabled={sending}/>
      <div className="workbench-intervention-actions">
        {pending ? <><span role="status">结果待核对</span><button type="button" disabled={sending} onClick={() => value.recover()}>核对结果</button>
          {missing && <button type="button" disabled={sending} onClick={value.submit}>重试原插话</button>}</>
          : target ? <button type="button" disabled={sending || !text.trim() || count > 2000 || data.steer_remaining === 0} onClick={value.submit}>插话</button>
            : text && <button type="button" onClick={() => onNewRound(text, () => value.draft(''))}>移到新轮输入</button>}
      </div></>}
    {error && <div role="alert">{error}</div>}
  </section>;
}
