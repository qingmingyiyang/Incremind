import { Icon, StatusDot } from '../../shared/ui';
import { insightUsageCount } from './reviewShortcuts';

export function ProfileCards({ items, busy, onEdit, onOpen, onForget, onAdd }) {
  const pending = items.filter(item => item.state === 'pending');
  const groups = new Map();
  for (const item of items.filter(item => item.state !== 'pending')) {
    const name = item.scene || (item.pattern ? '规律' : '关于我');
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }
  if (!groups.size) groups.set('关于我', []);
  return <section className="library-profile" aria-label="我">
    <header className="library-profile-heading"><h1>我</h1><span className="library-meta">已生效的认识可用于所有项目</span></header>
    {pending.length > 0 && <section className="library-profile-pending" aria-label="画像待确认">{pending.map(item => <div className="library-profile-pending-row" key={item.id}>
      <StatusDot state="pending"/><button type="button" className="library-profile-text" disabled={busy} onClick={() => onOpen(item)}>{item.text}
        {item.conditions?.length > 0 && <span className="library-profile-condition">{item.conditions.join(' · ')}</span>}</button>
      <button type="button" className="library-profile-evidence" aria-label={`查看依据 ${item.text}`} disabled={busy} onClick={() => onOpen(item)}>查看依据</button>
    </div>)}</section>}
    <div className="library-profile-grid">{Array.from(groups, ([name, rows]) => <section className="library-profile-card" key={name} aria-label={name}>
      <h2>{name}<span className="library-meta">{rows.filter(row => row.state !== 'forgotten').length}</span></h2>
      {rows.map(item => <div className={`library-profile-row ${item.state === 'forgotten' ? 'is-forgotten' : ''}`} key={item.id}>
        <StatusDot state={item.state === 'forgotten' ? 'forgotten-user' : item.state === 'stale' ? 'unverified' : 'done'}/>
        <button type="button" className="library-profile-text" title={item.state === 'active' && item.can_edit_insight !== false ? '点一下改' : '打开认识'} disabled={busy} onClick={() => item.state === 'active' && item.can_edit_insight !== false ? onEdit(item) : onOpen(item)}>{item.text}</button>
        <span className="library-meta">使用 {insightUsageCount(item) ?? '—'}</span>
        {item.kind === 'recognition' && ['active', 'forgotten'].includes(item.state) && <button type="button" className="library-profile-icon" disabled={busy} aria-label={`${item.state === 'forgotten' ? '恢复' : '遗忘'} ${item.text}`} title={item.state === 'forgotten' ? '恢复' : '遗忘'} onClick={() => onForget(item, item.state !== 'forgotten')}><Icon name={item.state === 'forgotten' ? 'review-reask' : 'review-unused'} size={16}/></button>}
      </div>)}
      <button type="button" className="library-profile-add" disabled={busy || !onAdd} onClick={() => onAdd(name)}><Icon name="plus" size={16}/>补一条</button>
    </section>)}</div>
  </section>;
}
