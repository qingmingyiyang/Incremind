import { Receipt, Row, StatusDot } from '../../shared/ui';

export function SharedInspirationReceipts({ state, projectId, canWrite, onNavigate, retryDisabled, retry = state.retry }) {
  return <section aria-label="共享灵感记录">{state.commands.map(command => {
    const value = state.values[command.id], insight = value?.insight;
    return <article key={command.id}>
      <div className="workbench-bubble">{command.body.text}</div>
      <Receipt kind="inspiration">
        {insight ? <Row title={insight.content} dot={insight.state === 'pending' ? 'pending' : 'done'}
          meta={insight.state === 'pending' ? '待确认' : insight.state === 'published' ? '已确认' : '已丢弃'}
          readOnly={insight.state === 'rejected' || !onNavigate} onOpen={() => onNavigate?.('library', {
            project_id: projectId, scope_kind: 'shared', layer: 'insight',
            ...(insight.state === 'pending' ? { insight_id: insight.id, filter: 'pending_memory' } : {}),
          })}/> : <div className="workbench-receipt-title"><StatusDot state="unverified"/><span>{command.phase === 'not_received' ? '尚未受理' : '结果待核对'}</span>
          <button type="button" disabled={state.working} onClick={() => state.read(command)}>核对</button>
          {canWrite && command.phase === 'not_received' && <button type="button" disabled={state.working || retryDisabled} onClick={() => retry(command)}>重试</button>}
        </div>}
        {state.errorId === command.id && <p className="workbench-error" role="alert">{state.error}</p>}
      </Receipt>
    </article>;
  })}</section>;
}
