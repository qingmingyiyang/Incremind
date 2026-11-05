import { useEffect, useId, useState } from 'react';
import { Icon, Row } from '../../shared/ui';
import { formatLocalDate } from '../../shared/lib/time';

const EMPTY_USAGE = [];

export function CitationUsage({ entries = EMPTY_USAGE, onOpen }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  useEffect(() => { setOpen(false); }, [entries]);
  const expanded = open && entries.length > 0;
  return <section className="library-used-by" aria-label="被用过">
    <button type="button" className="library-used-by-toggle" title="被用过"
      aria-label={`被用过 ${entries.length} 次`} aria-expanded={expanded} aria-controls={listId}
      disabled={!entries.length} onClick={() => setOpen(value => !value)}>
      <Icon name="related" size={16}/><span className="ui-count">{entries.length}</span>
    </button>
    <div id={listId} className="library-used-by-rows" hidden={!expanded}>
      {entries.map(entry => <Row key={`${entry.use}:${entry.thread_id}:${entry.turn_id}`}
        className="library-used-by-row" dot={<Icon name={entry.use === 'outcome' ? 'do' : 'ask'} size={14}/>}
        title={entry.title} meta={entry.created_at ? formatLocalDate(entry.created_at) : undefined}
        trailing={entry.changed ? <span className="library-used-by-changed" role="img" title="内容已变化" aria-label="内容已变化"><Icon name="review-reask" size={14}/></span> : undefined}
        readOnly={!onOpen} onOpen={() => onOpen(entry)}/>)}
    </div>
  </section>;
}

export default CitationUsage;
