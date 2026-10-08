import { Icon, Row } from '../../shared/ui';

export function OutcomeVersion({ version }) {
  return Number.isSafeInteger(version) && version > 1 ? <span className="ui-count">v{version}</span> : null;
}

export function OutcomeChanges({ changes = [], onSelect }) {
  if (!changes.length) return null;
  return <details><summary className="ui-count">改动 {changes.length}</summary>
    {changes.map((change, index) => <Row key={index} title={change.path.join(' / ')}
      dot={change.kind === 'added'
        ? <span aria-label="新增" style={{ color: 'var(--red)' }}><Icon name="plus" size={11}/></span>
        : <span aria-label="更新" className="ui-status-dot" style={{ width: 6, height: 6, background: 'var(--red)' }}/>}
      onOpen={() => onSelect?.(change.path)} readOnly={!onSelect}/>)}</details>;
}
